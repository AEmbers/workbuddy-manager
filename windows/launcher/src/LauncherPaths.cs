using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace WorkBuddyLauncher
{
    /// 应用级常量与固定路径。
    internal static class LauncherPaths
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern uint GetLongPathName(string shortPath, StringBuilder longPath, uint length);

        public const string AppName = "WorkBuddy Manager";
        public const string AppExeName = "WorkBuddyManager.exe";
        public const string UninstallExeName = "Uninstall.exe";
        public const string UninstallRegKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\WorkBuddyManager";

        /// 内嵌 payload 的资源名（csc -resource:payload.zip,payload.zip）。
        public const string PayloadResourceName = "payload.zip";

        /// 安装标识：payload 解压产物之一，用它判断目录里是否是一个安装好的项目。
        private const string InstallMarker = @"server\main.py";

        public static bool LooksInstalled(string dir)
        {
            return !string.IsNullOrEmpty(dir) && File.Exists(Path.Combine(dir, InstallMarker));
        }

        /// 安装目录的稳定短标识，用于单实例名、每安装独立 Run 值等共存边界。
        public static string InstallIdentity(string dir)
        {
            string path = NormalizePath(dir)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                .ToUpperInvariant();
            using (SHA256 sha = SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(Encoding.UTF8.GetBytes(path));
                return BitConverter.ToString(hash, 0, 8).Replace("-", string.Empty);
            }
        }

        public static string NormalizePath(string path)
        {
            string full = Path.GetFullPath(path);
            var buffer = new StringBuilder(4096);
            uint length = GetLongPathName(full, buffer, (uint)buffer.Capacity);
            return length > 0 && length < buffer.Capacity ? buffer.ToString() : full;
        }

        public static string DefaultInstallDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "WorkBuddyManager");
            }
        }

        /// 项目内脚本路径（start/prepare/service-tools 等都在 windows\ 下）。
        public static string ScriptPath(string root, string fileName)
        {
            return Path.Combine(Path.Combine(root, "windows"), fileName);
        }

        public static string EnvFilePath(string root)
        {
            return Path.Combine(root, ".env");
        }

        /// 启动器偏好目录（%APPDATA%\WorkBuddyManager）。
        public static string AppDataDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "WorkBuddyManager");
            }
        }

        public static string SettingsFile
        {
            get { return Path.Combine(AppDataDir, "launcher.conf"); }
        }

        public static string DesktopShortcutPath
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), AppName + ".lnk");
            }
        }

        /// 开始菜单的程序组目录（%APPDATA%\Microsoft\Windows\Start Menu\Programs\WorkBuddy Manager）。
        public static string StartMenuFolder
        {
            get
            {
                return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), AppName);
            }
        }
    }
}
