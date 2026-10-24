"""请求日志可观测性：推理档位、客户端渠道与账号回填诊断（#267/#232/#233）。"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402
from server.routers import gateway, logs  # noqa: E402
from server.services import accountlog  # noqa: E402


def _temp_db(test: unittest.TestCase) -> None:
    test._tmp = tempfile.TemporaryDirectory()
    test._orig_db = config.DB_PATH
    config.DB_PATH = Path(test._tmp.name) / 'observability.db'
    db._conn = None
    db.connect()


def _close_temp_db(test: unittest.TestCase) -> None:
    if db._conn is not None:
        db._conn.close()
    db._conn = None
    config.DB_PATH = test._orig_db
    test._tmp.cleanup()


class SchemaAndHelpersTest(unittest.TestCase):
    def setUp(self) -> None:
        _temp_db(self)

    def tearDown(self) -> None:
        _close_temp_db(self)

    def test_new_columns_are_nullable_and_persisted(self) -> None:
        cols = {r['name']: r for r in db.query('PRAGMA table_info(request_logs)')}
        for name in ('reasoning_effort', 'client_channel', 'channel_validation'):
            self.assertIn(name, cols)
            self.assertFalse(cols[name]['notnull'], f'{name} 历史值必须能保持 NULL')

        db.add_request_log(
            ts=1, model='m', status=200, stream=0,
            reasoning_effort='high', client_channel='cli',
            channel_validation='accepted',
        )
        row = db.query_one(
            'SELECT reasoning_effort, client_channel, channel_validation FROM request_logs'
        )
        self.assertEqual(dict(row), {
            'reasoning_effort': 'high',
            'client_channel': 'cli',
            'channel_validation': 'accepted',
        })

    def test_missing_effort_stays_null(self) -> None:
        db.add_request_log(ts=1, model='m', status=200, stream=0)
        row = db.query_one('SELECT reasoning_effort FROM request_logs')
        self.assertIsNone(row['reasoning_effort'], '没传档位时不能按模型默认值补猜')

    def test_reasoning_effort_from_both_protocols(self) -> None:
        self.assertEqual(gateway._reasoning_effort({'reasoning_effort': 'low'}), 'low')
        self.assertEqual(
            gateway._reasoning_effort({'reasoning': {'effort': 'max'}}), 'max',
        )
        self.assertIsNone(gateway._reasoning_effort({'reasoning': {}}))
        self.assertIsNone(gateway._reasoning_effort({'reasoning_effort': 3}))
        self.assertIsNone(gateway._reasoning_effort(None))

    def test_client_channel_is_derived_without_header_leakage(self) -> None:
        self.assertEqual(gateway._client_channel({
            'user-agent': 'codex_cli_rs/1.2',
            'authorization': 'Bearer secret-should-never-be-read',
            'x-private-token': 'also-secret',
        }), 'cli')
        self.assertEqual(gateway._client_channel({
            'user-agent': 'Mozilla/5.0 VSCode/1.100',
        }), 'vscode')
        self.assertEqual(gateway._client_channel({
            'user-agent': 'WorkBuddy/5.0',
        }), 'workbuddy')
        self.assertEqual(gateway._client_channel({
            'user-agent': 'python-httpx/0.28',
        }), 'cli')
        self.assertEqual(gateway._client_channel({'user-agent': 'browser'}), 'unknown')

    def test_channel_validation_summary_is_stable(self) -> None:
        self.assertEqual(gateway._channel_validation(200, None), 'accepted')
        self.assertEqual(gateway._channel_validation(429, 'too many requests'), 'rate_limited')
        self.assertEqual(
            gateway._channel_validation(
                403, 'This call failed channel verification; code 11140',
            ),
            'mismatch:11140',
        )
        self.assertEqual(gateway._channel_validation(503, 'upstream down'), 'failed')
        self.assertEqual(gateway._channel_validation(0, None), 'unknown')

    def test_repeated_tail_line_is_not_counted_as_new_match_failure(self) -> None:
        db.add_request_log(
            ts=100, model='m', mapped_model='m', account='A(aaaaaaaa)', status=200,
        )
        counts = db.attach_request_accounts_detailed([
            {'ts': 100, 'model': 'm', 'account': 'A(aaaaaaaa)'},
        ])
        self.assertEqual(counts['matched'], 0)
        self.assertEqual(counts['already_matched'], 1)
        self.assertEqual(counts['unmatched'], 0)


class OldDatabaseMigrationTest(unittest.TestCase):
    def test_existing_request_log_rows_keep_null_new_fields(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / 'old.db'
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE request_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts INTEGER NOT NULL, key_id INTEGER, ip TEXT, model TEXT,
              mapped_model TEXT, status INTEGER DEFAULT 0,
              prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
              latency_ms INTEGER DEFAULT 0, first_token_ms INTEGER, ua TEXT,
              error TEXT, stream INTEGER DEFAULT 0, credit REAL, realm TEXT,
              account TEXT, cache_hit_tokens INTEGER, cache_miss_tokens INTEGER,
              cache_write_tokens INTEGER
            );
            INSERT INTO request_logs(ts, model, status)
              VALUES(123, 'old-model', 200);
        """)
        conn.commit()
        conn.close()

        orig = config.DB_PATH
        config.DB_PATH = path
        db._conn = None
        try:
            db.connect()
            cols = {r['name'] for r in db.query('PRAGMA table_info(request_logs)')}
            self.assertTrue(
                {'reasoning_effort', 'client_channel', 'channel_validation'} <= cols
            )
            row = db.query_one(
                'SELECT reasoning_effort, client_channel, channel_validation '
                'FROM request_logs WHERE ts = 123'
            )
            self.assertIsNone(row['reasoning_effort'])
            self.assertIsNone(row['client_channel'])
            self.assertIsNone(row['channel_validation'])
        finally:
            if db._conn is not None:
                db._conn.close()
            db._conn = None
            config.DB_PATH = orig
            tmp.cleanup()


