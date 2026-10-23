"""Responses image-budget retry integration.

Only the upstream HTTP client is faked.  Requests still pass through the real
Responses conversion, authorization, budget mutation, logging, and response
translation paths.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402
from server.routers import responses as R  # noqa: E402


_IMAGE_LIMIT_ERROR = {
    'error': {
        'message': (
            "prompt is too long: 100001 tokens > 100000 maximum "
            "(request has images)"
        ),
    },
}

_TEXT_LIMIT_ERROR = {
    'error': {
        'message': "prompt is too long: 1048577 tokens > 1048576 maximum",
    },
}

_IMAGE_LIMIT_ERROR_120K = {
    'error': {
        'message': "prompt is too long: 120001 tokens > 100000 maximum",
    },
}


def _image_data_url() -> str:
    image = Image.effect_noise((192, 192), 100).convert('RGB')
    output = io.BytesIO()
    image.save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')


def _image_request(*, stream: bool = False) -> dict:
    return {
        'model': 'glm-5.2',
        'stream': stream,
        'input': [{
            'type': 'message',
            'role': 'user',
            'content': [
                {'type': 'input_text', 'text': 'inspect this screenshot'},
                {'type': 'input_image', 'image_url': _image_data_url()},
            ],
        }],
    }


def _payload_images(payload: dict) -> list[str]:
    images: list[str] = []
    for message in payload.get('messages') or []:
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get('type') != 'image_url':
                continue
            value = block.get('image_url')
            url = value.get('url') if isinstance(value, dict) else value
            if isinstance(url, str):
                images.append(url)
    return images


class _Response:
    def __init__(
        self,
        status_code: int,
        data: dict | None = None,
        *,
        stream_body: bytes = b'',
    ) -> None:
        self.status_code = status_code
        self._data = data
        self._stream_body = stream_body
        self.headers = {'content-type': 'text/event-stream' if stream_body else 'application/json'}
        self.text = json.dumps(data, ensure_ascii=False) if data is not None else ''

    def json(self) -> dict:
        if self._data is None:
            raise ValueError('no json body')
        return self._data

    async def aread(self) -> bytes:
        return self.text.encode()

    async def aiter_bytes(self):
        if self._stream_body:
            yield self._stream_body

    async def aclose(self) -> None:
        return None


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def build_request(self, method: str, url: str, **kwargs) -> object:
        self.requests.append(kwargs['json'])
        return object()

    async def send(self, request: object, stream: bool = True) -> _Response:
        return self.responses.pop(0)

    async def post(self, url: str, *, json: dict, **kwargs) -> _Response:
        self.requests.append(json)
        return self.responses.pop(0)

    async def aclose(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class ResponsesImageBudgetEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'r.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{
                'username': 'admin',
                'role': 'admin',
                'pwd_hash': security.make_hash('p'),
            }],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)
        self.token = keysvc.create_key('t')['key']
        self._image_dir = Path(self._tmp.name) / 'images'

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        self._tmp.cleanup()

    @contextmanager
    def _upstream(self, responses: list[_Response]):
        fake_client = _Client(responses)
        fake_upstream = {
            'base_url': 'http://mock-upstream.invalid',
            'api_key': 'test-only',
            'is_default': False,
        }
        with (
            mock.patch.object(config, 'WB2API_BASE', 'http://127.0.0.1:1'),
            mock.patch.object(config, 'http_client', lambda *a, **k: fake_client),
            mock.patch.object(R.upstreamsvc, 'resolve_for_key', return_value=fake_upstream),
            mock.patch.object(
                R.image_budget,
                '_read_settings',
                # The first pass stops at quality 90; the force pass targets a
                # lower quality and must therefore send a smaller image.
                return_value=(1000, 1500, 2000, 30, 2 * 1024 * 1024),
            ),
            mock.patch.object(
                R.image_budget,
                '_resolve_data_dir',
                return_value=self._image_dir,
            ),
        ):
            yield fake_client

    def _post(self, payload: dict):
        return self.client.post(
            '/v1/responses',
            headers={'Authorization': f'Bearer {self.token}'},
            json=payload,
        )

    def test_image_budget_error_retries_once_with_smaller_image(self) -> None:
        success = {
            'choices': [{
                'index': 0,
                'message': {'role': 'assistant', 'content': 'ok'},
                'finish_reason': 'stop',
            }],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 1},
        }
        with self._upstream([
            _Response(400, _IMAGE_LIMIT_ERROR),
            _Response(200, success),
        ]) as upstream:
            response = self._post(_image_request())

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['output_text'], 'ok')
        self.assertEqual(len(upstream.requests), 2)
        first = _payload_images(upstream.requests[0])
        second = _payload_images(upstream.requests[1])
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertLess(len(second[0]), len(first[0]))

    def test_second_image_budget_error_is_returned_without_third_try(self) -> None:
        with self._upstream([
            _Response(400, _IMAGE_LIMIT_ERROR),
            _Response(400, _IMAGE_LIMIT_ERROR),
        ]) as upstream:
            response = self._post(_image_request())

        self.assertEqual(response.status_code, 400)
        self.assertIn('100001', response.text)
        self.assertEqual(len(upstream.requests), 2)
        self.assertLess(
            len(_payload_images(upstream.requests[1])[0]),
            len(_payload_images(upstream.requests[0])[0]),
        )

    def test_no_image_100k_error_is_not_retried(self) -> None:
        with self._upstream([_Response(400, _IMAGE_LIMIT_ERROR)]) as upstream:
            response = self._post({'model': 'glm-5.2', 'input': 'text only'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(upstream.requests), 1)

    def test_text_context_error_is_not_misidentified_as_image_budget(self) -> None:
        with self._upstream([_Response(400, _TEXT_LIMIT_ERROR)]) as upstream:
            response = self._post(_image_request())

        self.assertEqual(response.status_code, 400)
        self.assertIn('1048576 maximum', response.text)
        self.assertEqual(len(upstream.requests), 1)

    def test_any_image_overrun_above_the_actual_value_retries(self) -> None:
        success = {
            'choices': [{
                'index': 0,
                'message': {'role': 'assistant', 'content': 'ok'},
                'finish_reason': 'stop',
            }],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 1},
        }
        with self._upstream([
            _Response(400, _IMAGE_LIMIT_ERROR_120K),
            _Response(200, success),
        ]) as upstream:
            response = self._post(_image_request())

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(upstream.requests), 2)

    def test_image_budget_parser_accepts_escaped_and_wrapped_limits(self) -> None:
        self.assertTrue(R._is_image_budget_rejection(
            400, r"100001 tokens \u003e 100000 maximum",
        ))
        self.assertTrue(R._is_image_budget_rejection(
            413, "135000 tokens > 100000 maximum",
        ))
        self.assertFalse(R._is_image_budget_rejection(
            400, "100001 tokens > 200000 maximum",
        ))
        self.assertFalse(R._is_image_budget_rejection(
            503, "100001 tokens > 100000 maximum",
        ))

    def test_structured_output_without_input_is_still_rejected(self) -> None:
        with self._upstream([]) as upstream:
            response = self._post({
                'model': 'glm-5.2',
                'text': {'format': {'type': 'json_object'}},
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn('empty_input', response.text)
        self.assertEqual(len(upstream.requests), 0)

    def test_streaming_image_budget_error_retries_before_sse_starts(self) -> None:
        stream_body = b''.join([
            b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n',
            (
                b'data: {"choices":[{"index":0,"delta":{},'
                b'"finish_reason":"stop"}],"usage":{"prompt_tokens":3,'
                b'"completion_tokens":1}}\n\n'
            ),
        ])
        with self._upstream([
            _Response(400, _IMAGE_LIMIT_ERROR),
            _Response(200, stream_body=stream_body),
        ]) as upstream:
            response = self._post(_image_request(stream=True))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('response.completed', response.text)
        self.assertIn('"delta": "ok"', response.text)
        self.assertEqual(len(upstream.requests), 2)
        self.assertLess(
            len(_payload_images(upstream.requests[1])[0]),
            len(_payload_images(upstream.requests[0])[0]),
        )

    def test_midstream_image_budget_error_does_not_restart_stream(self) -> None:
        stream_body = (
            b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            b'data: {"error":{"message":"100001 tokens > 100000 maximum"}}\n\n'
        )
        with self._upstream([
            _Response(200, stream_body=stream_body),
        ]) as upstream:
            response = self._post(_image_request(stream=True))

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('response.failed', response.text)
        self.assertIn('100001', response.text)
        self.assertEqual(len(upstream.requests), 1)


if __name__ == '__main__':
    unittest.main()
