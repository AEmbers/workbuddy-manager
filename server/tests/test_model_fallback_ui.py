"""v2.8.6 模型限流降级链的前端接线守卫。"""
from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
LAYOUT = ROOT / 'web' / 'app' / '(main)' / 'settings' / 'layout.tsx'
API = ROOT / 'web' / 'lib' / 'api.ts'


class ModelFallbackUiTest(unittest.TestCase):
    def test_settings_api_is_wired(self) -> None:
        source = API.read_text(encoding='utf-8')
        self.assertIn('/api/settings/model-fallbacks', source)
        self.assertIn('modelFallbacks:', source)
        self.assertIn('saveModelFallbacks:', source)

    def test_models_tab_edits_bounded_chains(self) -> None:
        source = LAYOUT.read_text(encoding='utf-8')
        self.assertIn("modelFallbacks: () => settingsApi.modelFallbacks()", source)
        self.assertIn("fallbackTitle", source)
        self.assertIn('chain.length >= 3', source)
        self.assertIn('fallbackPrimary.trim() === fallbackTarget.trim()', source)
        self.assertIn("[primary, ...chain].join(' → ')", source)

    def test_save_is_optimistic_and_reloads(self) -> None:
        source = LAYOUT.read_text(encoding='utf-8')
        start = source.index('async function saveModelFallbacks')
        block = source[start:start + 600]
        self.assertIn('setOptimisticFallbacks(next)', block)
        self.assertIn('await settingsApi.saveModelFallbacks(next)', block)
        self.assertIn('if (await reload()) setOptimisticFallbacks(null)', block)


if __name__ == '__main__':
    unittest.main()
