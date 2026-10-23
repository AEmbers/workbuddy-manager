# WorkBuddy Manager —— Python 解释器解析（供其他脚本 dot-source 使用）
# 预置运行时（.runtime\python，随安装包分发）优先；uv 建的虚拟环境（.venv）兜底。

# The bundled runtime must never import user-site packages. Windows user-site
# directories are searched before the interpreter's own site-packages, so an
# old typing_extensions can break the application before dependency repair.
$env:PYTHONNOUSERSITE = '1'

function Get-WbPython {
    param([Parameter(Mandatory = $true)][string]$Root)
    foreach ($rel in @('.runtime\python\python.exe', '.venv\Scripts\python.exe')) {
        $path = Join-Path $Root $rel
        if (Test-Path $path) { return $path }
    }
    return $null
}
