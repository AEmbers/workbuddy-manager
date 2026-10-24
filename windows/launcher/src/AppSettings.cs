using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Microsoft.Win32;

namespace WorkBuddyLauncher
{
    /// 启动器偏好存储：key=value 纯文本，落在 %APPDATA%\WorkBuddyManager\launcher.conf。
    internal sealed class AppSettings
    {
        private readonly string _path;
        private readonly Dictionary<string, string> _values =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        public AppSettings(string path)
        {
            _path = path;
            Load();
        }

        public string Get(string key, string fallback)
        {
            string value;
            return _values.TryGetValue(key, out value) ? value : fallback;
        }

        public void Set(string key, string value)
        {
            _values[key] = value;
        }

        public void Remove(string key)
        {
            _values.Remove(key);
        }

        public void Save()
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_path));
            var lines = new List<string>();
            foreach (KeyValuePair<string, string> pair in _values)
            {
                lines.Add(pair.Key + "=" + pair.Value);
            }
            File.WriteAllLines(_path, lines.ToArray(), new UTF8Encoding(false));
        }

        private void Load()
        {
            if (!File.Exists(_path)) { return; }
            foreach (string line in File.ReadAllLines(_path, Encoding.UTF8))
            {
                int index = line.IndexOf('=');
                if (index > 0)
                {
                    _values[line.Substring(0, index).Trim()] = line.Substring(index + 1).Trim();
                }
            }
        }
    }

    /// Windows 登录自启登记。HKCU\...\Run 是唯一持久化来源；每个安装目录使用独立值名，
    /// 安装、覆盖和卸载只增删自己的值，不能顺带关闭或改写另一份安装。
    internal static class StartupRegistration
    {
        public const string SilentArgument = "--silent";
        public const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
        public const string ValueNamePrefix = "WorkBuddyManager.";

        public static string ValueNameFor(string installDir)
        {
            return ValueNamePrefix + LauncherPaths.InstallIdentity(installDir);
        }

        public static string ExecutablePathFor(string installDir)
        {
            return Path.Combine(LauncherPaths.NormalizePath(installDir), LauncherPaths.AppExeName);
        }

        public static string CommandFor(string installDir)
        {
            return "\"" + ExecutablePathFor(installDir) + "\" " + SilentArgument;
        }

        public static string ReadCommand(string installDir)
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RunKeyPath, false))
            {
                if (key == null) { return string.Empty; }
                return (key.GetValue(ValueNameFor(installDir), string.Empty) as string) ?? string.Empty;
            }
        }

        public static bool IsEnabledFor(string installDir)
        {
            return string.Equals(
                ReadCommand(installDir),
                CommandFor(installDir),
                StringComparison.OrdinalIgnoreCase);
        }

        public static void Disable(string installDir)
        {
            SetEnabled(installDir, false);
        }

        public static void SetEnabled(string installDir, bool enabled)
        {
            if (string.IsNullOrWhiteSpace(installDir))
            {
                throw new ArgumentException("必须提供要操作的安装目录。", "installDir");
            }
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RunKeyPath))
            {
                if (key == null)
                {
                    throw new InvalidOperationException("无法打开当前用户的登录启动项。");
                }
                if (enabled)
                {
                    key.SetValue(ValueNameFor(installDir), CommandFor(installDir), RegistryValueKind.String);
                }
                else
                {
                    key.DeleteValue(ValueNameFor(installDir), false);
                }
            }
        }
    }
}
