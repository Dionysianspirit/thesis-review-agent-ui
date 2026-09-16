$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Resolve-GuiPython {
    $candidates = @()
    if ($env:THESIS_REVIEW_PYTHON) {
        $candidates += $env:THESIS_REVIEW_PYTHON
    }
    $candidates += (Join-Path $Root ".venv\Scripts\python.exe")
    $sibling = Join-Path (Split-Path $Root) "thesis-review-agent\.venv\Scripts\python.exe"
    $candidates += $sibling
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            return $path
        }
    }
    return $null
}

$Python = Resolve-GuiPython
if (-not $Python) {
    throw "先创建 .venv 并安装 requirements.txt，或设置 THESIS_REVIEW_PYTHON 指向可用的 python.exe"
}

$env:PYTHONPATH = Join-Path $Root "python"
if (-not $env:THESIS_REVIEW_ROOT) {
    $env:THESIS_REVIEW_ROOT = $Root
}
Write-Host "正在启动论文审稿助手（首次打开窗口可能需要几秒）..."
& $Python -m thesis_review.gui.app @args
exit $LASTEXITCODE
