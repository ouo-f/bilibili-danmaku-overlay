# Recreates desktop shortcuts for the danmaku overlay tools.
# Run create_desktop_shortcuts.bat after moving this folder.
$dir = $PSScriptRoot
$desktop = [Environment]::GetFolderPath('Desktop')
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) {
    foreach ($cand in @("$env:LOCALAPPDATA\anaconda3\pythonw.exe",
                        "$env:ProgramFiles\anaconda3\pythonw.exe",
                        "${env:ProgramFiles(x86)}\anaconda3\pythonw.exe")) {
        if (Test-Path $cand) { $py = $cand; break }
    }
}
if (-not $py) { Write-Output 'pythonw.exe not found; please add Python to PATH'; exit 1 }
$edge = "$env:ProgramFiles (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }

$ws = New-Object -ComObject WScript.Shell

$s1 = $ws.CreateShortcut((Join-Path $desktop '一键直播.lnk'))
$s1.TargetPath = $py
$s1.Arguments = '"' + (Join-Path $dir '一键直播.py') + '"'
$s1.WorkingDirectory = $dir
$s1.IconLocation = "$edge,0"
$s1.Description = 'B站画中画+弹幕+音量 一键启动'
$s1.Save()

$s2 = $ws.CreateShortcut((Join-Path $desktop '启动弹幕层.lnk'))
$s2.TargetPath = $py
$s2.Arguments = '"' + (Join-Path $dir 'danmaku_overlay.py') + '"'
$s2.WorkingDirectory = $dir
$s2.IconLocation = "$edge,0"
$s2.Description = 'B站直播弹幕悬浮层（单独启动）'
$s2.Save()

Write-Output 'shortcuts created on desktop'
