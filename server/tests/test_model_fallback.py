"""Model-fallback policy and end-to-end protocol coverage."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security, upstreamsvc  # noqa: E402
from server.services import modelfallback  # noqa: E402


PRIMARY = 'primary-model'
FALLBACK = 'fallback-model'
NO_HEALTHY = {
    'error': {
        'message': 'no healthy account available in pool',
        'type': 'api_error',
        'code': 'no_healthy_account',
    },
}
RATE_6004 = {
    'error': {
        'message': '您的使用量已超出频率限制',
        'type': 'rate_limit_error',
        'code': 6004,
    },
}
CHAT_OK = {
    'id': 'chat_1',
    'object': 'chat.completion',
    'choices': [{
        'index': 0,
        'message': {'role': 'assistant', 'content': 'ok'},
        'finish_reason': 'stop',
    }],
    'usage': {'prompt_tokens': 2, 'completion_tokens': 1},
}
STREAM_OK = (
    'data: ' + json.dumps({
        'choices': [{'index': 0, 'delta': {'content': 'ok'}}],
    }) + '\n\n'
    + 'data: ' + json.dumps({
        'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 2, 'completion_tokens': 1},
    }) + '\n\n'
    + 'data: [DONE]\n\n'
)


def _key(**changes) -> dict:
    value = {
        'id': 1,
        'enabled': True,
        'expires_at': 0,
        'quota': 0,
        'quota_credit': 0,
        'used_tokens': 0,
        'used_credit': 0,
        'ip_allowlist': [],
        'max_ips': 0,
        'realm': '',
        'models': [],
    }
    value.update(changes)
    return value


class PolicyTest(unittest.TestCase):
    def test_setting_validation_deduplicates_and_caps_chain(self) -> None:
        clean = modelfallback.validate({
            PRIMARY: [FALLBACK, FALLBACK, 'third'],
        })
        self.assertEqual(clean, {PRIMARY: [FALLBACK, 'third']})
        for bad in (
            None,
            {PRIMARY: 'not-a-list'},
            {PRIMARY: []},
            {PRIMARY: [PRIMARY]},
            {PRIMARY: ['a', 'b', 'c', 'd', 'e', 'f']},
            {PRIMARY: ['has space']},
            {PRIMARY: ['*']},
            {'': [FALLBACK]},
        ):
            with self.subTest(raw=bad):
                with self.assertRaises(ValueError):
                    modelfallback.validate(bad)

    def test_only_explicit_exhaustion_signals_trigger(self) -> None:
        self.assertTrue(modelfallback.is_fallback_trigger(429, RATE_6004))
        self.assertTrue(modelfallback.is_fallback_trigger(
            429, {'error': {'code': 'rate_limit_exceeded'}},
        ))
        self.assertTrue(modelfallback.is_fallback_trigger(
            429, {'error': {'message': 'too many requests'}},
        ))
        self.assertTrue(modelfallback.is_fallback_trigger(
            429, None, 'data: {"code":6004,"msg":"limited"}\n\n',
        ))
        self.assertTrue(modelfallback.is_fallback_trigger(503, NO_HEALTHY))
        self.assertFalse(modelfallback.is_fallback_trigger(
            429, {'error': {'code': 'quota_exhausted'}}, 'quota exhausted',
        ))
        self.assertFalse(modelfallback.is_fallback_trigger(400, RATE_6004))
        self.assertFalse(modelfallback.is_fallback_trigger(
            503, {'error': {'code': 'upstream_unavailable'}},
        ))

    def test_retry_after_prefers_headers(self) -> None:
        headers = {
            'Retry-After': '17',
            'retry-after-ms': '17000',
            'X-RateLimit-Reset': '1770000000',
        }
        got = modelfallback.failure_headers(
            headers, RATE_6004,
            'reset at 2026-10-24 20:35:49 UTC+8',
        )
        self.assertEqual(got['Retry-After'], '17')
        self.assertEqual(got['retry-after-ms'], '17000')
        self.assertEqual(got['x-ratelimit-reset'], '1770000000')
        self.assertEqual(got['X-Should-Retry'], 'false')

    def test_retry_after_supports_ms_and_http_date(self) -> None:
        now = datetime(2015, 10, 21, 7, 25, tzinfo=timezone.utc)
        self.assertEqual(
            modelfallback.retry_after_seconds(
                {'retry-after-ms': '250'}, now=now,
            ),
            1,
        )
        self.assertEqual(
            modelfallback.retry_after_seconds(
                {'Retry-After': 'Wed, 21 Oct 2015 07:27:30 GMT'}, now=now,
            ),
            150,
        )

    def test_cooldown_scope_separates_upstream_credentials(self) -> None:
        a = modelfallback.scope_for('https://upstream.test/', 'pool-a-secret')
        b = modelfallback.scope_for('https://upstream.test', 'pool-b-secret')
        same = modelfallback.scope_for('https://upstream.test', 'pool-a-secret')
        self.assertEqual(a, same)
        self.assertNotEqual(a, b)
        self.assertNotIn('pool-a-secret', a)

    def test_tencent_utc_plus_8_reset_is_converted_not_treated_as_utc(self) -> None:
        now = datetime(2026, 10, 24, 11, 35, 49, tzinfo=timezone.utc)
        message = '限流将在 2026-10-24 20:35:49 UTC+8 恢复'
        self.assertEqual(modelfallback._seconds_until(message, now=now), 3600)
        message = 'reset at 2026-10-24 20:35:49 UTC+08:00'
        self.assertEqual(modelfallback._seconds_until(message, now=now), 3600)

    def test_default_chain_applies_to_any_primary(self) -> None:
        # '*' 默认链：任何未单独配置链的模型在额度耗尽后都走它（调谁谁用完就进链）。
        table = {'*': ['fallback-a', 'fallback-b']}
        key = _key(realm='cn', models=['kimi-k3', 'fallback-a', 'fallback-b'])
        plan = modelfallback.build_plan('kimi-k3', 'kimi-k3', table=table, key=key)
        self.assertEqual(plan, ['kimi-k3', 'fallback-a', 'fallback-b'])

    def test_default_chain_skips_called_model_itself(self) -> None:
        # 默认链里含被调用模型自身时剔除（选 DeepSeek 用完 → 从链内下一站走）。
        table = {'*': [PRIMARY, FALLBACK]}
        key = _key(realm='cn', models=[PRIMARY, FALLBACK])
        plan = modelfallback.build_plan(PRIMARY, PRIMARY, table=table, key=key)
        self.assertEqual(plan, [PRIMARY, FALLBACK])

    def test_explicit_chain_overrides_default(self) -> None:
        table = {PRIMARY: [FALLBACK], '*': ['other']}
        key = _key(realm='cn', models=[PRIMARY, FALLBACK])
        plan = modelfallback.build_plan(PRIMARY, PRIMARY, table=table, key=key)
        self.assertEqual(plan, [PRIMARY, FALLBACK])

    def test_validate_accepts_default_chain_key(self) -> None:
        self.assertEqual(
            modelfallback.validate({'*': [FALLBACK]}),
            {'*': [FALLBACK]},
        )
        # '*' 不能作为链内候选（仍然只允许可见 ASCII 具体模型名）。
        with self.assertRaises(ValueError):
            modelfallback.validate({PRIMARY: ['*']})

    def test_chain_obeys_realm_and_whitelist(self) -> None:
        table = {PRIMARY: ['global:wrong-realm', 'blocked', 'allowed']}
        key = _key(realm='cn', models=[PRIMARY, 'global:wrong-realm', 'allowed'])
        plan = modelfallback.build_plan(
            PRIMARY, PRIMARY, table=table, key=key,
        )
        self.assertEqual(plan, [PRIMARY, 'allowed'])

        key = _key(realm='cn', models=[PRIMARY, 'allowed'])
        plan = modelfallback.build_plan(
            PRIMARY, PRIMARY, table=table, key=key,
        )
        self.assertEqual(plan, [PRIMARY, 'allowed'])

        key = _key(realm='cn', models=[PRIMARY])
        self.assertEqual(
            modelfallback.build_plan(PRIMARY, PRIMARY, table=table, key=key),
            [PRIMARY],
        )

    def test_exact_alias_target_is_not_mapped_a_second_time(self) -> None:
        table = {'alias': ['candidate']}
        mapping = {'alias': 'exact-target', '*': 'wildcard-target'}
        plan = modelfallback.build_plan(
            'alias', 'exact-target',
            table=table, key=_key(), model_map=mapping,
        )
        self.assertEqual(plan, ['exact-target', 'wildcard-target'])


class _Resp:
    def __init__(
        self,
        status: int,
        body: object,
        *,
        headers: dict[str, str] | None = None,
        stream_bytes: bytes | None = None,
    ) -> None:
        self.status_code = status
        self._body = body
        self.text = (
            body if isinstance(body, str)
            else json.dumps(body, ensure_ascii=False)
        )
        self.headers = {
            'content-type': 'application/json',
            **(headers or {}),
        }
        self._stream_bytes = (
            stream_bytes if stream_bytes is not None else self.text.encode()
        )

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body

    async def aread(self) -> bytes:
        return self.text.encode()

    async def aiter_bytes(self):
        yield self._stream_bytes

    async def aclose(self) -> None:
        return None


class _Upstream:
    def __init__(self, responses: list[_Resp]) -> None:
        self.responses = deque(responses)
        self.models: list[str] = []

    def _take(self) -> _Resp:
        if not self.responses:
            raise AssertionError('unexpected extra upstream request')
        return self.responses.popleft()

    def build_request(self, _method, _url, **kwargs):
        self.models.append(kwargs['json']['model'])
        return object()

    async def send(self, _request, stream=True):
        return self._take()

    async def post(self, _url, **kwargs):
        self.models.append(kwargs['json']['model'])
        return self._take()

    async def aclose(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = cls._dir / 'fallback.db'
        config.USERS_FILE = cls._dir / 'users.json'
        config.STATIC_DIR = cls._dir / 'no-static'
        security.save_users({
            'secret': 'S',
            'users': [{
                'username': 'admin',
                'role': 'admin',
                'pwd_hash': security.make_hash('fallback-admin'),
            }],
            'api_keys': [],
        })
        db._conn = None
        db.connect()
        from server.main import app
        cls.app = app

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        modelfallback.reset_exhaustion_state()
        db.execute('DELETE FROM api_keys')
        db.execute('DELETE FROM upstreams')
        db.execute('DELETE FROM request_logs')
        db.execute("DELETE FROM settings WHERE key IN ('model_fallbacks', 'model_map')")
        self.admin = TestClient(self.app)
        response = self.admin.post('/api/login', json={
            'username': 'admin',
            'password': 'fallback-admin',
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.token = keysvc.create_key('fallback')['key']

    def tearDown(self) -> None:
        modelfallback.reset_exhaustion_state()

    def _call(
        self,
        protocol: str,
        *,
        stream: bool,
        upstreams: list[_Resp],
        token: str | None = None,
    ) -> tuple[object, _Upstream]:
        owner = _Upstream(upstreams)
        headers = {'Authorization': f'Bearer {token or self.token}'}
        if protocol == 'chat':
            path = '/v1/chat/completions'
            body = {
                'model': PRIMARY,
                'messages': [{'role': 'user', 'content': 'hi'}],
                'stream': stream,
            }
        elif protocol == 'responses':
            path = '/v1/responses'
            body = {'model': PRIMARY, 'input': 'hi', 'stream': stream}
        else:
            path = '/v1/messages'
            headers['anthropic-version'] = '2023-06-01'
            body = {
                'model': PRIMARY,
                'max_tokens': 8,
                'messages': [{'role': 'user', 'content': 'hi'}],
                'stream': stream,
            }
        with mock.patch.object(config, 'http_client', lambda *_a, **_k: owner):
            response = TestClient(self.app).post(path, headers=headers, json=body)
        return response, owner

    def _success_response(self, stream: bool) -> _Resp:
        if stream:
            return _Resp(200, '', stream_bytes=STREAM_OK.encode())
        return _Resp(200, CHAT_OK)

    def test_three_protocols_stream_and_non_stream_fall_back(self) -> None:
        for protocol in ('chat', 'responses', 'anthropic'):
            for stream in (False, True):
                with self.subTest(protocol=protocol, stream=stream):
                    modelfallback.reset_exhaustion_state()
                    db.execute('DELETE FROM request_logs')
                    db.set_setting('model_fallbacks', {
                        PRIMARY: [FALLBACK],
                    })
                    response, owner = self._call(
                        protocol,
                        stream=stream,
                        upstreams=[
                            _Resp(429, RATE_6004),
                            self._success_response(stream),
                        ],
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(owner.models, [PRIMARY, FALLBACK])
                    row = db.query_one(
                        'SELECT mapped_model FROM request_logs '
                        'ORDER BY id DESC LIMIT 1'
                    )
                    self.assertEqual(row['mapped_model'], FALLBACK)
                    if stream:
                        self.assertIn('ok', response.text)

    def test_no_chain_makes_exactly_one_request(self) -> None:
        response, owner = self._call(
            'chat', stream=False, upstreams=[_Resp(429, RATE_6004)],
        )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(owner.models, [PRIMARY])
        self.assertEqual(response.headers['x-should-retry'], 'false')

    def test_bare_final_failure_does_not_poison_next_request(self) -> None:
        first, first_owner = self._call(
            'chat',
            stream=False,
            upstreams=[_Resp(503, NO_HEALTHY)],
        )
        self.assertEqual(first.status_code, 503, first.text)
        self.assertEqual(first_owner.models, [PRIMARY])

        # A bare final failure has no reliable recovery time.  It must close
        # this request only, rather than make the next independent request hit
        # a synthetic cooldown and fail without contacting the upstream.
        second, second_owner = self._call(
            'chat',
            stream=False,
            upstreams=[_Resp(200, CHAT_OK)],
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second_owner.models, [PRIMARY])

    def test_immediate_client_retry_uses_local_cooldown(self) -> None:
        for protocol in ('chat', 'responses', 'anthropic'):
            with self.subTest(protocol=protocol):
                modelfallback.reset_exhaustion_state()
                db.execute('DELETE FROM request_logs')
                first, first_owner = self._call(
                    protocol,
                    stream=False,
                    upstreams=[
                        _Resp(429, RATE_6004, headers={'Retry-After': '30'}),
                    ],
                )
                self.assertEqual(first.status_code, 429, first.text)
                self.assertEqual(first_owner.models, [PRIMARY])

                # The upstream fake would fail if contacted. The second call
                # must stop at the process-local cooldown instead of scanning
                # all accounts again.
                second, second_owner = self._call(
                    protocol, stream=False, upstreams=[],
                )
                self.assertEqual(second.status_code, 429, second.text)
                self.assertEqual(second_owner.models, [])
                self.assertEqual(second.headers['retry-after'], '30')
                self.assertEqual(second.headers['x-should-retry'], 'false')
                row = db.query_one(
                    'SELECT status, model, mapped_model FROM request_logs '
                    'ORDER BY id DESC LIMIT 1'
                )
                self.assertEqual(row['status'], 429)
                self.assertEqual(row['model'], PRIMARY)
                self.assertEqual(row['mapped_model'], PRIMARY)

    def test_same_base_with_different_upstream_keys_has_separate_cooldowns(self) -> None:
        pool_a = upstreamsvc.create_upstream(
            'pool-a', 'https://same-upstream.test', 'internal-key-a',
        )
        pool_b = upstreamsvc.create_upstream(
            'pool-b', 'https://same-upstream.test', 'internal-key-b',
        )
        token_a = keysvc.create_key('pool-a-client', upstream_id=pool_a['id'])['key']
        token_b = keysvc.create_key('pool-b-client', upstream_id=pool_b['id'])['key']

        first, first_owner = self._call(
            'chat',
            stream=False,
            token=token_a,
            upstreams=[
                _Resp(429, RATE_6004, headers={'Retry-After': '30'}),
            ],
        )
        self.assertEqual(first.status_code, 429, first.text)
        self.assertEqual(first_owner.models, [PRIMARY])

        second, second_owner = self._call(
            'chat',
            stream=False,
            token=token_b,
            upstreams=[_Resp(200, CHAT_OK)],
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second_owner.models, [PRIMARY])

    def test_successful_fallback_is_preferred_while_primary_cools(self) -> None:
        db.set_setting('model_fallbacks', {PRIMARY: [FALLBACK]})
        first, first_owner = self._call(
            'chat',
            stream=False,
            upstreams=[_Resp(429, RATE_6004), _Resp(200, CHAT_OK)],
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first_owner.models, [PRIMARY, FALLBACK])

        second, second_owner = self._call(
            'chat', stream=False, upstreams=[_Resp(200, CHAT_OK)],
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second_owner.models, [FALLBACK])

    def test_chain_is_limited_to_four_physical_attempts(self) -> None:
        db.set_setting('model_fallbacks', {
            PRIMARY: ['f1', 'f2', 'f3'],
        })
        response, owner = self._call(
            'chat',
            stream=False,
            upstreams=[_Resp(429, RATE_6004) for _ in range(4)],
        )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(owner.models, [PRIMARY, 'f1', 'f2', 'f3'])

    def test_non_exhaustion_errors_never_fall_back(self) -> None:
        cases = (
            (400, {'error': {'code': 6004}}),
            (401, RATE_6004),
            (413, RATE_6004),
            (429, {'error': {'code': 'quota_exhausted'}}),
            (503, {'error': {'code': 'upstream_unavailable'}}),
            (400, {
                'error': {
                    'code': 'image_budget_exceeded',
                    'message': '120000 tokens > 100000 maximum',
                },
            }),
        )
        db.set_setting('model_fallbacks', {PRIMARY: [FALLBACK]})
        for status, body in cases:
            with self.subTest(status=status, body=body):
                response, owner = self._call(
                    'chat', stream=False, upstreams=[_Resp(status, body)],
                )
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(owner.models, [PRIMARY])

    def test_final_failure_preserves_status_and_safe_retry_headers(self) -> None:
        db.set_setting('model_fallbacks', {PRIMARY: [FALLBACK]})
        response, owner = self._call(
            'anthropic',
            stream=True,
            upstreams=[
                _Resp(429, RATE_6004, headers={'Retry-After': '9'}),
                _Resp(
                    503,
                    NO_HEALTHY,
                    headers={
                        'Retry-After': '12',
                        'retry-after-ms': '12000',
                        'x-ratelimit-reset': '1770000000',
                    },
                ),
            ],
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(owner.models, [PRIMARY, FALLBACK])
        self.assertEqual(response.headers['retry-after'], '12')
        self.assertEqual(response.headers['retry-after-ms'], '12000')
        self.assertEqual(response.headers['x-ratelimit-reset'], '1770000000')
        self.assertEqual(response.headers['x-should-retry'], 'false')
        self.assertNotIn('text/event-stream', response.headers['content-type'])

    def test_realm_and_whitelist_skip_unauthorized_candidates(self) -> None:
        token = keysvc.create_key(
            'restricted', realm='cn', models=[PRIMARY, 'allowed'],
        )['key']
        db.set_setting('model_fallbacks', {
            PRIMARY: ['global:wrong-realm', 'blocked', 'allowed'],
        })
        response, owner = self._call(
            'chat',
            stream=False,
            token=token,
            upstreams=[_Resp(429, RATE_6004), _Resp(200, CHAT_OK)],
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(owner.models, [PRIMARY, 'allowed'])

    def test_settings_endpoints_validate_and_persist(self) -> None:
        saved = self.admin.post('/api/settings/model-fallbacks', json={
            PRIMARY: [FALLBACK, FALLBACK],
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json(), {PRIMARY: [FALLBACK]})
        loaded = self.admin.get('/api/settings/model-fallbacks')
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json(), {PRIMARY: [FALLBACK]})
        bad = self.admin.post('/api/settings/model-fallbacks', json={
            PRIMARY: [PRIMARY],
        })
        self.assertEqual(bad.status_code, 400, bad.text)


if __name__ == '__main__':
    unittest.main()
