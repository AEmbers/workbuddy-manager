"""Custom/random API-key creation must be wired through the visible form."""

from __future__ import annotations

from pathlib import Path
import unittest


_ROOT = Path(__file__).resolve().parents[2]
_PAGE = _ROOT / "web" / "app" / "(main)" / "keys" / "page.tsx"


class CustomKeyUiTest(unittest.TestCase):
    def test_form_offers_both_key_sources_and_only_for_creation(self) -> None:
        source = _PAGE.read_text(encoding="utf-8")
        self.assertIn("keyMode: 'random' | 'custom'", source)
        self.assertIn("payload.key_mode = form.keyMode", source)
        self.assertIn("payload.custom_key = form.customKey.trim()", source)
        self.assertIn("value=\"random\"", source)
        self.assertIn("value=\"custom\"", source)
        self.assertIn("!editing && (", source)

    def test_custom_value_is_masked_and_can_be_revealed(self) -> None:
        source = _PAGE.read_text(encoding="utf-8")
        self.assertIn("showCustomKey ? 'text' : 'password'", source)
        self.assertIn("setShowCustomKey((value) => !value)", source)

    def test_client_side_validation_matches_backend_minimum(self) -> None:
        source = _PAGE.read_text(encoding="utf-8")
        self.assertIn("custom.length < 16", source)
        self.assertIn("/^[\\x21-\\x7E]+$/", source)


if __name__ == "__main__":
    unittest.main()
