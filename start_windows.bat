@echo off
chcp 65001 >nul
echo 正在启动 TableKit Local 本地表格工具箱...
echo 启动后请在浏览器打开 http://127.0.0.1:8765 （纯本地运行，数据绝不上传外网）
python tablekit.py webui --port 8765
pause
