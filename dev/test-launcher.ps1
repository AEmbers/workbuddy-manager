# WorkBuddy 启动器自动化测试：控制窗口启动/关闭项目 + 状态 + 配置
# 定位全部走 Win32（EnumChildWindows），不依赖 UI 线程配合，免疫浏览器启动/弹窗导致的 UIA 挂起。
# 前置：已完成安装（dev\test-installer.ps1 PASS）。测试用独立端口 17864，不碰常见默认端口。
param([switch]$AutostartSmokeOnly)

$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public class WbWin
{
    public delegate bool EnumProc(IntPtr hwnd, IntPtr lp);

    [DllImport("user32.dll")]
    public static extern bool EnumChildWindows(IntPtr parent, EnumProc cb, IntPtr lp);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr h, StringBuilder sb, int max);
    [DllImport("user32.dll")]
    public static extern int GetClassName(IntPtr h, StringBuilder sb, int max);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, string lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, StringBuilder lParam);
    [DllImport("user32.dll")]
    public static extern IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    public static extern uint GetLongPathName(string shortPath, StringBuilder longPath, uint length);

    public struct RECT { public int Left, Top, Right, Bottom; }
    public const uint WM_SETTEXT = 0x000C;
    public const uint WM_GETTEXT = 0x000D;
    public const uint BM_CLICK = 0x00F5;
    public const uint BM_GETCHECK = 0x00F0;

    // EDIT 控件的文本读取需要目标 UI 线程处理 WM_GETTEXT（忙时长时间阻塞），
    // 一律跳过（输入框改按序号定位）；BUTTON/STATIC 的标题走 win32k 缓存，安全。
    public static List<object[]> Children(IntPtr parent)
    {
        var list = new List<object[]>();
        EnumProc cb = delegate(IntPtr hwnd, IntPtr lp)
        {
            var cls = new StringBuilder(256);
            GetClassName(hwnd, cls, 256);
            string clsName = cls.ToString();
            string text = string.Empty;
            if (clsName.IndexOf("EDIT", StringComparison.OrdinalIgnoreCase) < 0)
            {
                var txt = new StringBuilder(256);
                GetWindowText(hwnd, txt, 256);
                text = txt.ToString();
            }
            list.Add(new object[] { hwnd, clsName, text });
            return true;
        };
        EnumChildWindows(parent, cb, IntPtr.Zero);
        return list;
    }

    public static IntPtr FindChild(IntPtr parent, string classPart, string textExact)
    {
        foreach (object[] item in Children(parent))
        {
            IntPtr hwnd = (IntPtr)item[0];
            string cls = (string)item[1];
            string txt = (string)item[2];
            if (cls.IndexOf(classPart, StringComparison.OrdinalIgnoreCase) < 0) { continue; }
            if (textExact != null && txt != textExact) { continue; }
            return hwnd;
        }
        return IntPtr.Zero;
    }

    public static void Click(IntPtr hwnd) { SendMessage(hwnd, BM_CLICK, IntPtr.Zero, IntPtr.Zero); }
    public static int GetCheck(IntPtr hwnd) { return SendMessage(hwnd, BM_GETCHECK, IntPtr.Zero, IntPtr.Zero).ToInt32(); }
    public static void SetText(IntPtr hwnd, string text) { SendMessage(hwnd, WM_SETTEXT, IntPtr.Zero, text); }
    public static string LongPath(string path) {
        var buffer = new StringBuilder(4096);
        uint length = GetLongPathName(path, buffer, (uint)buffer.Capacity);
        return length > 0 ? buffer.ToString() : path;
    }
}

public class WbTop
{
    public delegate bool EnumProc(IntPtr hwnd, IntPtr lp);
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumProc cb, IntPtr lp);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")]
    public static extern int GetClassName(IntPtr h, StringBuilder sb, int max);

    // 找某进程的模态对话框（#32770）
    public static IntPtr FindDialog(int pid)
    {
        IntPtr found = IntPtr.Zero;
        EnumProc cb = delegate(IntPtr h, IntPtr lp)
        {
            uint wpid;
            GetWindowThreadProcessId(h, out wpid);
            if (wpid == (uint)pid)
            {
                var cls = new StringBuilder(64);
                GetClassName(h, cls, 64);
                if (cls.ToString() == "#32770") { found = h; return false; }
            }
            return true;
        };
        EnumWindows(cb, IntPtr.Zero);
        return found;
    }
}
'@

