param(
    [switch]$SkipModelDownload,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root ".venv"
$Python = $null

function Find-Python {
    $candidates = @(
        @{ Command = "py"; Args = @("-3.11") },
        @{ Command = "python3.11"; Args = @() },
        @{ Command = "python"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        try {
            $version = & $candidate.Command @($candidate.Args) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
            if ($version -in @("3.10", "3.11", "3.12")) {
                return @{ Command = $candidate.Command; Args = $candidate.Args }
            }
        } catch { }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Host "未检测到 Python 3.10-3.12。" -ForegroundColor Yellow
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "请先安装 Python 3.11，然后重新运行 start.ps1。"
    }
    $answer = Read-Host "是否使用 winget 安装 Python 3.11？[Y/n]"
    if ($answer -and $answer.ToLowerInvariant() -ne "y") {
        throw "已取消安装。"
    }
    winget install --id Python.Python.3.11 --exact --accept-package-agreements --accept-source-agreements
    $Python = Find-Python
    if (-not $Python) { throw "Python 安装后仍不可用，请重新打开终端。" }
}

$VenvPython = Join-Path $Venv "Scripts\python.exe"
$venvHealthy = $false
if (Test-Path $VenvPython) {
    $venvVersion = (& $VenvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2> $null).Trim()
    $venvHealthy = $LASTEXITCODE -eq 0 -and $venvVersion -in @("3.10", "3.11", "3.12")
}

if (-not $venvHealthy) {
    if (Test-Path $Venv) {
        Write-Host "[1/3] 检测到损坏或失效的 .venv，正在重建..." -ForegroundColor Yellow
    } else {
        Write-Host "[1/3] 创建隔离运行环境..." -ForegroundColor Green
    }
    & $Python.Command @($Python.Args) -m venv --clear $Venv
    if ($LASTEXITCODE -ne 0) { throw "虚拟环境创建失败，请确认 Python 3.11 已正确安装。" }
}
Write-Host "[2/3] 检查项目依赖..." -ForegroundColor Green
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip 升级失败，请检查网络或 Python 环境。" }
& $VenvPython -m pip install --disable-pip-version-check -e "${Root}[ml,archives]"
if ($LASTEXITCODE -ne 0) { throw "项目依赖安装失败，请修复上面的 pip 错误后重新运行 start.ps1。" }

$arguments = @("-m", "aoba_translator", "serve")
if ($SkipModelDownload) { $arguments += "--skip-model-download" }
if ($NoBrowser) { $arguments += "--no-browser" }

Write-Host "[3/3] 启动青穗翻译台..." -ForegroundColor Green
& $VenvPython @arguments
