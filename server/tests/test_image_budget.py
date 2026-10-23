from __future__ import annotations

import base64
import copy
import io
import json
import os
import random
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services.image_budget import _collect_records, apply_image_budget


def _noise_image(
    size: tuple[int, int],
    *,
    seed: int,
    image_format: str = "PNG",
    exif: Image.Exif | None = None,
) -> str:
    width, height = size
    source = Image.frombytes(
        "RGB", size, random.Random(seed).randbytes(width * height * 3)
    )
    output = io.BytesIO()
    options = {"format": image_format}
    if exif is not None:
        options["exif"] = exif
    source.save(output, **options)
    mime = "jpeg" if image_format.upper() == "JPEG" else image_format.lower()
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"


def _block(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _payload(*messages: dict, tools: list[dict] | None = None) -> dict:
    payload = {"model": "fixture", "messages": list(messages)}
    if tools is not None:
        payload["tools"] = tools
    return payload


def _view_image_tool() -> list[dict]:
    return [{"type": "function", "function": {"name": "view_image", "parameters": {}}}]


class ImageBudgetTests(unittest.TestCase):
    def _env(self, **overrides: str):
        values = {
            "WB_IMAGE_BUDGET_TRIGGER": "1",
            "WB_IMAGE_BUDGET_TARGET": "1",
            "WB_IMAGE_BUDGET_HARD": "1000000",
            "WB_IMAGE_BUDGET_TTL_DAYS": "30",
            "WB_IMAGE_BUDGET_CACHE_MB": "16",
            "WB_IMAGE_BUDGET_PLACEHOLDERS": "1",
        }
        values.update(overrides)
        return patch.dict(os.environ, values, clear=False)

    def test_below_trigger_is_byte_for_byte_passthrough(self) -> None:
        url = _noise_image((32, 24), seed=1)
        payload = _payload({"role": "user", "content": [_text("hello"), _block(url)]})
        original = copy.deepcopy(payload)
        with tempfile.TemporaryDirectory() as temporary, self._env(
            WB_IMAGE_BUDGET_TRIGGER="100000"
        ):
            stats = apply_image_budget(payload, data_dir=temporary)

            self.assertEqual(payload, original)
            self.assertFalse((Path(temporary) / "image_refs").exists())
        self.assertEqual(stats.image_count, 1)
        self.assertEqual(stats.compressible_count, 1)
        self.assertEqual(stats.estimated_before, stats.estimated_after)
        self.assertEqual(stats.compressed, 0)
        self.assertEqual(stats.placeholders, 0)

    def test_old_image_is_compressed_before_current_image(self) -> None:
        old_url = _noise_image((512, 512), seed=2)
        new_url = _noise_image((8, 8), seed=3)
        payload = _payload(
            {"role": "user", "content": [_block(old_url)]},
            {"role": "assistant", "content": [_text("working")]},
            {"role": "user", "content": [_text("next")]},
            {"role": "assistant", "content": [_text("done")]},
            {"role": "user", "content": [_text("current"), _block(new_url)]},
        )
        before = copy.deepcopy(payload)
        with tempfile.TemporaryDirectory() as temporary, self._env(
            WB_IMAGE_BUDGET_TARGET="5000"
        ):
            stats = apply_image_budget(payload, data_dir=temporary)

        self.assertGreater(stats.estimated_before, 5000)
        self.assertGreaterEqual(stats.compressed, 1)
        self.assertNotEqual(
            payload["messages"][0]["content"][0]["image_url"]["url"], old_url
        )
        self.assertEqual(
            payload["messages"][-1]["content"][-1]["image_url"]["url"], new_url
        )
        # Sanity check that the fixture actually started above the target.
        self.assertNotEqual(payload, before)

    def test_force_mode_compresses_even_below_the_normal_trigger(self) -> None:
        url = _noise_image((320, 240), seed=25)
        payload = _payload({"role": "user", "content": [_block(url)]})
        original = copy.deepcopy(payload)
        with tempfile.TemporaryDirectory() as temporary, self._env(
            WB_IMAGE_BUDGET_TRIGGER="100000000",
            WB_IMAGE_BUDGET_TARGET="5000",
        ):
            stats = apply_image_budget(
                payload, force_aggressive=True, data_dir=temporary
            )

        self.assertGreater(stats.compressed, 0)
        self.assertLess(stats.estimated_after, stats.estimated_before)
        self.assertNotEqual(payload, original)

    def test_lifted_tool_images_stay_in_current_user_turn(self) -> None:
        old_url = _noise_image((512, 512), seed=21)
        tool_url_1 = _noise_image((8, 8), seed=22)
        tool_url_2 = _noise_image((8, 8), seed=23)
        latest_url = _noise_image((8, 8), seed=24)
        payload = _payload(
            {"role": "user", "content": [_block(old_url)]},
            {
                "role": "user",
                "content": [_text("latest"), _block(latest_url)],
            },
            {
                "role": "assistant",
                "content": [_text("calling tools")],
                "tool_calls": [
                    {"id": "call-1", "type": "function"},
                    {"id": "call-2", "type": "function"},
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "first"},
            {"role": "tool", "tool_call_id": "call-2", "content": "second"},
            {
                "role": "user",
                "content": [_block(tool_url_1), _block(tool_url_2)],
            },
            {"role": "assistant", "content": [_text("tools returned images")]},
        )

        records = _collect_records(payload["messages"])
        self.assertEqual([record.distance for record in records], [1, 0, 0, 0])

        with tempfile.TemporaryDirectory() as temporary, self._env(
            WB_IMAGE_BUDGET_TARGET="5000"
        ):
            stats = apply_image_budget(payload, data_dir=temporary)

        self.assertGreaterEqual(stats.compressed, 1)
        self.assertNotEqual(
            payload["messages"][0]["content"][0]["image_url"]["url"], old_url
        )
        self.assertEqual(
            [
                block["image_url"]["url"]
                for block in payload["messages"][5]["content"]
            ],
            [tool_url_1, tool_url_2],
        )
        self.assertEqual(
            payload["messages"][1]["content"][-1]["image_url"]["url"],
            latest_url,
        )

    def test_no_view_image_never_produces_a_placeholder(self) -> None:
        url = _noise_image((160, 120), seed=4)
        payload = _payload({"role": "user", "content": [_block(url)]})
        with tempfile.TemporaryDirectory() as temporary, self._env(
            WB_IMAGE_BUDGET_TARGET="5000",
            WB_IMAGE_BUDGET_HARD="5000",
        ):
            stats = apply_image_budget(
                payload, force_aggressive=True, data_dir=temporary
            )

        block = payload["messages"][0]["content"][0]
        self.assertEqual(stats.placeholders, 0)
        self.assertLessEqual(stats.estimated_after, 5000)
        self.assertEqual(block["type"], "image_url")
        self.assertNotIn("image_ref", json.dumps(payload))

    def test_nested_schema_default_named_view_image_is_not_a_tool(self) -> None:
        url = _noise_image((160, 120), seed=41)
        payload = _payload(
            {"role": "user", "content": [_block(url)]},
            tools=[{
                "type": "function",
                "function": {
                    "name": "other",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "choice": {"default": {"name": "view_image"}},
                        },
                    },
                },
            }],
        )
        with tempfile.TemporaryDirectory() as temporary, self._env():
            stats = apply_image_budget(payload, data_dir=temporary)

        self.assertEqual(stats.placeholders, 0)
        self.assertEqual(
            payload["messages"][0]["content"][0]["type"], "image_url"
        )

    def test_unusable_cache_directory_degrades_to_passthrough(self) -> None:
        url = _noise_image((160, 120), seed=42)
        payload = _payload({"role": "user", "content": [_block(url)]})
        with tempfile.TemporaryDirectory() as temporary:
            unusable = Path(temporary) / "not-a-directory"
            unusable.write_text("occupied", encoding="utf-8")
            with self._env():
                stats = apply_image_budget(payload, data_dir=unusable)

        self.assertEqual(stats.compressed, 0)
        self.assertEqual(stats.placeholders, 0)
        self.assertEqual(
            payload["messages"][0]["content"][0]["image_url"]["url"], url
        )

    def test_view_image_enables_a_recoverable_placeholder(self) -> None:
        url = _noise_image((160, 120), seed=5)
        payload = _payload(
            {"role": "user", "content": [_block(url)]},
            tools=_view_image_tool(),
        )
        with tempfile.TemporaryDirectory() as temporary, self._env():
            stats = apply_image_budget(payload, data_dir=temporary)

            block = payload["messages"][0]["content"][0]
            self.assertEqual(block["type"], "text")
            self.assertIn("<image_ref ", block["text"])
            self.assertIn("view_image", block["text"])
            self.assertNotIn("base64", block["text"])
            path_match = re.search(r'path="([^"]+)"', block["text"])
            self.assertIsNotNone(path_match)
            self.assertTrue(Path(path_match.group(1)).is_file())
        self.assertEqual(stats.placeholders, 1)

    def test_container_auto_mode_keeps_compressing_without_path_refs(self) -> None:
        url = _noise_image((160, 120), seed=51)
        payload = _payload(
            {"role": "user", "content": [_block(url)]},
            tools=_view_image_tool(),
        )
        with tempfile.TemporaryDirectory() as temporary, self._env(
            WB_IMAGE_BUDGET_PLACEHOLDERS="auto",
        ), patch(
            "server.services.updater.in_container", return_value=True
        ):
            stats = apply_image_budget(payload, data_dir=temporary)

        self.assertEqual(stats.placeholders, 0)
        self.assertEqual(
            payload["messages"][0]["content"][0]["type"], "image_url"
        )

    def test_same_sha_reuses_cached_profile(self) -> None:
        url = _noise_image((512, 512), seed=6)
        first = _payload({"role": "user", "content": [_block(url)]})
        second = copy.deepcopy(first)
        with tempfile.TemporaryDirectory() as temporary, self._env(
            WB_IMAGE_BUDGET_TARGET="5000"
        ):
            apply_image_budget(first, data_dir=temporary)
            cache_dir = next((Path(temporary) / "image_cache").iterdir())
            first_files = sorted(cache_dir.iterdir())
            first_inodes = {
                path.name: path.stat().st_ino for path in first_files
            }

            apply_image_budget(second, data_dir=temporary)
            second_files = sorted(cache_dir.iterdir())
            second_inodes = {
                path.name: path.stat().st_ino for path in second_files
            }

        self.assertEqual([path.name for path in first_files], [path.name for path in second_files])
        self.assertEqual(first_inodes, second_inodes)
        self.assertEqual(
            first["messages"][0]["content"][0],
            second["messages"][0]["content"][0],
        )

    def test_atomic_cache_and_exif_orientation(self) -> None:
        exif = Image.Exif()
        exif[274] = 6  # Rotate 90 degrees clockwise when displayed.
        url = _noise_image((40, 20), seed=7, image_format="JPEG", exif=exif)
        payload = _payload(
            {"role": "user", "content": [_block(url)]},
            tools=_view_image_tool(),
        )
        with tempfile.TemporaryDirectory() as temporary, self._env():
            apply_image_budget(payload, data_dir=temporary)

            root = Path(temporary)
            cache_files = list((root / "image_cache").rglob("*"))
            compressed = [path for path in cache_files if path.is_file()]
            self.assertTrue(compressed)
            with Image.open(compressed[0]) as image:
                self.assertGreater(image.height, image.width)
            self.assertFalse(list(root.rglob(".tmp-*")))

            placeholder = payload["messages"][0]["content"][0]["text"]
            self.assertIn('width="20"', placeholder)
            self.assertIn('height="40"', placeholder)

    def test_message_tool_and_block_order_are_unchanged(self) -> None:
        url = _noise_image((160, 120), seed=8)
        payload = _payload(
            {
                "role": "assistant",
                "content": [_text("call")],
                "tool_calls": [{"id": "call-1", "type": "function"}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
            {
                "role": "user",
                "content": [_text("before"), _block(url), _text("after")],
            },
            tools=_view_image_tool(),
        )
        roles_before = [message["role"] for message in payload["messages"]]
        ids_before = [
            message.get("tool_calls", [{}])[0].get("id")
            if message.get("tool_calls")
            else message.get("tool_call_id")
            for message in payload["messages"]
        ]
        with tempfile.TemporaryDirectory() as temporary, self._env():
            apply_image_budget(payload, data_dir=temporary)

        self.assertEqual(
            [message["role"] for message in payload["messages"]], roles_before
        )
        self.assertEqual(
            [
                message.get("tool_calls", [{}])[0].get("id")
                if message.get("tool_calls")
                else message.get("tool_call_id")
                for message in payload["messages"]
            ],
            ids_before,
        )
        content = payload["messages"][-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "before"})
        self.assertEqual(content[-1], {"type": "text", "text": "after"})
        self.assertEqual(content[1]["type"], "text")

    def test_remote_url_is_preserved_and_not_cached(self) -> None:
        remote = {"type": "image_url", "image_url": {"url": "https://x.example/a.png"}}
        payload = _payload(
            {"role": "user", "content": [remote]},
            tools=_view_image_tool(),
        )
        with tempfile.TemporaryDirectory() as temporary, self._env():
            stats = apply_image_budget(payload, data_dir=temporary)

            self.assertEqual(payload["messages"][0]["content"][0], remote)
            self.assertFalse((Path(temporary) / "image_refs").exists())
        self.assertEqual(stats.image_count, 1)
        self.assertEqual(stats.compressible_count, 0)
        self.assertEqual(stats.estimated_after, 0)


if __name__ == "__main__":
    unittest.main()