$repoRoot = Split-Path -Parent $PSScriptRoot

if ($AutostartSmokeOnly) {
    # issue #269 的隔离冒烟：临时安装目录 + 每安装目录独立的单实例名，
    # 不碰正在使用的 WorkBuddy Manager。测试结束恢复原 HKCU Run 值。
    $script:failed = $false
    function Check($ok, $label) {
        if ($ok) { Write-Host ('OK: ' + $label) }
        else { Write-Host ('FAIL: ' + $label); $script:failed = $true }
    }

    $src = Join-Path $repoRoot 'windows\launcher\src'
    $appSettings = Get-Content (Join-Path $src 'AppSettings.cs') -Raw
    $program = Get-Content (Join-Path $src 'Program.cs') -Raw
    $mainForm = Get-Content (Join-Path $src 'MainForm.cs') -Raw
    $installer = Get-Content (Join-Path $src 'InstallerForm.cs') -Raw
    $maintenance = Get-Content (Join-Path $src 'Maintenance.cs') -Raw

    Check ($appSettings -match 'Software\\Microsoft\\Windows\\CurrentVersion\\Run' -and
        $appSettings -match '--silent' -and $appSettings -match 'Registry\.CurrentUser') `
        '自启使用 HKCU Run 并固定 --silent'
    Check ($program -match 'LauncherPaths\.InstallIdentity' -and $program -match 'RegisterWaitForSingleObject' -and
        $program -match 'wakeEvent\.Set') '第二实例通过命名事件唤醒'
    Check ($mainForm -match 'public static bool IsReady' -and
        $mainForm -match 'IAutostartServiceGate' -and
        $mainForm -match 'StartAutomaticService') '静默启动接托盘判定与服务门'
    Check (($program -match '(?s)if \(mainForm\.AutostartRequested\).*?else\s*\{\s*mainForm\.Show\(\)') -and
        ($mainForm -match '(?s)catch\s*\{\s*_trayReady = false')) '托盘注册失败回退显示主窗口'
    Check ($installer -match '_chkAutoStart\.Checked = false' -and
        $installer -match 'StartupRegistration\.IsEnabledFor') '新安装默认关闭且覆盖安装保留选择'
    Check ($maintenance -match 'StartupRegistration\.Disable\(installDir\)' -and
        $maintenance -match 'process\.MainModule\.FileName') 'Run 与进程清理按安装路径隔离'
    Check ($maintenance -match 'DeleteShortcutIfOwned' -and
        $maintenance -match 'GetValue\("InstallLocation"') '共享快捷方式/卸载项按目标归属清理'
    $installerTest = Get-Content (Join-Path $repoRoot 'dev\test-installer.ps1') -Raw
    $uninstallTest = Get-Content (Join-Path $repoRoot 'dev\test-uninstall.ps1') -Raw
    Check ($installerTest -match 'Restore-WorkBuddyRunSnapshot' -and
        $installerTest -match 'Restore-RegistrySnapshot' -and
        $installerTest -match 'Restore-FileSnapshot' -and
        $installerTest -match 'Restore-DirectorySnapshot' -and
        $uninstallTest -match 'Restore-WorkBuddyRunSnapshot' -and
        $uninstallTest -match 'Restore-RegistrySnapshot' -and
        $uninstallTest -match 'Restore-FileSnapshot' -and
        $uninstallTest -match 'Restore-DirectorySnapshot') `
        '安装/卸载测试完整快照并恢复 HKCU Run、卸载项、快捷方式与程序组'

    function Get-InstallIdentity([string]$dir) {
        $path = [IO.Path]::GetFullPath([WbWin]::LongPath($dir)).TrimEnd('\').ToUpperInvariant()
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($path))
            return ([BitConverter]::ToString($hash, 0, 8)).Replace('-', '')
        } finally { $sha.Dispose() }
    }
    function Get-RunValueName([string]$dir) {
        return 'WorkBuddyManager.' + (Get-InstallIdentity $dir)
    }
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
    function Get-FreeTcpPort {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
        $listener.Start()
        try { return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
        finally { $listener.Stop() }
    }
    function New-LauncherFixture([string]$root, [string]$exeSource) {
        New-Item -ItemType Directory (Join-Path $root 'server') -Force | Out-Null
        New-Item -ItemType Directory (Join-Path $root 'windows') -Force | Out-Null
        Copy-Item -LiteralPath $exeSource -Destination (Join-Path $root 'WorkBuddyManager.exe')
        [IO.File]::WriteAllText((Join-Path $root 'server\main.py'), '# test marker', [Text.UTF8Encoding]::new($false))
        $port = Get-FreeTcpPort
        [IO.File]::WriteAllText((Join-Path $root '.env'),
            "WB_MANAGER_HOST=127.0.0.1`r`nWB_MANAGER_PORT=$port`r`n",
            [Text.UTF8Encoding]::new($false))
        $gateLog = Join-Path $root 'gate.log'
        $prepare = "Add-Content -LiteralPath '" + $gateLog.Replace("'", "''") +
            "' -Value 'prepare'`r`nexit 0`r`n"
        $service = "Add-Content -LiteralPath '" + $gateLog.Replace("'", "''") +
            "' -Value ('service:' + `$args[0])`r`nexit 0`r`n"
        [IO.File]::WriteAllText((Join-Path $root 'windows\prepare.ps1'), $prepare,
            [Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText((Join-Path $root 'windows\service-tools.ps1'), $service,
            [Text.UTF8Encoding]::new($false))
    }

    $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (-not (Test-Path -LiteralPath $csc)) {
        $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
    }
    Check (Test-Path -LiteralPath $csc) '找到 .NET Framework csc'

    $testRoot = Join-Path $env:TEMP ('wb autostart A-' + [Guid]::NewGuid().ToString('N'))
    $testRootB = Join-Path $env:TEMP ('wb autostart B-' + [Guid]::NewGuid().ToString('N'))
    $compiledExe = Join-Path $testRoot 'launcher-build.exe'
    $exe = Join-Path $testRoot 'WorkBuddyManager.exe'
    $exeB = Join-Path $testRootB 'WorkBuddyManager.exe'
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    $runSnapshot = @(Get-WorkBuddyRunSnapshot)
    $first = $null
    $second = $null
    $other = $null
    $harness = $null
    try {
        New-Item -ItemType Directory $testRoot -Force | Out-Null
        $sources = @(Get-ChildItem (Join-Path $src '*.cs') | ForEach-Object { $_.FullName })
        & $csc -nologo -target:winexe -codepage:65001 -optimize+ ('-out:' + $compiledExe) `
            -reference:System.dll,System.Core.dll,System.Drawing.dll,System.Windows.Forms.dll,System.IO.Compression.dll,System.IO.Compression.FileSystem.dll,Microsoft.CSharp.dll `
            $sources
        Check ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $compiledExe)) 'launcher 全源编译'
        if ($LASTEXITCODE -ne 0) { throw 'csc 编译失败' }
        New-LauncherFixture $testRoot $compiledExe
        New-LauncherFixture $testRootB $compiledExe

        $valueA = Get-RunValueName $testRoot
        $valueB = Get-RunValueName $testRootB
        Remove-ItemProperty -LiteralPath $runKey -Name $valueA -ErrorAction SilentlyContinue
        Remove-ItemProperty -LiteralPath $runKey -Name $valueB -ErrorAction SilentlyContinue
        $sentinelName = 'WorkBuddyManager.REVIEW-OTHER'
        $sentinelValue = '"D:\other install\WorkBuddyManager.exe" --silent'
        New-ItemProperty -LiteralPath $runKey -Name $sentinelName -Value $sentinelValue `
            -PropertyType String -Force | Out-Null
        $first = Start-Process -FilePath $exe -PassThru
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline) {
            $first.Refresh()
            if ($first.MainWindowHandle -ne 0) { break }
            Start-Sleep -Milliseconds 250
        }
        Check ($first.MainWindowHandle -ne 0) '普通启动显示主窗口'
        $box = [WbWin]::FindChild($first.MainWindowHandle, 'BUTTON', '登录 Windows 时自动启动（静默到托盘）')
        Check ($box -ne [IntPtr]::Zero) '找到开机自启开关'
        Check ($box -ne [IntPtr]::Zero -and [WbWin]::GetCheck($box) -eq 0) '新安装默认关闭'

        if ($box -ne [IntPtr]::Zero) { [WbWin]::Click($box) }
        Start-Sleep -Milliseconds 500
        $actual = (Get-ItemProperty -LiteralPath $runKey -Name $valueA -ErrorAction Stop).$valueA
        $canonicalExe = [WbWin]::LongPath($exe)
        Check ($actual -eq ('"' + $canonicalExe + '" --silent')) 'Run 值精确指向安装 exe 并带 --silent'

        $other = Start-Process -FilePath $exeB -PassThru
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline) {
            $other.Refresh()
            if ($other.MainWindowHandle -ne 0) { break }
            Start-Sleep -Milliseconds 250
        }
        $boxB = [WbWin]::FindChild($other.MainWindowHandle, 'BUTTON', '登录 Windows 时自动启动（静默到托盘）')
        $props = Get-ItemProperty -LiteralPath $runKey
        Check ($boxB -ne [IntPtr]::Zero -and [WbWin]::GetCheck($boxB) -eq 0 -and
            ($props.PSObject.Properties.Name -contains $valueA) -and
            -not ($props.PSObject.Properties.Name -contains $valueB)) `
            '安装 B 且未勾选时保留 A 的 Run 值'

        if ($boxB -ne [IntPtr]::Zero) { [WbWin]::Click($boxB) }
        Start-Sleep -Milliseconds 500
        $props = Get-ItemProperty -LiteralPath $runKey
        Check (($props.PSObject.Properties.Name -contains $valueB) -and
            ($props.PSObject.Properties.Name -contains $valueA)) 'A/B 两个安装拥有独立 Run 值'
        if ($boxB -ne [IntPtr]::Zero) { [WbWin]::Click($boxB) }
        Start-Sleep -Milliseconds 500
        $props = Get-ItemProperty -LiteralPath $runKey
        Check (-not ($props.PSObject.Properties.Name -contains $valueB) -and
            ($props.PSObject.Properties.Name -contains $valueA)) '关闭 B 自启不关闭 A'

        # 直接调用生产 Maintenance，真实验证只关闭 B 路径的进程。
        $asm = [Reflection.Assembly]::Load([IO.File]::ReadAllBytes([WbWin]::LongPath($exeB)))
        $maintenanceType = $asm.GetType('WorkBuddyLauncher.Maintenance', $true)
        $closeMethod = $maintenanceType.GetMethod('CloseRunningLauncher',
            [Reflection.BindingFlags]'Static,Public,NonPublic')
        $closeMethod.Invoke($null, @([WbWin]::LongPath($testRootB))) | Out-Null
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline) {
            $other.Refresh()
            if ($other.HasExited) { break }
            Start-Sleep -Milliseconds 100
        }
        $first.Refresh()
        Check ($other.HasExited -and -not $first.HasExited) '关闭进程按安装路径过滤，不误杀 A'

        $first.Kill()
        $first.WaitForExit()
        $first.Dispose()
        $first = $null
        Start-Sleep -Milliseconds 500
        $first = Start-Process -FilePath $exe -PassThru
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline) {
            $first.Refresh()
            if ($first.MainWindowHandle -ne 0) { break }
            Start-Sleep -Milliseconds 250
        }
        $box = [WbWin]::FindChild($first.MainWindowHandle, 'BUTTON', '登录 Windows 时自动启动（静默到托盘）')
        if ($box -ne [IntPtr]::Zero) { [WbWin]::Click($box) }
        Start-Sleep -Milliseconds 500
        $props = Get-ItemProperty -LiteralPath $runKey
        $removedAfterRestart = -not ($props.PSObject.Properties.Name -contains $valueA)
        if ($box -ne [IntPtr]::Zero) { [WbWin]::Click($box) }
        Start-Sleep -Milliseconds 500
        $props = Get-ItemProperty -LiteralPath $runKey
        Check ($removedAfterRestart -and ($props.PSObject.Properties.Name -contains $valueA)) `
            '重启后开关状态保持（切一次关闭、再切一次恢复）'

        $first.Kill()
        $first.WaitForExit()
        $first.Dispose()
        $first = $null
        Start-Sleep -Milliseconds 500
        $first = Start-Process -FilePath $exe -ArgumentList '--silent' -PassThru
        $silentSawWindow = $false
        $gateLog = Join-Path $testRoot 'gate.log'
        $gateStarted = $false
        $deadline = (Get-Date).AddSeconds(12)
        while ((Get-Date) -lt $deadline) {
            $first.Refresh()
            if ($first.MainWindowHandle -ne 0) { $silentSawWindow = $true; break }
            if (Test-Path -LiteralPath $gateLog) {
                $gateLines = @(Get-Content -LiteralPath $gateLog)
                if ($gateLines -contains 'prepare' -and $gateLines -contains 'service:start') {
                    $gateStarted = $true
                    break
                }
            }
            Start-Sleep -Milliseconds 50
        }
        Start-Sleep -Milliseconds 800
        $gateLines = @(Get-Content -LiteralPath $gateLog -ErrorAction SilentlyContinue)
        Check (-not $first.HasExited -and -not $silentSawWindow -and $gateStarted) `
            '--silent 驻留托盘并自动执行 prepare + service-tools start'
        Check (@($gateLines | Where-Object { $_ -eq 'prepare' }).Count -eq 1 -and
            @($gateLines | Where-Object { $_ -eq 'service:start' }).Count -eq 1) `
            '静默启动不重复拉起服务'

        $second = Start-Process -FilePath $exe -PassThru
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline) {
            $first.Refresh()
            $second.Refresh()
            if ($second.HasExited -and $first.MainWindowHandle -ne 0) { break }
            Start-Sleep -Milliseconds 250
        }
        Check ($second.HasExited -and $first.MainWindowHandle -ne 0) '第二实例唤醒托盘实例并退出'

        $box = [WbWin]::FindChild($first.MainWindowHandle, 'BUTTON', '登录 Windows 时自动启动（静默到托盘）')
        if ($box -ne [IntPtr]::Zero) { [WbWin]::Click($box) }
        Start-Sleep -Milliseconds 500
        $props = Get-ItemProperty -LiteralPath $runKey
        Check (-not ($props.PSObject.Properties.Name -contains $valueA) -and
            ($props.PSObject.Properties.Name -contains $sentinelName) -and
            $props.$sentinelName -eq $sentinelValue) '关闭 A 只移除 A 的 Run 值并保留其他值'

        # 可注入托盘/服务门 harness：真实实例化 MainForm，不碰真实网络端口。
        $harnessSource = Join-Path $testRoot 'ReviewAutostartHarness.cs'
        $harnessExe = Join-Path $testRoot 'ReviewAutostartHarness.exe'
        $harnessCode = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    internal sealed class FakeAutostartGate : IAutostartServiceGate
    {
        private readonly bool _fail;
        public int StartCount;

        public FakeAutostartGate(bool fail) { _fail = fail; }
        public bool IsHealthy(string connectHost, int port) { return false; }
        public bool IsPortInUse(string connectHost, int port) { return false; }
        public void Start(string installDir, Action<string> onLine, Action<int> onExit)
        {
            StartCount++;
            if (_fail && onExit != null) { onExit(7); }
        }
    }

    internal static class ReviewAutostartHarness
    {
        [STAThread]
        private static int Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string mode = Environment.GetEnvironmentVariable("WB_AUTOSTART_HARNESS_MODE");
            string root = Environment.GetEnvironmentVariable("WB_AUTOSTART_HARNESS_ROOT");
            string resultPath = Environment.GetEnvironmentVariable("WB_AUTOSTART_HARNESS_RESULT");
            if (string.IsNullOrWhiteSpace(mode) || string.IsNullOrWhiteSpace(root) ||
                string.IsNullOrWhiteSpace(resultPath))
            {
                return 4;
            }
            root = Path.GetFullPath(root);
            resultPath = Path.GetFullPath(resultPath);
            string allowedPrefix = root.TrimEnd(Path.DirectorySeparatorChar) +
                Path.DirectorySeparatorChar;
            if (!resultPath.StartsWith(allowedPrefix, StringComparison.OrdinalIgnoreCase))
            {
                return 5;
            }
            bool trayUnavailable = mode.StartsWith("tray-unavailable", StringComparison.Ordinal);
            bool trayReady = !trayUnavailable;
            bool gateShouldFail = mode.EndsWith("gate-fail", StringComparison.Ordinal);
            var gate = new FakeAutostartGate(gateShouldFail);
            var form = new MainForm(root, true, delegate { return trayReady; }, gate);
            var timer = new Timer();
            var watch = Stopwatch.StartNew();
            bool passed = false;
            timer.Interval = 100;
            timer.Tick += delegate
            {
                bool ok = trayUnavailable
                    ? gate.StartCount == 1 && form.Visible && !form.ShouldStartHidden &&
                        form.AutostartFailed == gateShouldFail
                    : gateShouldFail
                        ? gate.StartCount == 1 && form.Visible && !form.ShouldStartHidden &&
                            form.AutostartFailed
                        : gate.StartCount == 1 && !form.Visible && form.ShouldStartHidden;
                if (ok)
                {
                    passed = true;
                    timer.Stop();
                    Application.Exit();
                }
                else if (watch.Elapsed.TotalSeconds > 8)
                {
                    timer.Stop();
                    Application.Exit();
                }
            };
            timer.Start();
            Application.Run(new LauncherApplicationContext(form));
            timer.Dispose();
            File.WriteAllText(resultPath, passed ? "PASS" : "FAIL");
            return passed ? 0 : 1;
        }
    }
}
'@
        [IO.File]::WriteAllText($harnessSource, $harnessCode, [Text.UTF8Encoding]::new($false))
        $sourcesWithHarness = @($sources) + $harnessSource
        & $csc -nologo -target:exe -codepage:65001 -optimize+ ('-out:' + $harnessExe) `
            -main:WorkBuddyLauncher.ReviewAutostartHarness `
            -reference:System.dll,System.Core.dll,System.Drawing.dll,System.Windows.Forms.dll,System.IO.Compression.dll,System.IO.Compression.FileSystem.dll,Microsoft.CSharp.dll `
            $sourcesWithHarness
        Check ($LASTEXITCODE -eq 0) '可注入托盘/服务门 harness 编译'
        foreach ($mode in @('tray-unavailable', 'tray-unavailable-gate-fail', 'gate-start', 'gate-fail')) {
            $resultPath = Join-Path $testRoot ('harness-' + $mode + '.txt')
            $env:WB_AUTOSTART_HARNESS_MODE = $mode
            $env:WB_AUTOSTART_HARNESS_ROOT = $testRoot
            $env:WB_AUTOSTART_HARNESS_RESULT = $resultPath
            $harness = Start-Process -FilePath $harnessExe -PassThru -Wait
            $result = if (Test-Path -LiteralPath $resultPath) {
                (Get-Content -LiteralPath $resultPath -Raw).Trim()
            } else { '<missing>' }
            Write-Host ('  [diag] ' + $mode + ' exit=' + $harness.ExitCode + ' result=' + $result)
            Check ($harness.ExitCode -eq 0 -and $result -eq 'PASS') `
                ('注入 harness：' + $mode)
        }
        Check (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'autostart'))) `
            'harness 未向仓库根目录泄漏结果文件'
    }
    finally {
        if ($other -and -not $other.HasExited) { $other.Kill(); $other.WaitForExit() }
        if ($harness -and -not $harness.HasExited) { $harness.Kill(); $harness.WaitForExit() }
        if ($first -and -not $first.HasExited) { $first.Kill(); $first.WaitForExit() }
        if ($second -and -not $second.HasExited) { $second.Kill(); $second.WaitForExit() }
        Remove-Item Env:WB_AUTOSTART_HARNESS_MODE -ErrorAction SilentlyContinue
        Remove-Item Env:WB_AUTOSTART_HARNESS_ROOT -ErrorAction SilentlyContinue
        Remove-Item Env:WB_AUTOSTART_HARNESS_RESULT -ErrorAction SilentlyContinue
        Restore-WorkBuddyRunSnapshot $runSnapshot
        $resolvedTemp = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
        foreach ($path in @($testRoot, $testRootB)) {
            $resolvedTest = [IO.Path]::GetFullPath($path)
            if ($resolvedTest.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and
                (Test-Path -LiteralPath $resolvedTest)) {
                Remove-Item -LiteralPath $resolvedTest -Recurse -Force
            }
        }
    }

    if ($script:failed) { Write-Host 'RESULT: FAIL'; exit 1 }
    Write-Host 'RESULT: PASS'
    exit 0
}