class AccountCollectorDiagnosticsTest(unittest.TestCase):
    LINE = (
        '2026-09-23T06:58:13.123895379Z | #971 | 14:58:13 | deepseek-v4.1-flash'
        ' | stream | 200 | A(aaaaaaaa) | TTFB=1ms | tok=1 | 1tok/s | total=1s |'
    )

    def setUp(self) -> None:
        _temp_db(self)
        self._orig_mode = config.WB2API_MODE
        self._orig_log = config.WB2API_LOG_FILE
        accountlog._reset_status_for_tests()

    def tearDown(self) -> None:
        accountlog._reset_status_for_tests()
        config.WB2API_MODE = self._orig_mode
        config.WB2API_LOG_FILE = self._orig_log
        _close_temp_db(self)

    def test_native_path_validation_and_successful_counts(self) -> None:
        log = Path(self._tmp.name) / 'server.err.log'
        log.write_text('line\n', encoding='utf-8')
        config.WB2API_MODE = 'native'
        config.WB2API_LOG_FILE = log
        ts = accountlog.parse_request_lines([self.LINE])[0]['ts']
        with mock.patch.object(
            accountlog, '_read_native_log',
            return_value=([self.LINE], float(ts)),
        ), mock.patch.object(db, 'attach_request_accounts_detailed', return_value={
            'matched': 1, 'unmatched': 0, 'already_matched': 0,
            'invalid': 0, 'errors': 0,
        }) as attach:
            result = accountlog._collect_once_detailed()

        self.assertEqual(result, {
            'parsed': 1, 'filled': 1, 'unmatched': 0,
            'already_matched': 0, 'log_readable': True,
        })
        attach.assert_called_once()
        status = accountlog.collector_status()
        self.assertEqual(status['source'], 'native')
        self.assertTrue(status['log_readable'])
        self.assertEqual(status['matched_count'], 1)
        self.assertEqual(status['unmatched_count'], 0)
        self.assertIsNotNone(status['last_attempt_at'])
        self.assertIsNotNone(status['last_success_at'])
        self.assertIsNone(status['last_error'])
        self.assertIn(str(log), status['log_location'])
        self.assertEqual(ts, attach.call_args.args[0][0]['ts'])

    def test_unmatched_entries_are_counted_without_guessing(self) -> None:
        log = Path(self._tmp.name) / 'server.err.log'
        log.write_text('line\n', encoding='utf-8')
        config.WB2API_MODE = 'native'
        config.WB2API_LOG_FILE = log
        ts = accountlog.parse_request_lines([self.LINE])[0]['ts']
        with mock.patch.object(
            accountlog, '_read_native_log',
            return_value=([self.LINE, self.LINE], float(ts)),
        ), mock.patch.object(db, 'attach_request_accounts_detailed', return_value={
            'matched': 0, 'unmatched': 2, 'already_matched': 0,
            'invalid': 0, 'errors': 0,
        }):
            result = accountlog._collect_once_detailed()
        self.assertEqual(result['parsed'], 2)
        self.assertEqual(result['unmatched'], 2)
        self.assertEqual(accountlog.collector_status()['unmatched_count'], 2)

    def test_missing_native_file_is_reported_and_not_misread(self) -> None:
        config.WB2API_MODE = 'native'
        config.WB2API_LOG_FILE = Path(self._tmp.name) / 'missing.log'
        with mock.patch.object(accountlog, '_read_native_log') as reader:
            with self.assertRaisesRegex(RuntimeError, '不存在'):
                accountlog._collect_once_detailed()
        reader.assert_not_called()
        status = accountlog.collector_status()
        self.assertFalse(status['log_readable'])
        self.assertIn('不存在', status['last_error'])

    def test_docker_missing_cli_is_reported_with_source(self) -> None:
        config.WB2API_MODE = 'docker'
        with mock.patch.object(accountlog.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'docker'):
                accountlog._collect_once_detailed()
        status = accountlog.collector_status()
        self.assertEqual(status['source'], 'docker')
        self.assertFalse(status['log_readable'])
        self.assertIn('docker', status['last_error'])

    def test_quoted_and_relative_native_paths(self) -> None:
        log = Path(self._tmp.name) / 'server.err.log'
        log.touch()
        config.WB2API_LOG_FILE = f'"{log}"'
        path, error = accountlog._normalized_native_path()
        self.assertIsNone(error)
        self.assertEqual(Path(path), log)

        config.WB2API_LOG_FILE = Path('relative.log')
        _, error = accountlog._normalized_native_path()
        self.assertIn('绝对路径', error or '')

    def test_viewer_does_not_receive_host_path(self) -> None:
        config.WB2API_MODE = 'native'
        config.WB2API_LOG_FILE = Path(self._tmp.name) / 'server.err.log'
        admin = logs._collector_payload({'role': 'admin'})
        viewer = logs._collector_payload({'role': 'viewer'})
        token = logs._collector_payload({'role': 'admin'}, session_admin=False)
        self.assertTrue(admin['log_location'])
        self.assertEqual(viewer['log_location'], '')
        self.assertEqual(token['log_location'], '', 'API Token 不该拿到宿主机路径')


