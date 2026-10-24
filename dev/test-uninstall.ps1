# WorkBuddy 卸载流程自动化测试
# 参数：-KeepData 走「保留数据」卸载；默认走「全部删除」卸载。
#       -IsolationSmokeOnly 故障注入，只验证 finally 完整恢复全局安装状态。
# 前置：已完成安装（dev\test-installer.ps1）。
param([switch]$KeepData, [switch]$IsolationSmokeOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ui-helpers.ps1')

$installDir = 'D:\dev\debug\wb-install-e2e\WorkBuddyManager'
$otherInstallDir = 'D:\ai-project\_wb_other_install'
$uninstallExe = Join-Path $installDir 'Uninstall.exe'
$desktopLink = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'WorkBuddy Manager.lnk'
$menuDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\WorkBuddy Manager'
$uninstallRegNative = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\WorkBuddyManager'
$regKey = 'HKCU:\' + $uninstallRegNative
$startupRunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$script:failed = $false

function Get-WorkBuddyRunSnapshot {
    $snapshot = @()
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey(
        'Software\Microsoft\Windows\CurrentVersion\Run', $false)
    if ($key) {
        try {
            foreach ($name in $key.GetValueNames()) {
                if ($name -like 'WorkBuddyManager*') {
                    $snapshot += [pscustomobject]@{
                        Name = $name
                        Value = $key.GetValue($name, $null, 'DoNotExpandEnvironmentNames')
                        Kind = $key.GetValueKind($name)
                    }
                }
            }
        } finally { $key.Dispose() }
    }
    return $snapshot
}
function Restore-WorkBuddyRunSnapshot($snapshot) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey(
        'Software\Microsoft\Windows\CurrentVersion\Run', $true)
    if (-not $key) {
        $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey(
            'Software\Microsoft\Windows\CurrentVersion\Run')
    }
    try {
        foreach ($name in @($key.GetValueNames())) {
            if ($name -like 'WorkBuddyManager*') { $key.DeleteValue($name, $false) }
        }
        foreach ($item in $snapshot) {
            $key.SetValue($item.Name, $item.Value, $item.Kind)
        }
    } finally { $key.Dispose() }
}
function Get-InstallRunName([string]$dir) {
    $path = [IO.Path]::GetFullPath($dir).TrimEnd('\').ToUpperInvariant()
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($path))
        return 'WorkBuddyManager.' + ([BitConverter]::ToString($hash, 0, 8)).Replace('-', '')
    } finally { $sha.Dispose() }
}
function Get-RegistrySnapshot([string]$psPath, [string]$nativePath) {
    $values = @()
    $exists = Test-Path -LiteralPath $psPath
    if ($exists) {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($nativePath, $false)
        if ($key) {
            try {
                foreach ($name in $key.GetValueNames()) {
                    $values += [pscustomobject]@{
                        Name = $name
                        Value = $key.GetValue($name, $null, 'DoNotExpandEnvironmentNames')
                        Kind = $key.GetValueKind($name)
                    }
                }
            } finally { $key.Dispose() }
        }
    }
    return [pscustomobject]@{ Exists = $exists; NativePath = $nativePath; Values = $values }
}
function Restore-RegistrySnapshot($snapshot) {
    if (Test-Path -LiteralPath ('HKCU:\' + $snapshot.NativePath)) {
        [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($snapshot.NativePath, $false)
    }
    if (-not $snapshot.Exists) { return }
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($snapshot.NativePath)
    if (-not $key) { throw ('无法恢复注册表项：' + $snapshot.NativePath) }
    try {
        foreach ($item in $snapshot.Values) {
            $key.SetValue($item.Name, $item.Value, $item.Kind)
        }
    } finally { $key.Dispose() }
}
function Get-FileSnapshot([string]$path) {
    $exists = Test-Path -LiteralPath $path -PathType Leaf
    return [pscustomobject]@{
        Path = $path
        Exists = $exists
        Bytes = if ($exists) { [IO.File]::ReadAllBytes($path) } else { $null }
    }
}
function Restore-FileSnapshot($snapshot) {
    $parent = Split-Path -Parent $snapshot.Path
    if ($snapshot.Exists) {
        [IO.Directory]::CreateDirectory($parent) | Out-Null
        [IO.File]::WriteAllBytes($snapshot.Path, $snapshot.Bytes)
    } elseif (Test-Path -LiteralPath $snapshot.Path) {
        [IO.File]::Delete($snapshot.Path)
    }
}
function Get-DirectorySnapshot([string]$root) {
    $dirs = @()
    $files = @()
    $exists = Test-Path -LiteralPath $root -PathType Container
    if ($exists) {
        $fullRoot = [IO.Path]::GetFullPath($root).TrimEnd('\')
        foreach ($item in @(Get-ChildItem -LiteralPath $root -Force -Recurse -Directory)) {
            $dirs += $item.FullName.Substring($fullRoot.Length).TrimStart('\')
        }
        foreach ($item in @(Get-ChildItem -LiteralPath $root -Force -Recurse -File)) {
            $relative = $item.FullName.Substring($fullRoot.Length).TrimStart('\')
            $files += [pscustomobject]@{
                Relative = $relative
                Bytes = [IO.File]::ReadAllBytes($item.FullName)
            }
        }
    }
    return [pscustomobject]@{ Root = $root; Exists = $exists; Dirs = $dirs; Files = $files }
}
function Restore-DirectorySnapshot($snapshot) {
    if (Test-Path -LiteralPath $snapshot.Root) {
        [IO.Directory]::Delete($snapshot.Root, $true)
    }
    if (-not $snapshot.Exists) { return }
    [IO.Directory]::CreateDirectory($snapshot.Root) | Out-Null
    foreach ($relative in $snapshot.Dirs) {
        [IO.Directory]::CreateDirectory((Join-Path $snapshot.Root $relative)) | Out-Null
    }
    foreach ($file in $snapshot.Files) {
        $path = Join-Path $snapshot.Root $file.Relative
        [IO.Directory]::CreateDirectory((Split-Path -Parent $path)) | Out-Null
        [IO.File]::WriteAllBytes($path, $file.Bytes)
    }
}
function Stop-LaunchersAt([string]$dir) {
    $expected = [IO.Path]::GetFullPath((Join-Path $dir 'WorkBuddyManager.exe'))
    Get-Process -Name 'WorkBuddyManager' -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            if ([string]::Equals([IO.Path]::GetFullPath($_.Path), $expected,
                [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $_.Id -Force
            }
        } catch { }
    }
}

function Check($ok, $label) {
    if ($ok) { Write-Host ('OK: ' + $label) } else { Write-Host ('FAIL: ' + $label); $script:failed = $true }
}

$runSnapshot = @(Get-WorkBuddyRunSnapshot)
$uninstallSnapshot = Get-RegistrySnapshot $regKey $uninstallRegNative
$desktopSnapshot = Get-FileSnapshot $desktopLink
$menuSnapshot = Get-DirectorySnapshot $menuDir
$otherProcess = $null
$targetRunName = Get-InstallRunName $installDir
$otherRunName = Get-InstallRunName $otherInstallDir
$otherRunValue = '"' + (Join-Path $otherInstallDir 'WorkBuddyManager.exe') + '" --silent'

function Restore-UninstallTestState {
    $restoreErrors = @()
    try {
        if ($proc -and -not $proc.HasExited) { $proc.Kill() }
        if ($otherProcess -and -not $otherProcess.HasExited) {
            $otherProcess.Kill()
            $otherProcess.WaitForExit()
        }
    } catch { $restoreErrors += ('Processes: ' + $_.Exception.Message) }
    try { Restore-WorkBuddyRunSnapshot $runSnapshot }
    catch { $restoreErrors += ('Run: ' + $_.Exception.Message) }
    try { Restore-RegistrySnapshot $uninstallSnapshot }
    catch { $restoreErrors += ('Uninstall: ' + $_.Exception.Message) }
    try { Restore-FileSnapshot $desktopSnapshot }
    catch { $restoreErrors += ('Desktop: ' + $_.Exception.Message) }
    try { Restore-DirectorySnapshot $menuSnapshot }
    catch { $restoreErrors += ('Menu: ' + $_.Exception.Message) }
    try {
        if (Test-Path -LiteralPath $otherInstallDir) {
            [IO.Directory]::Delete($otherInstallDir, $true)
        }
    } catch { $restoreErrors += ('OtherInstall: ' + $_.Exception.Message) }
    if ($restoreErrors.Count -gt 0) {
        throw ('卸载测试状态恢复失败：' + ($restoreErrors -join '; '))
    }
}

try {
if ($IsolationSmokeOnly) {
    New-ItemProperty -LiteralPath $startupRunKey -Name 'WorkBuddyManager.ISOLATION-SMOKE' `
        -Value '"D:\isolated\WorkBuddyManager.exe" --silent' -PropertyType String -Force | Out-Null
    New-Item -Path $regKey -Force | Out-Null
    New-ItemProperty -LiteralPath $regKey -Name 'InstallLocation' `
        -Value 'D:\isolated' -PropertyType String -Force | Out-Null
    [IO.Directory]::CreateDirectory((Split-Path -Parent $desktopLink)) | Out-Null
    [IO.File]::WriteAllText($desktopLink, 'isolation-smoke')
    [IO.Directory]::CreateDirectory($menuDir) | Out-Null
    [IO.File]::WriteAllText((Join-Path $menuDir 'isolation-smoke.txt'), 'isolation-smoke')
    throw [InvalidOperationException]::new('intentional isolation smoke failure')
}
if (-not (Test-Path $uninstallExe)) { Write-Error "未找到卸载程序：$uninstallExe"; exit 1 }

# B 的 Run 值应删除；另一个真实目录 A 的 Run 值必须原样保留。
New-ItemProperty -LiteralPath $startupRunKey -Name $targetRunName `
    -Value ('"' + (Join-Path $installDir 'WorkBuddyManager.exe') + '" --silent') `
    -PropertyType String -Force | Out-Null
New-ItemProperty -LiteralPath $startupRunKey -Name $otherRunName -Value $otherRunValue `
    -PropertyType String -Force | Out-Null

# 构造并启动另一安装目录的最小控制进程，验证卸载 B 不会误杀 A。
New-Item -ItemType Directory (Join-Path $otherInstallDir 'server') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $installDir 'WorkBuddyManager.exe') `
    -Destination (Join-Path $otherInstallDir 'WorkBuddyManager.exe') -Force
[IO.File]::WriteAllText((Join-Path $otherInstallDir 'server\main.py'),
    '# other install marker', [Text.UTF8Encoding]::new($false))
$otherProcess = Start-Process -FilePath (Join-Path $otherInstallDir 'WorkBuddyManager.exe') -PassThru
Start-Sleep -Seconds 2

if ($KeepData) {
    # 先准备出「可保留的数据」：prepare 会生成 .env 与 data/（幂等），否则目录里没有数据可验证
    $prepare = Join-Path $installDir 'windows\prepare.ps1'
    & powershell -NoProfile -ExecutionPolicy Bypass -File $prepare 2>&1 | Out-Null
    New-Item -ItemType Directory (Join-Path $installDir 'data\probe') -Force | Out-Null
    Set-Content (Join-Path $installDir 'data\probe\keep.txt') 'keep-test'
}

# 卸载前：.env 端口改为测试端口（17864），避免卸载停服时碰到机器上 7864 的其他实例
$envFile = Join-Path $installDir '.env'
if (Test-Path $envFile) {
    $lines = Get-Content $envFile
    $lines = $lines | ForEach-Object { if ($_ -match '^WB_MANAGER_PORT=') { 'WB_MANAGER_PORT=17864' } else { $_ } }
    Set-Content $envFile $lines -Encoding UTF8
}
# 让服务处于运行状态（覆盖「卸载时自动停服」路径）
& (Join-Path $installDir 'windows\service-tools.ps1') start 2>&1 | Out-Null
Start-Sleep -Seconds 3
$wasRunning = $false
try {
    $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:17864/api/healthz' -UseBasicParsing -TimeoutSec 2
    $wasRunning = ($resp.StatusCode -eq 200)
} catch { }Write-Host ('卸载前服务运行中: ' + $wasRunning)

Stop-LaunchersAt $installDir
$proc = Start-Process $uninstallExe -PassThru

# 等卸载窗口（确认页）
$win = [IntPtr]::Zero
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    $proc.Refresh()
    if ($proc.MainWindowHandle -ne 0) { $win = $proc.MainWindowHandle; break }
    Start-Sleep -Milliseconds 500
}
if ($win -eq [IntPtr]::Zero) { Write-Host 'FAIL: 卸载窗口未出现'; $proc.Kill(); exit 1 }
Write-Host 'OK: 卸载窗口出现'

# 勾选/不勾选「保留数据」
$keepBox = [WbWin]::FindChildByPrefix($win, 'BUTTON', '保留数据')
if ($keepBox -ne [IntPtr]::Zero -and $KeepData) {
    [WbWin]::Click($keepBox)  # 勾选
    Write-Host 'OK: 已勾选「保留数据」'
}

# 点「卸载(U)」（按钮文本带加速键尾巴，用前缀匹配）
$uninstallBtn = [WbWin]::FindChildByPrefix($win, 'BUTTON', '卸载')
if ($uninstallBtn -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未找到「卸载」按钮'; $proc.Kill(); exit 1 }
[WbWin]::Click($uninstallBtn)
Write-Host 'OK: 已点击「卸载」，等待确认框…'

# 确认 MessageBox → 点「是」
$mb = [IntPtr]::Zero
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
    $mb = [WbTop]::FindDialog($proc.Id)
    if ($mb -ne [IntPtr]::Zero) { break }
}
if ($mb -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未出现确认框'; $proc.Kill(); exit 1 }
$yesBtn = [WbWin]::FindChildByPrefix($mb, 'BUTTON', '是')
[WbWin]::Click($yesBtn)
Write-Host 'OK: 已确认卸载，等待清理完成…'

# 等完成页（标题含「卸载完成」，清理 5000+ 文件需要时间）
$doneShown = $false
$deadline = (Get-Date).AddSeconds(180)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $proc.Refresh()
    if ($proc.HasExited) { break }
    if ([WbWin]::FindChildByPrefix($win, 'STATIC', '卸载完成') -ne [IntPtr]::Zero) { $doneShown = $true; break }
}
if (-not $doneShown) { Write-Host 'FAIL: 等待卸载完成页超时'; if (-not $proc.HasExited) { $proc.Kill() }; exit 1 }
Write-Host 'OK: 卸载完成页已出现'

# 点「完成(F)」→ 自删除
$finishBtn = [WbWin]::FindChildByPrefix($win, 'BUTTON', '完成')
[WbWin]::Click($finishBtn)
Start-Sleep -Seconds 4
$proc.Refresh()
Write-Host ('卸载进程已退出: ' + $proc.HasExited)

# 自删除延迟 1–2 秒 + 删除 5000 文件的目录需要时间，轮询目录消失
$dirGone = $false
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (-not (Test-Path $installDir)) { $dirGone = $true; break }
}
Write-Host ('安装目录已消失: ' + $dirGone)

# ── 核验 ────────────────────────────────────────────────
if ($KeepData) {
    # 保留模式：数据在、程序没了
    Check (Test-Path (Join-Path $installDir '.env')) '保留数据：.env 仍在'
    Check (Test-Path (Join-Path $installDir 'data')) '保留数据：data 目录仍在'
    Check (-not (Test-Path (Join-Path $installDir 'WorkBuddyManager.exe'))) '保留数据：主程序已删除'
    Check (-not (Test-Path (Join-Path $installDir 'server'))) '保留数据：程序目录已删除'
} else {
    Check $dirGone '全删模式：安装目录已完全删除'
}
Check (-not (Test-Path $desktopLink)) '桌面快捷方式已删除'
Check (-not (Test-Path $menuDir)) '开始菜单目录已删除'
Check (-not (Test-Path $regKey)) '注册表卸载项已删除'
$startupLeft = (Get-ItemProperty -LiteralPath $startupRunKey -Name $targetRunName `
    -ErrorAction SilentlyContinue).$targetRunName
Check ([string]::IsNullOrWhiteSpace($startupLeft)) '卸载 B 只删除 B 的自启 Run 值'
$otherRunLeft = (Get-ItemProperty -LiteralPath $startupRunKey -Name $otherRunName `
    -ErrorAction SilentlyContinue).$otherRunName
Check ($otherRunLeft -eq $otherRunValue) '卸载 B 保留 A 的自启 Run 值'
$otherProcess.Refresh()
Check (-not $otherProcess.HasExited) '卸载 B 不结束 A 的控制进程'
Check (-not (Test-Path (Join-Path $installDir 'Uninstall.exe.old'))) '无 .old 残留'

# 收尾：清掉测试目录残留（保留数据模式的目录）
if (Test-Path $installDir) { Remove-Item $installDir -Recurse -Force }
if ($failed) { Write-Host 'RESULT: FAIL'; exit 1 }
Write-Host 'RESULT: PASS'
}
catch {
    if (-not $IsolationSmokeOnly) { throw }
}
finally {
    Restore-UninstallTestState
}
if ($IsolationSmokeOnly) {
    Write-Host 'RESULT: PASS (expected exception restored)'
}
