param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 优先使用项目虚拟环境（依赖完整）；不存在时才回退到系统 Python
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $PythonSource = $VenvPython
} else {
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $Python) { throw "未检测到 Python，请运行 start.ps1 完成环境初始化。" }
    $PythonSource = $Python.Source
    Write-Host "提示：未找到 .venv，当前使用系统 Python，若提示缺少依赖请运行 start.ps1。" -ForegroundColor Yellow
}

$arguments = @("-m", "aoba_translator", "serve", "--skip-model-download")
if ($NoBrowser) { $arguments += "--no-browser" }
$env:PYTHONPATH = Join-Path $Root "src"
& $PythonSource @arguments
