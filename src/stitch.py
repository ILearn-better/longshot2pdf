"""重叠检测 + 流式拼接 + 自动分页。

两个设计决定，直接决定这个工具能不能截 500 页：

1) 重叠检测不用固定位移，用画面内容自己对齐。
   网页滚动经常不是整格整数倍（平滑滚动、列表懒加载、缩放），
   固定位移一定会错位。所以必须让画面自己告诉我们「滚了多少」。

2) 内存里永远只保留不到两页。
   500 页 × 1400px ≈ 70 万像素高，整图 RGB 需要 2.1GB，必然 OOM。
   所以缓冲区一满一页就立刻编码成 JPEG 落盘，剩余的 memmove 回开头。
   峰值内存与总长度无关。


定位为什么分三级
----------------
第一代实现只有「缩略图指纹 + 小半径精修」，实测有两类必然失败：

* **纵向降采样相位失配**。把 1400 行 BOX 缩到 72 行后，缩略图第 i 行
  实际代表原图第 [i·19.4, (i+1)·19.4) 行。上一帧与当前帧的滚动位移若不是
  19.4 的整数倍，同一个缩略图行在两帧里覆盖的原图区间就不一样，
  即使位置完全正确，MAD 也会远大于阈值 → 判成「匹配失败」。
* **周期结构 / 空白区误匹配**。指纹只有 12 行，落在页面空白处时任意位置
  MAD≈0，会给出完全错误的位置；表格等距横线同理。

所以现在改成三级，每一级解决上一级解决不了的问题：

    全局候选（纵向不降采样，无相位问题）
        ↓ 快，但只在 8 个横向块上比较，还不够精确
    抽样验证（全分辨率 RGB，带横向信息）
        ↓ 只用来挑大方向，它算出的值不可与下一级比大小
    全量精修（同一窗口、同一度量，三个候选重算一遍再取最小）
        ↓ 最终判据，也是返回给调用方的 error

关键点：**纵向一列都不降采样**。横向压到 8 维是为了让纯 1D 行均值
也能区分左右不同区域，避免被贯穿整幅的周期横线骗到。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image

SMALL_W = 64          # 匹配用缩略宽度（旧接口保留）
DEFAULT_K = 12        # 指纹行数（旧接口保留）
MAX_MATCH_ERROR = 8.0  # 旧接口的 MAD 阈值（旧接口保留）

# ---------------------------------------------------------------- 新算法参数

SIG_BLOCKS = 8        # 行块签名的横向分块数
# 为什么是 8：横向切太少（2~4）就退化成纯 1D 行均值，正文里贯穿整幅的
# 表格横线会让「上下都是横线」的位移同分；切太多（16+）单块只剩几十列，
# 块内均值被文字抗锯齿抹平，判别力反而下降，且 FFT 批量变宽更慢。
# 实测 8 块在 800~1600 宽的所有用例上都能唯一确定位移。

MIN_OVERLAP_RATIO = 20   # 最小重叠占视口高的比例：h // 20 ≈ 5%
MIN_OVERLAP_FLOOR = 32   # 最小重叠行数的绝对下限
# 为什么是 5% 而不是「随便几行」：重叠区就是唯一的判据面积，行数越少信噪比越差。
# 用 800 高视口做过统计（每个组合 77 个随机位置）：
#     重叠 60 行 77/77 · 48 行 77/77 · 40 行 77/77 · 32 行 77/77 · 25 行 76/77
# 25 行那一次返回 None（安全失败，但要多花一次缩步长重试）。40 行（5%）是
# 实测的可靠边界。另一方面，允许 95% 视口高的位移意味着「重叠 ≥5%」以外的
# 帧一律拒判 —— 这正好把「一帧内滚了快一整屏」这种不可靠情形退回给编排层
# 去缩步长，比赌一个位置安全得多。下限 32 行保证小视口不会退化成几行判据；
# 再用 h//4 封顶，避免 200px 高的迷你视口被这条规则直接判死。

MAX_MATCH_ERR = 12.0  # 全量精修后可接受的 MAD 上限（0~255 标度）
# 为什么是 12：同一画面在 JPEG 压缩/抗锯齿抖动下 MAD 通常 < 3；
# 而「完全不同的两块正文」MAD 普遍 > 25。12 落在两者之间，
# 留出 4 倍安全余量，又不至于把轻微重绘抖动误判成失败。

CAND_KEEP = 5         # 保留的候选位移个数
CAND_SEP = 6          # 候选之间的最小间隔（像素）
# 为什么间隔 6：lag-MSE 的局部极小常连成一片（同一条横线上下 2~3px 都近似），
# 不设间隔会把 5 个名额全浪费在同一个凹坑里，真正正确的那个反而被挤掉。

SAMPLE_ROWS = 160     # 抽样验证取的扫描行数
# 为什么是 160：1000 宽的帧全量比较约 1.4M 像素/次，5 候选 × 3 邻位 = 15 次，
# 全量要 30ms+；抽 160 行（约 11%）已经足够把错误候选的 MAD 顶到 25 以上，
# 耗时降到 3ms 量级。真正的 1px 判据留给最后一级全量精修。

IDLE_SIG_RMS = 1.5    # 「画面完全没动」判据：签名域 RMS 的绝对阈值
IDLE_SIG_RMS_MAX = 4.0  # 次条件生效前提：两帧签名域 RMS 至少这么接近
IDLE_RATIO = 0.85     # 次条件：最优位移的解释力不优于零位移的 85%
# 为什么到底判定必须放在**签名域**而不是像素域（这是实测踩出来的坑）：
# 像素域 MAD 的噪声底与信号完全重叠，任何阈值都分不开。1000×900 实测：
#     像素噪声 ±2 -> 像素 MAD 1.20   而真实滚动 1px -> 像素 MAD 3.18
# 也就是说「带 ±2 抖动的静止画面」比「真的滚了 1px」还难区分，
# 阈值取 0.8 会把到底判成 delta=1（VirtualScreen(noise=2) 实测确实返回 (899,1)），
# 编排层就会一直以小位移追加重复内容、永远检测不到到底。
# 换到签名域（每块 125 列 × 3 通道先求行均值，噪声被压低约 √375 ≈ 19 倍）：
#     像素噪声 ±2 -> 签名 RMS 0.058   真实滚动 1px -> 签名 RMS 7.01
# 两者差 120 倍，阈值 1.5 两边都有充足余量（噪声 26 倍、信号 4.7 倍）。
# 次条件兜住更脏的输入：纯色/大面积裁剪导致签名整体抬升时，只要「最优位移
# 并不比零位移更像」，仍然判为到底；再用 IDLE_SIG_RMS_MAX 限制它只在
# 两帧整体接近时生效，避免把「滚过头（两帧毫无关系）」误判成到底。

REFINE_MAX_ROWS = 900  # 全量精修最多比较的行数
# 精修是唯一 O(重叠行数) 的步骤：位移很小时重叠接近整帧（1400 行），
# 3 个候选 × 1400 行 × 1000px × 3 通道 = 1260 万像素，单帧就要 30ms+。
# 900 行（270 万像素）已经足够把「差 1px」和「差 0px」分开，
# 再加行只是线性变慢、判断不变。超过 900 行时取重叠区的**中部**，
# 避开顶部可能存在的整片页边距（那里三个位移的 MAD 都是 0，无法区分）。

SIG_MIN_STD = 1.0     # 重叠区行块签名的最低标准差（低于此视为「没有信息」）
# 为什么需要这道守卫：整段纯色（大片留白 / 纯色底）时任何位移的 MAD 都是 0，
# 「最小残差」只是在候选之间瞎挑。真实文档的正常行块签名标准差在 15~30，
# 纯色是 0，取 1.0 只拦「彻底没有纹理」的情形，不会误伤浅色页。


def page_height_for(width: int, ratio: float = 1.414) -> int:
    """按 A4 纵向比例推算单页像素高度。"""
    return max(1, int(round(width * ratio)))


def prep_gray(frame: np.ndarray, small_w: int = SMALL_W) -> np.ndarray:
    """转灰度并横向降采样到 small_w 列，用于快速匹配。"""
    img = Image.fromarray(frame).convert("L")
    h = max(1, int(round(img.height * small_w / img.width)))
    if (img.width, img.height) != (small_w, h):
        img = img.resize((small_w, h), Image.BOX)
    return np.asarray(img)


def match_offset(
    prev_small: np.ndarray,
    cur_small: np.ndarray,
    k: int = DEFAULT_K,
    max_error: float = MAX_MATCH_ERROR,
) -> tuple[int, float] | None:
    """在当前帧中定位上一帧底部 K 行的位置。

    返回 (y, error)：
        cur_small[y : y+k] 匹配 prev 的最后 k 行
        全新内容从 cur 的 y+k 行开始
    匹配失败（error 过大或尺寸异常）返回 None，调用方应停止采集，
    宁可少截也不要写入错位的脏数据。

    注意：这是第一代接口，`locate()` 已不再使用它（纵向降采样有相位失配，
    见模块 docstring）。保留是因为它仍是公开契约的一部分，且对
    「同尺寸、整格滚动」这种最理想场景依然够用。
    """
    if prev_small.ndim != 2 or cur_small.ndim != 2:
        return None
    hp, w = prev_small.shape
    hc, wc = cur_small.shape
    if w != wc or hp < k or hc < k + 2:
        return None

    tail = prev_small[-k:].astype(np.int16)
    windows = sliding_window_view(cur_small, (k, w))[:, 0].astype(np.int16)
    mad = np.abs(windows - tail).mean(axis=(1, 2))
    y = int(mad.argmin())
    err = float(mad[y])
    if err > max_error:
        return None
    return y, err


# ---------------------------------------------------------------- 第一级：行块签名


def sig(frame: np.ndarray, nb: int = SIG_BLOCKS) -> np.ndarray:
    """把整帧压成 (h, nb) 的「行块签名」：每块的行均值亮度。

    - **竖向保持原始行数**（关键）：不降采样就没有相位失配，
      位移 d 一定对应签名的整数行偏移 d。
    - **横向只压到 nb 块**：既保留「左边有字右边空白」这类左右分布信息
      （纯 1D 行均值会被贯穿整幅的表格横线骗到），又把 FFT 的宽度压到 8。

    返回 float32，值域 0~255。块宽取 floor(w/nb)，丢掉右侧除不尽的几列，
    这对匹配无影响（那几列在每一帧都被同样丢掉）。
    """
    h, w = int(frame.shape[0]), int(frame.shape[1])
    nb = max(1, min(int(nb), w))
    bw = w // nb
    if bw < 1 or h < 1:
        raise ValueError(f"帧太小无法签名: {frame.shape}")

    # 视图切片 + reshape 合并成 (h, nb, bw*3)，不产生拷贝
    block = frame[:, : nb * bw]
    # 用 uint32 而非 float32 累加：整数加法向量化更好，实测快 25%，
    # 且 255*3*bw 远小于 uint32 上限，结果与浮点累加逐位一致。
    total = block.reshape(h, nb, bw * 3).sum(axis=2, dtype=np.uint32)
    return total.astype(np.float32) * np.float32(1.0 / (bw * 3))


# ---------------------------------------------------------------- 第二级：lag-MSE


def lag_mse(
    sig_prev: np.ndarray,
    sig_cur: np.ndarray,
    d_min: int = 1,
    d_max: int | None = None,
) -> np.ndarray:
    """对所有候选位移 d，返回归一化块均方误差，索引 = 位移。

    定义（对每个横向块独立计算，最后 8 块求平均）：
        overlap = h - d
        mse[d]  = mean_t |prev[d+t] - cur[t]|²,  t ∈ [0, overlap)

    为什么用平方误差而不是绝对差：平方误差可以拆成
        Σ(A²) + Σ(B²) - 2·ΣAB
    前两项是后缀和/前缀和，第三项是一次互相关 —— 于是**整段位移范围
    一次算完**，不用对每个 d 重复扫帧。

    为什么除以重叠长度：不除的话大位移天然只累计少量行，数值必然更小，
    全局最小会永远偏向最大位移（那是错的）。

    为什么用 FFT 做互相关：8 个块 × 直接 O(h²) 卷积 ≈ 1600 万次乘加，
    实测 10ms 以上；补零到 2h 做一次批量 rfft 只要 1~2ms。
    """
    hp, nb = sig_prev.shape
    hc = sig_cur.shape[0]
    if hp != hc or nb != sig_cur.shape[1]:
        raise ValueError("两帧签名尺寸不一致")
    h = hp
    if d_max is None:
        d_max = h - 1
    d_max = int(min(d_max, h - 1))

    full = np.full(h + 1, np.inf, dtype=np.float64)  # 索引即位移，0 位不用
    if d_max < d_min:
        return full

    # 补零到 >= 2h，保证 d ∈ [0, h) 的线性相关不会被循环卷积混叠
    nfft = 1 << int(np.ceil(np.log2(2 * h)))
    fa = np.fft.rfft(sig_prev, n=nfft, axis=0)
    fb = np.fft.rfft(sig_cur, n=nfft, axis=0)
    # ifft(FFT(A)·conj(FFT(B)))[d] = Σ_t A[t+d]·B[t]
    corr = np.fft.irfft(fa * np.conj(fb), n=nfft, axis=0)

    a = sig_prev.astype(np.float64)
    b = sig_cur.astype(np.float64)
    a2 = np.cumsum((a * a)[::-1], axis=0)[::-1]      # a2[d] = Σ_{t>=d} a[t]²
    b2 = np.concatenate(
        [np.zeros((1, nb)), np.cumsum(b * b, axis=0)], axis=0
    )                                                # b2[n] = Σ_{t<n}  b[t]²

    d = np.arange(d_min, d_max + 1)
    overlap = (h - d).astype(np.float64)[:, None]
    num = a2[d] + b2[h - d] - 2.0 * corr[d]
    np.maximum(num, 0.0, out=num)  # 浮点误差可能给出 -1e-12
    full[d] = (num / overlap).mean(axis=1)
    return full


def _pick_candidates(mse: np.ndarray, keep: int = CAND_KEEP, sep: int = CAND_SEP) -> list[int]:
    """从 lag-MSE 曲线里挑出若干互不相邻的局部极小位移。

    只看全局最小是不够的：周期结构上全局最小经常落在错误的位置，
    正确位移排在第二第三。所以多留几个候选，交给带横向信息的一级去裁决。
    """
    vals = mse[1:]
    if vals.size == 0:
        return []
    core = np.isfinite(vals)
    if not core.any():
        return []
    v = np.where(core, vals, np.inf)

    # 局部极小：不大于左右邻居。边界用 +inf 补齐，避免索引越界。
    padded = np.concatenate(([np.inf], v, [np.inf]))
    is_min = (v <= padded[:-2]) & (v <= padded[2:])
    idx = np.flatnonzero(is_min)
    if idx.size == 0:
        idx = np.array([int(np.argmin(v))])

    order = idx[np.argsort(v[idx], kind="stable")]
    picked: list[int] = []
    for j in order:
        d = int(j) + 1
        if all(abs(d - p) >= sep for p in picked):
            picked.append(d)
            if len(picked) >= keep:
                break
    if not picked:  # 理论上不会发生，兜底保证有候选
        picked = [int(np.argmin(v)) + 1]
    return picked


# ---------------------------------------------------------------- 第三级：像素判据


def _row_sample(n: int, count: int = SAMPLE_ROWS) -> np.ndarray:
    """在 [0, n) 内均匀取 count 个行号（去重）。"""
    count = int(min(count, n))
    if count <= 0:
        return np.empty(0, dtype=np.intp)
    return np.unique(np.linspace(0, n - 1, count).astype(np.intp))


def _mean_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    """两段同形状 uint8 RGB 的平均绝对差（0~255）。

    用 max-min 而不是 abs(a-b)：uint8 直接相减会回绕，转 int16 又要多一次
    3 倍内存的拷贝 —— 实测 int16 路线在这台机器上比 max/min 慢 50%。
    |a-b| == max(a,b) - min(a,b) 在无符号域恒成立，且全程零类型转换。
    """
    if a.size == 0:
        return float("inf")
    hi = np.maximum(a, b)
    lo = np.minimum(a, b)
    hi -= lo
    # uint64 累加：4096×1600×3×255 ≈ 5e9 已经超出 uint32，必须留足余量
    return float(hi.sum(dtype=np.uint64)) / float(a.size)


def _sampled_mad(
    prev: np.ndarray, cur: np.ndarray, d: int, rows: np.ndarray
) -> float:
    """重叠区抽样 MAD：比较 prev[d + rows] 与 cur[rows]，全分辨率 RGB。

    抽样在这里是安全的：错误候选的差异是结构性的（整行整段的字形不同），
    160 行足以把它暴露出来；而 ±1px 的细微差别交给最后一级。

    ⚠ 这个值**只能用来在同一级内部给候选排序**，不能和 `_full_mad` 的返回值
    比大小：两者抽样集合不同（这里按各候选自己的 overlap 取 160 行，`_full_mad`
    取公共窗口整段），数值不可比。最终输出给调用方的 error 一律来自 `_full_mad`。
    """
    t = rows[rows < min(prev.shape[0] - d, cur.shape[0])]
    if t.size == 0:
        return float("inf")
    return _mean_abs_diff(prev[d + t], cur[t])


def _full_mad(prev: np.ndarray, cur: np.ndarray, d: int, off: int, rows: int) -> float:
    """重叠区第 off 行起连续 rows 行的整段 RGB 平均绝对差（1px 判据）。"""
    if rows <= 0:
        return float("inf")
    return _mean_abs_diff(prev[d + off : d + off + rows], cur[off : off + rows])


def locate(
    prev_full: np.ndarray,
    cur_full: np.ndarray,
    k_small: int = DEFAULT_K,
    small_w: int = SMALL_W,
    max_error: float = MAX_MATCH_ERR,
    min_overlap: int | None = None,
    blocks: int = SIG_BLOCKS,
) -> tuple[int, int, float] | None:
    """定位当前帧中「上一帧内容」的结束位置。

    返回 (new_start, delta_px, error)：
        new_start : cur_full 中全新内容的起始行号，新内容 = cur_full[new_start:]
        delta_px  : 本帧相对上一帧的滚动位移（像素），满足 new_start = h - delta_px
        error     : **全量精修级残差**。整段重叠区逐像素 RGB MAD（0~255 标度），
                    不是抽样值。为了让 d-1/d/d+1 三个候选比较同样多的行，
                    实际取的是三者的公共重叠窗口，比 d 自身重叠少 1~2 行。
    失败返回 None（宁可少截也不写脏数据）。

    特殊情况：画面完全没变（已滚动到底）返回 (h, 0, err)，
    由编排层据此判定到底。这一步必须在正常匹配之前做，
    否则候选位移的最小值 1 会把「零位移」这个语义吞掉。

    min_overlap : 允许的最小重叠行数，默认 max(32, h//20)（约 5% 视口高）。
                  重叠行数越少判据越薄，实测 25 行开始出现失败，40 行稳定。

    k_small / small_w 只为兼容旧签名而保留，新算法不使用。
    """
    if prev_full.ndim != 3 or cur_full.ndim != 3:
        return None
    h = int(prev_full.shape[0])
    if h != int(cur_full.shape[0]) or h < 4:
        return None
    if prev_full.shape[1] != cur_full.shape[1] or prev_full.shape[2] != 3:
        return None

    # ---- 1) 签名 + 位移范围 ----
    if min_overlap is None:
        min_overlap = min(
            h // 4, max(MIN_OVERLAP_FLOOR, h // MIN_OVERLAP_RATIO)
        )
    min_overlap = int(max(1, min(min_overlap, h - 1)))
    d_max = h - min_overlap

    sig_p = sig(prev_full, blocks)
    sig_c = sig(cur_full, blocks)

    # 所有候选位移的归一化块 MSE（索引即位移，0 号位留空）
    mse = lag_mse(sig_p, sig_c, 1, d_max) if d_max >= 1 else None

    # ---- 2) 画面静止 = 到底 ----
    # 必须在正常匹配之前判：候选位移的下限是 1，零位移这个语义会被它吞掉。
    # 判据放在签名域，理由见 IDLE_SIG_RMS 处的注释（像素域噪声底会盖过信号）。
    d0 = sig_p - sig_c
    idle_rms = float(np.sqrt(np.mean(d0 * d0)))
    if idle_rms <= IDLE_SIG_RMS:
        return h, 0, idle_rms
    if mse is not None:
        best_mse = float(np.min(mse[1 : d_max + 1]))
        # 次条件：「最优位移并不比零位移更像」+「两帧整体本来就很接近」
        if idle_rms <= IDLE_SIG_RMS_MAX and np.isfinite(best_mse):
            if best_mse >= (idle_rms * idle_rms) * IDLE_RATIO:
                return h, 0, idle_rms

    if mse is None:
        return None

    # ---- 3) 全局候选 ----
    cands = _pick_candidates(mse)
    if not cands:
        return None

    # ---- 4) 抽样验证：带横向信息，把错误候选筛掉 ----
    # 这一级只负责「从 5 个候选里挑出一个大方向」。它算出的 best_err 是
    # 160 行抽样、且每个候选按各自 overlap 取行，**与下一级的全量值不可比**，
    # 所以绝不允许把它带进最终判据（见下一级注释）。
    best_d = -1
    best_err = float("inf")
    for d in cands:
        overlap = h - d
        if overlap < 2:
            continue
        rows = _row_sample(overlap, SAMPLE_ROWS)
        for dd in (d - 1, d, d + 1):
            if dd < 1 or dd > d_max:
                continue
            e = _sampled_mad(prev_full, cur_full, dd, rows)
            if e < best_err:
                best_err, best_d = e, dd
    if best_d < 0:
        return None

    # ---- 5) 全量精修：在 ±1px 内用整段重叠区定案 ----
    # 三个候选必须用**同一个度量、同一段行**重新算一遍，再取最小：
    #   * 同一段行：重叠行多的候选天然更容易"像"，不统一行数会系统性偏向更小的 d；
    #   * 同一个度量：_sampled_mad 与 _full_mad 的抽样集合不同，数值不可比大小，
    #     拿全量值和抽样值比会让「真正更优的 dd」跨不过一个不可比的基准，
    #     也会让返回值退化成偏乐观的抽样残差。所以这里 best_err 从 inf 重新开始，
    #     输出的误差一定是全量值。
    # 候选按 best_d 优先排列：窗口若整段都是空白（三个位移 MAD 全 0），
    # 平局要判给上一级的胜者，而不是被 ±1 抢走。
    tried = [best_d] + [dd for dd in (best_d - 1, best_d + 1) if 1 <= dd <= d_max]
    tried = [dd for dd in tried if 1 <= dd <= d_max]
    if not tried:
        return None
    overlap = h - max(tried)
    if overlap < 2:
        return None
    rows_cmp = min(overlap, REFINE_MAX_ROWS)
    off = max(0, (overlap - rows_cmp) // 2)
    best_err = float("inf")
    best_dd = -1
    for dd in tried:
        e = _full_mad(prev_full, cur_full, dd, off, rows_cmp)
        if e < best_err:
            best_err, best_dd = e, dd
    if best_dd < 0 or not np.isfinite(best_err) or best_err > max_error:
        return None
    best_d = best_dd

    # ---- 6) 信息量守卫 ----
    # 重叠区如果整段是纯色（大片留白、纯色底），任何一个位移的 MAD 都是 0，
    # 「最小残差」只是在候选之间瞎挑 —— 这不是匹配成功，是没有信息。
    # 这种情况必须返回 None，让编排层缩小步长、换取更长的重叠区再来一次；
    # 直接给一个 err≈0 的假位置，会把错位内容当真理写进最终 PDF。
    ref_p = sig_p[best_d : best_d + rows_cmp].std()
    ref_c = sig_c[:rows_cmp].std()
    if min(ref_p, ref_c) < SIG_MIN_STD:
        return None

    new_start = h - best_d
    return new_start, int(best_d), float(best_err)


# ---------------------------------------------------------------- 流式拼接


class StreamStitcher:
    """流式拼接器：追加新内容行，满一页立即落盘。"""

    def __init__(
        self,
        width: int,
        page_h: int,
        out_dir: Path,
        quality: int = 85,
        max_frame_h: int = 4096,
    ) -> None:
        self.width = int(width)
        self.page_h = int(page_h)
        self.quality = int(quality)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        # 容量按「一页 + 最大单帧」预留：够容纳一次 append 之后立刻冲刷，
        # 所以正常流程永远不会触发 _grow，峰值内存与总页数无关。
        self._cap = self.page_h + int(max_frame_h)
        self._buf = np.empty((self._cap, self.width, 3), dtype=np.uint8)
        self._fill = 0
        self._pages: list[Path] = []
        self.total_rows = 0

    # ---------- 内部 ----------
    def _grow(self, need: int) -> None:
        """仅当单帧超过 max_frame_h 时扩容（正常流程不会走到这里）。"""
        new_cap = max(need, self._cap * 2)
        new_buf = np.empty((new_cap, self.width, 3), dtype=np.uint8)
        new_buf[: self._fill] = self._buf[: self._fill]
        self._buf = new_buf
        self._cap = new_cap

    def _flush(self, arr: np.ndarray) -> None:
        idx = len(self._pages)
        path = self.out_dir / f"page_{idx:05d}.jpg"
        Image.fromarray(arr).save(
            path,
            format="JPEG",
            quality=self.quality,
            subsampling=0,   # 4:4:4，关掉色度下采样，否则文字边缘发虚
            optimize=False,  # 500 页场景下 optimize 会让编码慢 2~3 倍
        )
        self._pages.append(path)

    def _fit(self, frame: np.ndarray) -> np.ndarray:
        """宽度不一致时缩放到目标宽度（正常流程不应触发）。"""
        img = Image.fromarray(frame)
        h = max(1, int(round(img.height * self.width / img.width)))
        return np.asarray(img.resize((self.width, h), Image.LANCZOS))

    def _drain(self) -> None:
        """把缓冲区里所有满页刷出去，剩余内容前移到开头。"""
        while self._fill >= self.page_h:
            self._flush(self._buf[: self.page_h].copy())
            remain = self._fill - self.page_h
            if remain > 0:
                # copy 避免重叠区域的自赋值被 numpy 当成别名跳过
                self._buf[:remain] = self._buf[self.page_h : self._fill].copy()
            self._fill = remain

    # ---------- 对外 ----------
    def append(self, frame: np.ndarray) -> None:
        """追加一段全新内容（调用方负责裁掉重叠部分）。

        空行段（shape[0] == 0，例如已到底时 cur[new_start:] 为空）安全返回，
        不改变任何状态 —— 编排层不需要在调用前特判。
        """
        if frame is None:
            return
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError(f"帧格式应为 (H, W, 3)，实际 {frame.shape}")
        h = int(frame.shape[0])
        if h <= 0:
            return
        if frame.shape[1] != self.width:
            frame = self._fit(frame)
            h = int(frame.shape[0])
            if h <= 0:
                return
        if self._fill + h > self._cap:
            self._grow(self._fill + h)

        self._buf[self._fill : self._fill + h] = frame
        self._fill += h
        self.total_rows += h
        self._drain()

    def finish(self) -> list[Path]:
        """把不满一页的残留内容也出一页，返回全部页 JPEG 路径。"""
        if self._fill > 0:
            self._flush(self._buf[: self._fill].copy())
            self._fill = 0
        return list(self._pages)

    def abort(self) -> list[Path]:
        """中断导出：把残留内容出一页后返回已生成的全部页。

        落盘路径与 finish() 完全一致（都只是「刷残留 + 返回列表」），
        之所以单列一个方法，是因为调用方意图不同：finish 表示正常采完，
        abort 表示用户取消/异常退出，此时**绝不能丢已采集的内容**，
        也不能因为残留不足一页就抛错。残留内容同样出一页 —— 半成品比缺内容好。
        """
        return self.finish()

    @property
    def pages(self) -> list[Path]:
        return list(self._pages)

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def pending_rows(self) -> int:
        return self._fill

    @property
    def buffer_bytes(self) -> int:
        """当前缓冲区占用的字节数（用于验证内存与总长解耦）。"""
        return int(self._buf.nbytes)
