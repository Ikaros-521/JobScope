param(
    [int]$Port = 8765,
    [switch]$Install,
    [switch]$Browser,
    [string]$LocalEmbeddingModel = ''
)

$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$pythonPath = Join-Path $projectDir '.venv\Scripts\python.exe'

if ($Install) {
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        python -m venv (Join-Path $projectDir '.venv')
        if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败。' }
    }
    & $pythonPath -m pip install -r (Join-Path $projectDir 'backend\requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败。' }
    if ($Browser) {
        & $pythonPath -m pip install -r (Join-Path $projectDir 'requirements-browser.txt')
        if ($LASTEXITCODE -ne 0) { throw '浏览器依赖安装失败。' }
    }
    if ($LocalEmbeddingModel) {
        & $pythonPath -m pip install -r (Join-Path $projectDir 'requirements-research.txt')
        if ($LASTEXITCODE -ne 0) { throw '本地语义模型依赖安装失败。' }
    }
    Push-Location (Join-Path $projectDir 'frontend')
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
    } finally { Pop-Location }
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '请先执行 .\start.ps1 -Install，或手工建立 .venv。'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectDir 'frontend\dist\index.html'))) {
    throw '缺少界面构建，请执行 .\start.ps1 -Install。'
}
Write-Host "知根 JobScope：http://127.0.0.1:$Port"
Write-Host '按 Ctrl+C 停止本地服务。'
if ($LocalEmbeddingModel) {
    $env:JOBSCOPE_EMBEDDING_LOCAL_MODEL = $LocalEmbeddingModel
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    Write-Host "本地混合检索模型：$LocalEmbeddingModel（只读取已缓存文件）"
}
Push-Location (Join-Path $projectDir 'backend')
try {
    & $pythonPath -m uvicorn jobscope.main:app --host 127.0.0.1 --port $Port
} finally { Pop-Location }
