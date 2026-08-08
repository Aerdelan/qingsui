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
        Write-Host "[1/4] 检测到损坏或失效的 .venv，正在重建..." -ForegroundColor Yellow
    } else {
        Write-Host "[1/4] 创建隔离运行环境..." -ForegroundColor Green
    }
    & $Python.Command @($Python.Args) -m venv --clear $Venv
    if ($LASTEXITCODE -ne 0) { throw "虚拟环境创建失败，请确认 Python 3.11 已正确安装。" }
}
Write-Host "[2/4] 检查项目依赖..." -ForegroundColor Green
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip 升级失败，请检查网络或 Python 环境。" }
& $VenvPython -m pip install --disable-pip-version-check -e "${Root}[ml,archives]"
if ($LASTEXITCODE -ne 0) { throw "项目依赖安装失败，请修复上面的 pip 错误后重新运行 start.ps1。" }

# 检查 Ollama（翻译引擎）
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    Write-Host "[3/4] 未检测到 Ollama（本地翻译引擎）..." -ForegroundColor Yellow
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        $answer = Read-Host "是否使用 winget 自动安装 Ollama？[Y/n]"
        if (-not $answer -or $answer.ToLowerInvariant() -eq "y") {
            winget install --id Ollama.Ollama --exact --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { throw "Ollama 安装失败，请手动安装：winget install --id Ollama.Ollama --exact" }
            # 刷新 PATH 以识别新安装的 ollama
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            $ollama = Get-Command ollama -ErrorAction SilentlyContinue
            if (-not $ollama) { throw "Ollama 已安装但当前终端未识别，请重新打开终端后运行 start.ps1。" }
        } else {
            Write-Host "跳过 Ollama 安装。翻译功能将不可用，可稍后手动安装。" -ForegroundColor Yellow
        }
    } else {
        Write-Host "未找到 winget，请手动安装 Ollama：https://ollama.com/download" -ForegroundColor Yellow
    }
} else {
    Write-Host "[3/4] Ollama 已就绪。" -ForegroundColor Green
}

$arguments = @("-m", "aoba_translator", "serve")
if ($SkipModelDownload) { $arguments += "--skip-model-download" }
if ($NoBrowser) { $arguments += "--no-browser" }

Write-Host "[4/4] 启动青穗翻译台..." -ForegroundColor Green
& $VenvPython @arguments
