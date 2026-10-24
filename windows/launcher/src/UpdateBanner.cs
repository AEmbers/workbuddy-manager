using System;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text.RegularExpressions;
using System.Windows.Forms;

namespace WorkBuddyLauncher
{
    /// 新版本通知横幅：发布渠道出现比本地更新的版本时，在控制窗口顶部显示一条
    /// 可关闭的横幅；点击链接用系统默认浏览器打开发布页，用户在发布页下载最新
    /// EXE 安装包自行覆盖安装（Windows 原生部署不做应用内一键更新）。
    /// 检查在后台线程进行，失败（无网 / 超时 / 限流）静默——横幅只做增量提示，
    /// 不弹窗、不打断，任何异常都不影响控制窗口本身。
    internal sealed class UpdateBanner : Panel
    {
        /// 用户仓库的发布页与 latest API（与 updater 的 WB_MANAGER_REPO 同源）。
        public const string ReleaseUrl = "https://github.com/AEmbers/workbuddy-manager/releases/latest";
        public const string ReleasesApi = "https://api.github.com/repos/AEmbers/workbuddy-manager/releases/latest";

        private readonly LinkLabel _link = new LinkLabel();
        private readonly Label _close = new Label();
        private string _latestTag;

        /// 当前展示的新版本 tag（构造后不可变，供控制窗口判断是否需要重建横幅）。
        public string LatestTag { get { return _latestTag; } }

        /// 用户手动关闭后触发；控制窗口据此在本次运行内不再重新显示。
        public event EventHandler Dismissed;

        public UpdateBanner(string latestTag)
        {
            _latestTag = latestTag;

            Dock = DockStyle.Top;
            Height = 34;
            BackColor = Color.FromArgb(255, 247, 231);

            _link.AutoSize = false;
            _link.BackColor = Color.Transparent;
            _link.ForeColor = Color.FromArgb(92, 64, 16);
            _link.LinkColor = Color.FromArgb(140, 90, 10);
            _link.ActiveLinkColor = Color.FromArgb(180, 110, 10);
            _link.LinkBehavior = LinkBehavior.HoverUnderline;
            _link.Font = new Font(SystemFonts.MessageBoxFont.FontFamily, 9f, FontStyle.Bold);
            _link.SetBounds(12, 8, 400, 18);
            _link.Text = "🚀 发现新版本 " + latestTag + " —— 点击前往发布页下载最新 EXE 安装包";
            _link.LinkClicked += delegate { OpenReleasePage(); };
            Controls.Add(_link);

            _close.AutoSize = false;
            _close.BackColor = Color.Transparent;
            _close.ForeColor = Color.FromArgb(140, 110, 60);
            _close.Font = new Font(SystemFonts.MessageBoxFont.FontFamily, 10f, FontStyle.Bold);
            _close.TextAlign = ContentAlignment.MiddleCenter;
            _close.SetBounds(0, 0, 26, Height);
            _close.Text = "×";
            _close.Click += delegate
            {
                Visible = false;
                EventHandler dismissed = Dismissed;
                if (dismissed != null) dismissed(this, EventArgs.Empty);
            };
            Controls.Add(_close);

            Resize += delegate
            {
                _link.Width = Width - _link.Left - _close.Width - 8;
                _close.Height = Height;
            };
            _link.Width = Width - _link.Left - _close.Width - 8;
        }

        /// 用系统默认浏览器打开发布页（UseShellExecute 走 shell 关联，EXE 内嵌
        /// WebView 也不会拦）。
        public static void OpenReleasePage()
        {
            try
            {
                System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(ReleaseUrl)
                {
                    UseShellExecute = true
                });
            }
            catch
            {
                // 浏览器打开失败：横幅本身还能看到发布页地址文字，静默即可
            }
        }

        /// 后台线程调用：拉 latest 发布并比较版本。返回要展示的 tag（如 "v2.8.8"），
        /// 无新版 / 网络 / 解析失败返回 null。localVersion 取自安装目录 .version。
        public static string CheckNewer(string localVersion)
        {
            if (string.IsNullOrEmpty(localVersion))
            {
                return null; // 本地版本未知时不误报
            }
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(ReleasesApi);
                req.UserAgent = "WorkBuddyManager-Launcher";
                req.Timeout = 8000;
                req.ReadWriteTimeout = 8000;
                using (var resp = req.GetResponse())
                using (var stream = resp.GetResponseStream())
                using (var reader = new StreamReader(stream))
                {
                    var json = reader.ReadToEnd();
                    var m = Regex.Match(json, "\"tag_name\"\\s*:\\s*\"v?([0-9]+(?:\\.[0-9]+)+)\"");
                    if (!m.Success)
                    {
                        return null;
                    }
                    if (CompareVersions(m.Groups[1].Value, localVersion) > 0)
                    {
                        return "v" + m.Groups[1].Value;
                    }
                }
            }
            catch
            {
                // 无网 / 超时 / GitHub 限流：静默，下次检查再说
            }
            return null;
        }

        /// 语义化版本比较：逐段数值比较，段数不足补 0；解析失败按不可比处理（-1）。
        internal static int CompareVersions(string a, string b)
        {
            long[] pa = ParseSegments(a);
            long[] pb = ParseSegments(b);
            if (pa == null || pb == null)
            {
                return -1;
            }
            int n = Math.Max(pa.Length, pb.Length);
            for (int i = 0; i < n; i++)
            {
                long x = i < pa.Length ? pa[i] : 0;
                long y = i < pb.Length ? pb[i] : 0;
                if (x != y)
                {
                    return x < y ? -1 : 1;
                }
            }
            return 0;
        }

        private static long[] ParseSegments(string v)
        {
            if (string.IsNullOrEmpty(v))
            {
                return null;
            }
            v = v.Trim();
            if (v.StartsWith("v", StringComparison.OrdinalIgnoreCase))
            {
                v = v.Substring(1);
            }
            try
            {
                string[] parts = v.Split('.');
                long[] outv = new long[parts.Length];
                for (int i = 0; i < parts.Length; i++)
                {
                    if (parts[i].Length == 0 || parts[i].Length > 9)
                    {
                        return null;
                    }
                    outv[i] = long.Parse(parts[i]);
                }
                return outv;
            }
            catch
            {
                return null;
            }
        }
    }
}
