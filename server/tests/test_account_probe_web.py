"""矩阵探测前端纯函数与防重复点击守卫。"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'account-probe.test.mjs'
_COMPONENT = (_ROOT / 'web' / 'components' / 'common' / 'accounts'
              / 'AccountProbeMatrixDialog.tsx')
_NODE = shutil.which('node')


def _node_major() -> int | None:
    if not _NODE:
        return None
    try:
        out = subprocess.run([_NODE, '--version'], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        return int(out.lstrip('v').split('.')[0])
    except Exception:  # noqa: BLE001
        return None


class ProbeMatrixWebBehaviourTest(unittest.TestCase):
    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless((_node_major() or 0) >= 22,
                         '需要 node ≥ 22（type stripping）')
    def test_merge_stats_and_realm_statuses(self) -> None:
        proc = subprocess.run(
            [_NODE, '--experimental-strip-types', str(_SCRIPT)],
            capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
        )
        output = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn('all passed', output)


class ProbeMatrixFrontendGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.code = re.sub(
            r'/\*.*?\*/', '', _COMPONENT.read_text(encoding='utf-8'), flags=re.S)

    def test_start_has_function_and_button_guards(self) -> None:
        self.assertIn('if (!canStartProbe(', self.code,
                      'start() 入口必须再次检查忙碌/活动任务，防回车或脚本重复提交')
        self.assertIn('disabled={!canStartProbe(', self.code,
                      '开始按钮必须由同一判据禁用')

    def test_running_job_cannot_be_closed_without_cancel(self) -> None:
        self.assertIn('if (!next && running)', self.code)
        self.assertIn('probeMatrixCloseRunning', self.code)

    def test_result_lookup_uses_realm(self) -> None:
        self.assertIn('probeCellResult(', self.code)
        self.assertIn('account.realm', self.code)

    def test_polling_is_non_overlapping_and_recovers_from_404(self) -> None:
        self.assertNotIn('setInterval', self.code)
        self.assertIn('inFlight', self.code)
        self.assertIn("probePollDecision(null, error)", self.code)
        self.assertIn("decision === 'lost'", self.code)
        self.assertIn("decision === 'denied'", self.code)
        self.assertIn('setJob(null)', self.code)
        self.assertIn('sessionStorage.removeItem(JOB_STORAGE_KEY)', self.code)

    def test_selection_limit_has_an_explicit_explanation(self) -> None:
        self.assertIn('probeSelectionLimit(', self.code)
        self.assertIn('probeMatrixLimitAccounts', self.code)
        self.assertIn('probeMatrixLimitModels', self.code)


if __name__ == '__main__':
    unittest.main()