$installDir = 'D:\ai-project\_wb_test_install'
$launcherExe = Join-Path $installDir 'WorkBuddyManager.exe'
$testPort = 17864          # 独立测试端口，避开机器上可能存在的 7864 生产实例

if (-not (Test-Path $launcherExe)) { Write-Error "未找到启动器：$launcherExe"; exit 1 }

# 清理上一次运行残留：先按 .env 停服务，再杀安装目录下的监听进程与启动器窗口
& (Join-Path $installDir 'windows\service-tools.ps1') stop 2>&1 | Out-Null
Get-Process -Name 'WorkBuddyManager' -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        if ([string]::Equals([IO.Path]::GetFullPath($_.Path), [IO.Path]::GetFullPath($launcherExe),
            [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id $_.Id -Force
        }
    } catch { }
}
foreach ($p in @(7864, 17864)) {
    Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        $listener = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        if ($listener -and $listener.Path -like ($installDir + '*')) {
            Stop-Process -Id $listener.Id -Force
            Write-Host ('清理残留进程: ' + $listener.Id + ' (端口 ' + $p + ')')
        }
    }
}
Start-Sleep -Seconds 2
# 测试端口写入 .env
$envFile = Join-Path $installDir '.env'
if (Test-Path $envFile) {
    $lines = Get-Content $envFile
    $lines = $lines | ForEach-Object { if ($_ -match '^WB_MANAGER_PORT=') { 'WB_MANAGER_PORT=' + $testPort } else { $_ } }
    Set-Content $envFile $lines -Encoding UTF8
}

