@echo off
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw.exe "%~dp0danmaku_overlay.py"
) else (
  echo [!] PATH 中找不到 pythonw，请先安装 Python 并加入 PATH
  pause
)
