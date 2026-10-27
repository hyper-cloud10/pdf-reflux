# 다운로드 폴더에 새 PDF가 생기면 WSL의 classify.py를 호출한다.
# WSL2는 /mnt/c 변경 이벤트를 inotify로 받지 못하므로 감시는 윈도우 쪽(FileSystemWatcher)에서 한다.
#
# 실행: powershell.exe -NoProfile -ExecutionPolicy Bypass -File watch_downloads.ps1
# 종료: Ctrl+C

param(
    [string]$WslDistro = "Ubuntu",
    # 비워두면 이 스크립트가 있는 폴더(프로젝트 루트)를 WSL 경로로 바꿔서 쓴다.
    [string]$ProjectDir = "",
    [string]$LogPath = "$env:LOCALAPPDATA\paper-archive\watch.log"
)

$ErrorActionPreference = "Stop"
if (-not $ProjectDir) {
    $ProjectDir = (wsl.exe -d $WslDistro -- wslpath -u "$PSScriptRoot").Trim()
}
$downloads = Join-Path $env:USERPROFILE "Downloads"
$logDir = Split-Path $LogPath -Parent
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Host $line
}

# 브라우저가 다운로드를 끝낼 때까지 기다린다 (파일을 독점 열 수 있으면 쓰기가 끝난 것으로 본다).
function Wait-FileReady([string]$path) {
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $fs = [System.IO.File]::Open($path, 'Open', 'Read', 'None')
            $fs.Close()
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

# 윈도우 경로 -> WSL 경로 (wslpath 사용, 공백/따옴표 문제 회피를 위해 base64로 전달)
function Invoke-Classify([string]$winPath) {
    $wslPath = (wsl.exe -d $WslDistro -- wslpath -u "$winPath").Trim()
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($wslPath))
    $cmd = "p=`$(echo $b64 | base64 -d); cd '$ProjectDir' && python3 classify.py `"`$p`""
    Write-Log "classify 시작: $wslPath"
    $out = wsl.exe -d $WslDistro -- bash -c $cmd 2>&1
    $code = $LASTEXITCODE
    $out | ForEach-Object { Add-Content -Path $LogPath -Value ("    " + $_) -Encoding UTF8 }
    Write-Log "classify 종료 코드 ${code}: $wslPath"
}

# 같은 파일에 대해 Created/Renamed가 연달아 올 수 있으므로 짧은 시간 내 중복은 무시한다.
$script:recent = @{}
function Handle-Pdf([string]$path) {
    if (-not $path.EndsWith(".pdf", [StringComparison]::OrdinalIgnoreCase)) { return }
    $now = Get-Date
    if ($script:recent.ContainsKey($path) -and ($now - $script:recent[$path]).TotalSeconds -lt 10) { return }
    $script:recent[$path] = $now

    if (-not (Wait-FileReady $path)) {
        Write-Log "파일이 계속 잠겨 있어 건너뜀: $path"
        return
    }
    try {
        Invoke-Classify $path
    } catch {
        Write-Log "오류: $($_.Exception.Message)"
    }
}

$watcher = New-Object System.IO.FileSystemWatcher $downloads
$watcher.IncludeSubdirectories = $false
$watcher.EnableRaisingEvents = $true

# Chrome/Edge는 .crdownload로 받다가 최종 이름(.pdf)으로 바꾸므로 Renamed도 본다.
$action = {
    $path = $Event.SourceEventArgs.FullPath
    Handle-Pdf $path
}
Register-ObjectEvent $watcher Created -Action $action | Out-Null
Register-ObjectEvent $watcher Renamed -Action $action | Out-Null

Write-Log "감시 시작: $downloads (Ctrl+C로 종료)"
try {
    while ($true) { Wait-Event -Timeout 5 | Out-Null }
} finally {
    Get-EventSubscriber | Unregister-Event
    $watcher.Dispose()
    Write-Log "감시 종료"
}