$proc = Start-Process $launcherExe -PassThru

function Wait-MainWindow {
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        $proc.Refresh()
        if ($proc.MainWindowHandle -ne 0) { return $proc.MainWindowHandle }
        Start-Sleep -Milliseconds 500
    }
    return [IntPtr]::Zero
}

function Wait-Healthy($seconds) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        try {
            $resp = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $testPort + '/api/healthz') -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Wait-Gone {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 60) {
        try {
            $resp = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $testPort + '/api/healthz') -UseBasicParsing -TimeoutSec 2
        } catch { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

$win = Wait-MainWindow
if ($win -eq [IntPtr]::Zero) { Write-Host 'FAIL: 控制窗口未出现'; $proc.Kill(); exit 1 }
[WbWin]::SetForegroundWindow($win) | Out-Null
Write-Host 'OK: 控制窗口出现'
Start-Sleep -Seconds 1

# 截图初始状态（应显示「未运行」与端口 17864）
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Drawing;

public class WinCap6
{
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
    public struct RECT { public int Left, Top, Right, Bottom; }

    public static void Capture(IntPtr hwnd, string path)
    {
        RECT r;
        GetWindowRect(hwnd, out r);
        int w = r.Right - r.Left;
        int h = r.Bottom - r.Top;
        if (w < 10) { w = 400; }
        if (h < 10) { h = 200; }
        using (var bmp = new Bitmap(w, h))
        using (var g = Graphics.FromImage(bmp))
        {
            IntPtr hdc = g.GetHdc();
            PrintWindow(hwnd, hdc, 2);
            g.ReleaseHdc(hdc);
            bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png);
        }
    }
}
'@ -ReferencedAssemblies System.Drawing
[WinCap6]::Capture($win, (Join-Path $env:TEMP 'wb-ctrl-initial.png'))

