#!/usr/bin/env bash
# LongShot2PDF 启动入口（macOS / Linux）
#
# Windows 用 start.bat；这里是同一个入口的 POSIX 版本。
# 逻辑保持一致：优先项目自带虚拟环境，其次 python3，最后 python。
set -u

cd "$(dirname "$0")" || exit 1

PY=""
if [ -x ".venv/bin/python3" ]; then
    PY=".venv/bin/python3"
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo
    echo "  [x] 没找到 Python 3。"
    echo "      macOS:  brew install python-tk"
    echo "      Ubuntu: sudo apt install python3 python3-tk python3-venv"
    echo
    exit 2
fi

exec "$PY" run.py "$@"
