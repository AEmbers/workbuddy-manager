using System;
using System.IO;
using System.Threading;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 安装器 / 启动器 / 卸载器的统一入口。
    /// 同一个 exe 复制成不同名字后承担不同角色：安装包（任意位置）→ 安装模式；
    /// 安装目录里的 WorkBuddyManager.exe → 控制模式；Uninstall.exe → 卸载模式。
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            // 卸载模式：安装目录里的 Uninstall.exe（按文件名判定）
            string exeName = Path.GetFileName(Application.ExecutablePath);
            if (exeName.StartsWith("Uninstall", StringComparison.OrdinalIgnoreCase))
            {
                Application.Run(new UninstallForm());
                return;
            }

            string exeDir = Path.GetDirectoryName(Application.ExecutablePath);

            // 控制模式：exe 所在目录就是一个安装好的项目（单实例）
            if (LauncherPaths.LooksInstalled(exeDir))
            {
                bool silentStartup = HasSilentArgument();
                string instanceSuffix = LauncherPaths.InstallIdentity(exeDir);
                string instanceMutexName = @"Local\WorkBuddyManagerLauncher." + instanceSuffix;
                string wakeEventName = @"Local\WorkBuddyManagerLauncher.Wake." + instanceSuffix;
                // 事件先于互斥量创建，避免第二实例在首实例完成初始化前丢失唤醒信号。
                using (var wakeEvent = new EventWaitHandle(false, EventResetMode.AutoReset, wakeEventName))
                {
                    bool createdNew = false;
                    using (new Mutex(true, instanceMutexName, out createdNew))
                    {
                        if (!createdNew)
                        {
                            // 第二次启动时唤醒已有控制窗口；静默到托盘的首实例也会被恢复。
                            try { wakeEvent.Set(); }
                            catch { }
                            return;
                        }

                        var mainForm = new MainForm(exeDir, silentStartup);
                        RegisteredWaitHandle wakeRegistration = ThreadPool.RegisterWaitForSingleObject(
                            wakeEvent,
                            delegate { mainForm.WakeExistingInstance(); },
                            null,
                            Timeout.Infinite,
                            false);
                        try
                        {
                            Application.Run(new LauncherApplicationContext(mainForm));
                        }
                        finally
                        {
                            wakeRegistration.Unregister(null);
                        }
                        return;
                    }
                }
            }

            // 安装模式：从任意位置双击的安装包
            Application.Run(new InstallerForm());
        }

        private static bool HasSilentArgument()
        {
            string[] args = Environment.GetCommandLineArgs();
            for (int i = 1; i < args.Length; i++)
            {
                if (string.Equals(args[i], StartupRegistration.SilentArgument,
                    StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

    }

    /// 控制窗口不一定在启动时显示：登录自启先创建句柄并开始探测，窗口保持隐藏。
    internal sealed class LauncherApplicationContext : ApplicationContext
    {
        public LauncherApplicationContext(MainForm mainForm)
        {
            mainForm.FormClosed += delegate { ExitThread(); };

            if (mainForm.AutostartRequested)
            {
                mainForm.PrepareHiddenStart();
            }
            else
            {
                mainForm.Show();
            }
        }
    }
}