# 点击「启动项目」
$startBtn = [WbWin]::FindChild($win, 'BUTTON', '启动项目')
if ($startBtn -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未找到「启动项目」按钮'; $proc.Kill(); exit 1 }
[WbWin]::Click($startBtn)
Write-Host 'OK: 已点击「启动项目」，等待服务就绪…'

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$healthy = Wait-Healthy 150
Write-Host ('健康检查: ' + $healthy + '（用时 ' + [int]$sw.Elapsed.TotalSeconds + ' 秒）')
if (-not $healthy) { Write-Host 'FAIL: 服务未就绪'; $proc.Kill(); exit 1 }

Start-Sleep -Seconds 3
[WinCap6]::Capture($win, (Join-Path $env:TEMP 'wb-ctrl-running.png'))
$stateText = [WbWin]::FindChild($win, 'STATIC', '运行中')
Write-Host ('状态文本「运行中」: ' + ($stateText -ne [IntPtr]::Zero))

# 「打开面板」按钮存在
$openBtn = [WbWin]::FindChild($win, 'BUTTON', '打开面板')
Write-Host ('「打开面板」按钮存在: ' + ($openBtn -ne [IntPtr]::Zero))
$browser = Get-Process -Name msedge,chrome,firefox -ErrorAction SilentlyContinue
Write-Host ('浏览器进程存在（面板自动打开）: ' + ($null -ne $browser -and @($browser).Count -gt 0))

# 修改端口并保存 → 校验 .env
# 端口框按控件序号定位（EDIT#1 = 端口，创建顺序稳定），避免跨进程读大文本
Write-Host '[diag] 定位端口框…'
$editItems = @([WbWin]::Children($win) | Where-Object { $_[1] -like '*EDIT*' })
if ($editItems.Count -lt 2) { Write-Host ('FAIL: 输入框不足（' + $editItems.Count + '）'); $proc.Kill(); exit 1 }
$portBox = [IntPtr]$editItems[1][0]
Write-Host ('[diag] 端口框 hwnd=' + $portBox)
[WbWin]::SetText($portBox, '17865')
$saveBtn = [WbWin]::FindChild($win, 'BUTTON', '保存配置')
if ($saveBtn -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未找到保存按钮'; $proc.Kill(); exit 1 }
[WbWin]::Click($saveBtn)

function Wait-DialogAndConfirm($seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        $dialog = [WbTop]::FindDialog($proc.Id)
        if ($dialog -ne [IntPtr]::Zero) {
            $okBtn = [WbWin]::FindChild($dialog, 'BUTTON', '确定')
            if ($okBtn -ne [IntPtr]::Zero) { [WbWin]::Click($okBtn); return $true }
        }
    }
    return $false
}

$tipSeen = Wait-DialogAndConfirm 10
Write-Host ('保存提示框已处理: ' + $tipSeen)
Start-Sleep -Seconds 1

$portLine = (Get-Content $envFile) | Where-Object { $_ -match '^WB_MANAGER_PORT=' }
Write-Host ('.env 端口行: ' + $portLine)
if ($portLine -match '17865') { Write-Host 'OK: 端口修改已保存到 .env' } else { Write-Host 'FAIL: 端口修改未生效' }

# 还原端口
[WbWin]::SetText($portBox, [string]$testPort)
[WbWin]::Click($saveBtn)
$tipSeen = Wait-DialogAndConfirm 10
Start-Sleep -Seconds 1

$portLine = (Get-Content $envFile) | Where-Object { $_ -match '^WB_MANAGER_PORT=' }
Write-Host ('.env 端口行: ' + $portLine)
if ($portLine -match '17865') { Write-Host 'OK: 端口修改已保存到 .env' } else { Write-Host 'FAIL: 端口修改未生效' }

# 还原端口
[WbWin]::SetText($portBox, [string]$testPort)
[WbWin]::Click($saveBtn)
$tipDeadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $tipDeadline) {
    Start-Sleep -Milliseconds 400
    $dialog = (Get-Process -Id $proc.Id).MainWindowHandle
    $okBtn = [WbWin]::FindChild($dialog, 'BUTTON', '确定')
    if ($okBtn -ne [IntPtr]::Zero) { [WbWin]::Click($okBtn); break }
}
Start-Sleep -Seconds 1

# 点击「关闭项目」→ 等端口释放
$stopBtn = [WbWin]::FindChild($win, 'BUTTON', '关闭项目')
if ($stopBtn -eq [IntPtr]::Zero) { Write-Host 'FAIL: 未找到「关闭项目」按钮'; $proc.Kill(); exit 1 }
[WbWin]::Click($stopBtn)
Write-Host 'OK: 已点击「关闭项目」，等待端口释放…'
$stopped = Wait-Gone
Write-Host ('服务已停止: ' + $stopped)
if (-not $stopped) { Write-Host 'FAIL: 服务未停止' }

# 截图关闭后的状态（应回到「未运行」）
Start-Sleep -Seconds 3
[WinCap6]::Capture($win, (Join-Path $env:TEMP 'wb-ctrl-stopped.png'))

# 收尾
if (-not $proc.HasExited) { $proc.Kill() }
& (Join-Path $installDir 'windows\service-tools.ps1') stop 2>&1 | Out-Null
Write-Host 'DONE'
