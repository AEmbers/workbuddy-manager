"""Windows 安装元数据的幂等刷新。

一键更新会替换程序文件并重启管理端，但不会重新运行安装向导。旧版本因此可能
继续在「设置 → 应用」里显示旧版本号。这里只修当前用户卸载项，而且仅在
`InstallLocation` 与本安装根目录完全一致时动作，避免误改另一份安装。
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import config

UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\WorkBuddyManager'


def _same_path(left: object, right: Path) -> bool:
    if not isinstance(left, str) or not left.strip():
        return False
    try:
        a = os.path.normcase(os.path.abspath(os.path.expandvars(left.strip())))
        b = os.path.normcase(os.path.abspath(str(right)))
    except (OSError, ValueError):
        return False
    return a.rstrip('\\/') == b.rstrip('\\/')


def refresh_uninstall_version(
    root: Path | None = None,
    *,
    winreg_module: object | None = None,
) -> bool:
    """同步 HKCU 卸载项的 DisplayVersion；任何不确定情况都保持原状。

    返回值只表示本次是否真的写入。调用方不应让它阻断服务启动。
    """
    base = Path(root or config.ROOT)
    try:
        version = (base / '.version').read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return False
    if not version:
        return False

    winreg = winreg_module
    if winreg is None:
        if os.name != 'nt':
            return False
        try:
            import winreg as imported_winreg  # type: ignore[import-not-found]
        except ImportError:
            return False
        winreg = imported_winreg

    try:
        with winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_CURRENT_USER,  # type: ignore[attr-defined]
            UNINSTALL_KEY,
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,  # type: ignore[attr-defined]
        ) as key:
            owner, _ = winreg.QueryValueEx(key, 'InstallLocation')  # type: ignore[attr-defined]
            if not _same_path(owner, base.resolve()):
                return False
            current, _ = winreg.QueryValueEx(key, 'DisplayVersion')  # type: ignore[attr-defined]
            if current == version:
                return False
            winreg.SetValueEx(  # type: ignore[attr-defined]
                key, 'DisplayVersion', 0, winreg.REG_SZ, version,  # type: ignore[attr-defined]
            )
            return True
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False
