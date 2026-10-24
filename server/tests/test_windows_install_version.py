"""Windows 一键更新后刷新卸载项显示版本的回归测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.services import windows_install


class _FakeWinReg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_WRITE = 2
    REG_SZ = 1

    def __init__(self, values: dict[str, object] | None):
        self.values = dict(values or {})
        self.created = self.values is not None
        self.writes: list[tuple[str, object]] = []

    def OpenKey(self, _hive, _path, _reserved, _access):
        if not self.created:
            raise FileNotFoundError
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name], self.REG_SZ

    def SetValueEx(self, _key, name, _reserved, _kind, value):
        self.writes.append((name, value))
        self.values[name] = value


class RefreshUninstallVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / '.version').write_text('v2.8.6\n', encoding='utf-8')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _refresh(self, values: dict[str, object] | None) -> tuple[bool, _FakeWinReg]:
        winreg = _FakeWinReg(values)
        changed = windows_install.refresh_uninstall_version(
            self.root, winreg_module=winreg,
        )
        return changed, winreg

    def test_stale_version_is_updated_for_the_owned_install(self) -> None:
        changed, winreg = self._refresh({
            'InstallLocation': str(self.root),
            'DisplayVersion': 'v2.8.5',
        })
        self.assertTrue(changed)
        self.assertEqual(winreg.writes, [('DisplayVersion', 'v2.8.6')])

    def test_current_version_is_idempotent(self) -> None:
        changed, winreg = self._refresh({
            'InstallLocation': str(self.root),
            'DisplayVersion': 'v2.8.6',
        })
        self.assertFalse(changed)
        self.assertEqual(winreg.writes, [])

    def test_another_install_is_never_modified(self) -> None:
        changed, winreg = self._refresh({
            'InstallLocation': r'D:\another\WorkBuddyManager',
            'DisplayVersion': 'v2.8.1',
        })
        self.assertFalse(changed)
        self.assertEqual(winreg.writes, [])

    def test_missing_registry_entry_is_left_alone(self) -> None:
        changed, winreg = self._refresh(None)
        self.assertFalse(changed)
        self.assertEqual(winreg.writes, [])

    def test_missing_or_blank_version_does_not_open_registry(self) -> None:
        (self.root / '.version').write_text('  \n', encoding='utf-8')
        fake = mock.Mock()
        self.assertFalse(windows_install.refresh_uninstall_version(
            self.root, winreg_module=fake,
        ))
        fake.OpenKey.assert_not_called()


if __name__ == '__main__':
    unittest.main()
