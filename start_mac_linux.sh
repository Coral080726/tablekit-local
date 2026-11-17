#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
echo "正在启动 TableKit Local 本地表格工具箱 -> http://127.0.0.1:8765"
python3 tablekit.py webui --port 8765
