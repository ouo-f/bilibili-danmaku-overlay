@echo off
rem GBK编码：cmd按GBK解析中文文件名，勿改存UTF-8
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw.exe "%~dp0一键直播.py"
) else (
  echo [!] PATH 中找不到 pythonw，请先安装 Python 并加入 PATH
  pause
)