class _JsonResponse:
    headers = {'content-type': 'application/json'}

    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    async def aclose(self):
        return None


class _Client:
    def __init__(self, status: int = 200):
        self.status = status
        self.posts: list[dict] = []

    async def post(self, url, **kwargs):
        self.posts.append({'url': url, **kwargs})
        if self.status == 429:
            return _JsonResponse(429, {'error': {'message': 'rate limited'}})
        return _JsonResponse(200, {
            'id': 'chatcmpl-test',
            'object': 'chat.completion',
            'created': 1,
            'model': 'glm-5.2',
            'choices': [{
                'index': 0,
                'message': {'role': 'assistant', 'content': 'ok'},
                'finish_reason': 'stop',
            }],
            'usage': {
                'prompt_tokens': 3,
                'completion_tokens': 2,
                'total_tokens': 5,
            },
        })

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _StreamResponse(_JsonResponse):
    def __init__(self) -> None:
        frame = {
            'id': 'chatcmpl-stream-test',
            'model': 'glm-5.2',
            'choices': [{
                'index': 0,
                'delta': {'role': 'assistant', 'content': 'ok'},
                'finish_reason': None,
            }],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2},
        }
        super().__init__(200, frame)
        self.headers = {'content-type': 'text/event-stream'}
        self.body = f'data: {json.dumps(frame)}\n\n'.encode()

    async def aiter_bytes(self):
        yield self.body


class _StreamingClient:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def build_request(self, method, url, **kwargs):
        self.requests.append({'method': method, 'url': url, **kwargs})
        return object()

    async def send(self, _request, stream=True):
        self.stream = stream
        return _StreamResponse()

    async def aclose(self):
        return None


class GatewayObservabilityIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'gateway.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)
        self.token = keysvc.create_key('observability')['key']

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _row(self) -> dict:
        return db.query_one(
            'SELECT reasoning_effort, client_channel, channel_validation '
            'FROM request_logs ORDER BY id DESC LIMIT 1'
        )

    def test_chat_records_actual_effort_and_cli_channel(self) -> None:
        client = _Client()
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            r = self.client.post(
                '/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {self.token}',
                    'User-Agent': 'codex_cli_rs/1.0',
                    'x-private-auth': 'must-not-be-stored',
                },
                json={
                    'model': 'glm-5.2',
                    'reasoning_effort': 'high',
                    'messages': [{'role': 'user', 'content': 'hi'}],
                },
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(dict(self._row()), {
            'reasoning_effort': 'high',
            'client_channel': 'cli',
            'channel_validation': 'accepted',
        })
        ua = db.query_one('SELECT ua FROM request_logs ORDER BY id DESC LIMIT 1')['ua']
        self.assertNotIn('must-not-be-stored', ua or '')

    def test_responses_records_actual_nested_effort_and_vscode_channel(self) -> None:
        client = _Client()
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            r = self.client.post(
                '/v1/responses',
                headers={
                    'Authorization': f'Bearer {self.token}',
                    'User-Agent': 'Mozilla/5.0 VSCode/1.100',
                },
                json={
                    'model': 'glm-5.2',
                    'input': 'hi',
                    'reasoning': {'effort': 'max'},
                },
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(dict(self._row()), {
            'reasoning_effort': 'max',
            'client_channel': 'vscode',
            'channel_validation': 'accepted',
        })

    def test_429_is_logged_without_a_channel_retry(self) -> None:
        client = _Client(status=429)
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            r = self.client.post(
                '/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {self.token}',
                    'x-client-channel': 'cli',
                },
                json={
                    'model': 'glm-5.2',
                    'messages': [{'role': 'user', 'content': 'hi'}],
                },
            )
        self.assertEqual(r.status_code, 429)
        self.assertEqual(len(client.posts), 1, '429 不允许被放大成多倍请求')
        row = self._row()
        self.assertEqual(row['client_channel'], 'cli')
        self.assertEqual(row['channel_validation'], 'rate_limited')

    def test_anthropic_messages_records_effort_and_channel_without_retry(self) -> None:
        client = _Client()
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            r = self.client.post(
                '/v1/messages',
                headers={
                    'Authorization': f'Bearer {self.token}',
                    'anthropic-version': '2023-06-01',
                    'User-Agent': 'claude-code/2.0',
                },
                json={
                    'model': 'glm-5.2',
                    'max_tokens': 32,
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    'output_config': {'effort': 'high'},
                },
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(client.posts), 1, '取证不允许产生额外请求')
        self.assertEqual(client.posts[0]['json']['reasoning_effort'], 'high')
        self.assertEqual(dict(self._row()), {
            'reasoning_effort': 'high',
            'client_channel': 'cli',
            'channel_validation': 'accepted',
        })

    def test_anthropic_stream_records_finally_metadata(self) -> None:
        client = _StreamingClient()
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            r = self.client.post(
                '/v1/messages',
                headers={
                    'Authorization': f'Bearer {self.token}',
                    'anthropic-version': '2023-06-01',
                    'x-client-channel': 'vscode',
                },
                json={
                    'model': 'glm-5.2',
                    'max_tokens': 32,
                    'stream': True,
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    'output_config': {'effort': 'max'},
                },
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(client.requests), 1, '流式取证同样不允许重试')
        self.assertEqual(client.requests[0]['json']['reasoning_effort'], 'max')
        self.assertEqual(dict(self._row()), {
            'reasoning_effort': 'max',
            'client_channel': 'vscode',
            'channel_validation': 'accepted',
        })


class ApiAndFrontendContractTest(unittest.TestCase):
    def test_collector_api_routes_are_registered(self) -> None:
        from server.main import app
        paths = app.openapi()['paths']
        self.assertIn('get', paths['/api/logs/account-collector'])
        self.assertIn('post', paths['/api/logs/account-collector/collect'])

    def test_logs_api_exposes_new_fields(self) -> None:
        _temp_db(self)
        try:
            db.add_request_log(
                ts=1, model='m', status=200, stream=0,
                reasoning_effort='low', client_channel='workbuddy',
                channel_validation='accepted',
            )
            payload = logs.list_logs(user={'role': 'admin'})
            item = payload['items'][0]
            self.assertEqual(item['reasoning_effort'], 'low')
            self.assertEqual(item['client_channel'], 'workbuddy')
            self.assertEqual(item['channel_validation'], 'accepted')
        finally:
            _close_temp_db(self)

    def test_logs_page_renders_diagnostics_and_derived_fields(self) -> None:
        page = (
            Path(__file__).resolve().parents[2] / 'web' / 'app' / '(main)'
            / 'logs' / 'page.tsx'
        ).read_text(encoding='utf-8')
        for required in (
            '/api/logs/account-collector',
            '/api/logs/account-collector/collect',
            'reasoning_effort',
            'client_channel',
            'channel_validation',
            "t('logs.collectorCollect')",
            "t('logs.collectorMatched')",
            "t('logs.collectorUnmatched')",
            "t('logs.reasoningUnspecified')",
        ):
            self.assertIn(required, page)


if __name__ == '__main__':
    unittest.main()
