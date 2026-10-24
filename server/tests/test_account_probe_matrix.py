"""账号 × 模型主动探测矩阵（issue #270）的回归测试。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import accounts  # noqa: E402
from server.services import tencent  # noqa: E402


def _account(file: str, realm: str = 'cn', token: str = 'SECRET') -> dict:
    return {
        'file': file,
        'nickname': file,
        'realm': realm,
        '_auth': {
            'access_token': token,
            'uid': f'uid-{file}',
            'realm': realm,
        },
    }


class _StreamingError:
    status_code = 200
    headers = {'content-type': 'text/event-stream'}

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b''

    async def aiter_bytes(self):
        self.calls += 1
        yield self.body


class _Client:
    responses: list[_StreamingError] = []
    requests = 0

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *a, **k):
        type(self).requests += 1
        return type(self).responses.pop(0)


class _SplitStreamingError(_StreamingError):
    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__(b'')
        self.chunks = chunks

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


class _Http429(_StreamingError):
    status_code = 429

    async def aread(self):
        return b'rate limited'


class _ChannelFallbackClient:
    urls: list[str] = []

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, **kw):
        type(self).urls.append(url)

        class _Resp:
            status_code = 400
            headers = {'content-type': 'application/json'}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aread(self):
                return (b'{"code":11128,"msg":"request failed channel verification"}')

        return _Resp()


class ProbeClassificationTest(unittest.TestCase):
    def test_code_classification(self) -> None:
        self.assertEqual(tencent._probe_category(200, 0), 'success')
        self.assertEqual(tencent._probe_category(400, 6004), 'rate_limited')
        self.assertEqual(tencent._probe_category(402, None), 'quota_exhausted')
        self.assertEqual(tencent._probe_category(429, 14018), 'quota_exhausted')
        self.assertEqual(tencent._probe_category(429, None), 'rate_limited')
        self.assertEqual(tencent._probe_category(401, None), 'unauthorized')
        self.assertEqual(tencent._probe_category(400, 12153), 'unauthorized')
        self.assertEqual(tencent._probe_category(400, 11102), 'model_unavailable')
        self.assertEqual(tencent._probe_category(200, 50000), 'upstream_error')

    def test_sse_error_is_not_mistaken_for_success(self) -> None:
        response = _StreamingError(
            b'data: {"code":6004,"msg":"rate limited"}\n\n')
        _Client.responses = [response]
        _Client.requests = 0
        with mock.patch.object(config, 'http_client', _Client):
            out = asyncio.run(tencent.probe_account_detailed(
                {'access_token': 'SECRET', 'realm': 'cn'}, 'glm-5.2'))
        self.assertFalse(out['ok'])
        self.assertEqual(out['category'], 'rate_limited')
        self.assertEqual(out['code'], 6004)
        self.assertEqual(_Client.requests, 1, '6004 后不得重试同一账号/模型')

    def test_split_6004_event_is_classified_without_retry(self) -> None:
        response = _SplitStreamingError([
            b'data: {"code":60',
            b'04,"msg":"rate limited"}\n\n',
        ])
        _Client.responses = [response]
        _Client.requests = 0
        with mock.patch.object(config, 'http_client', _Client):
            out = asyncio.run(tencent.probe_account_detailed(
                {'access_token': 'SECRET', 'realm': 'cn'}, 'glm-5.2'))
        self.assertEqual(out['category'], 'rate_limited')
        self.assertEqual(out['code'], 6004)
        self.assertEqual(_Client.requests, 1)

    def test_custom_base_only_accepts_http_and_https(self) -> None:
        base, error = tencent._probe_base_url('cn', 'http://10.0.0.8:7863')
        self.assertEqual(base, 'http://10.0.0.8:7863')
        self.assertIsNone(error, '管理员显式配置的内网目标不应被探测层擅自封禁')
        base, error = tencent._probe_base_url('global', 'HTTPS://example.com/')
        self.assertEqual(base, 'HTTPS://example.com')
        self.assertIsNone(error)
        for bad in ('file:///etc/passwd', 'ftp://example.com', 'http://'):
            with self.subTest(bad=bad):
                base, error = tencent._probe_base_url('cn', bad)
                self.assertEqual(base, '')
                self.assertIn('http://', error or '')

    def test_invalid_custom_base_is_rejected_before_network(self) -> None:
        _Client.responses = []
        _Client.requests = 0
        with mock.patch.object(config, 'http_client', _Client):
            out = asyncio.run(tencent.probe_account_detailed(
                {'access_token': 'SECRET', 'realm': 'cn',
                 'domain': 'file:///etc/passwd'}, 'glm-5.2'))
        self.assertEqual(out['category'], 'request_error')
        self.assertEqual(_Client.requests, 0)

    def test_result_redacts_access_token(self) -> None:
        class Broken(_Client):
            def stream(self, *a, **k):
                raise RuntimeError('boom SECRET')

        with mock.patch.object(config, 'http_client', Broken):
            out = asyncio.run(tencent.probe_account_detailed(
                {'access_token': 'SECRET', 'realm': 'cn'}, 'glm-5.2'))
        self.assertNotIn('SECRET', json.dumps(out))
        self.assertIn('[redacted]', out['message'])

    def test_bare_http_429_is_rate_limited(self) -> None:
        _Client.responses = [_Http429(b'')]
        _Client.requests = 0
        with mock.patch.object(config, 'http_client', _Client):
            out = asyncio.run(tencent.probe_account_detailed(
                {'access_token': 'SECRET', 'realm': 'cn'}, 'glm-5.2'))
        self.assertEqual(out['category'], 'rate_limited')
        self.assertEqual(_Client.requests, 1)

    def test_channel_fallback_consumes_physical_request_budget(self) -> None:
        _ChannelFallbackClient.urls = []
        gate = tencent.ProbeRequestGate(limit=1, min_interval_ms=0)
        with mock.patch.object(config, 'http_client', _ChannelFallbackClient):
            out = asyncio.run(tencent.probe_account_detailed(
                {'access_token': 'SECRET', 'uid': 'uid-a', 'realm': 'cn'},
                'glm-5.2', request_gate=gate, request_account_key='a.json',
            ))
        self.assertEqual(out['category'], 'budget_exhausted')
        self.assertIn('预算已耗尽', out['message'])
        self.assertEqual(gate.used, 1)
        self.assertEqual(len(_ChannelFallbackClient.urls), 1,
                         '预算阻止回退后不得再发第二个 HTTP 请求')


class ProbeRequestGateTest(unittest.TestCase):
    def test_budget_is_global_and_interval_is_per_account_after_finish(self) -> None:
        now = {'value': 0.0}
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)
            now['value'] += delay

        gate = tencent.ProbeRequestGate(
            limit=2,
            min_interval_ms=500,
            clock=lambda: now['value'],
            sleeper=fake_sleep,
        )

        async def scenario():
            async with gate.attempt('a.json'):
                now['value'] += 0.1
            async with gate.attempt('a.json'):
                now['value'] += 0.1
            with self.assertRaises(tencent.ProbeBudgetExhausted):
                async with gate.attempt('b.json'):
                    pass

        asyncio.run(scenario())
        self.assertEqual(gate.used, 2)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.5,
                               msg='间隔从上一请求结束开始计')


class ProbeMatrixRunnerTest(unittest.TestCase):
    def test_same_account_is_serial_with_500ms_gap(self) -> None:
        stamps: list[float] = []

        async def probe(auth, model, *, request_gate=None, request_account_key=''):
            stamps.append(time.monotonic())
            return {'ok': True, 'category': 'success', 'code': 200,
                    'message': 'ok', 'elapsed_ms': 1}

        job = accounts._new_probe_matrix_job(
            [_account('a.json')], ['m1', 'm2'], 2, 500)
        asyncio.run(accounts._run_probe_matrix_job(job, probe))
        self.assertEqual(len(stamps), 2)
        self.assertGreaterEqual(stamps[1] - stamps[0], 0.49,
                                '同一账号相邻请求至少间隔 500ms')
        self.assertEqual(job['state'], 'finished')

    def test_account_concurrency_is_bounded(self) -> None:
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def probe(auth, model, *, request_gate=None, request_account_key=''):
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.03)
            async with lock:
                active -= 1
            return {'ok': True, 'category': 'success', 'code': 200,
                    'message': 'ok', 'elapsed_ms': 30}

        job = accounts._new_probe_matrix_job(
            [_account(f'{i}.json') for i in range(6)], ['m'], 2, 500)
        asyncio.run(accounts._run_probe_matrix_job(job, probe))
        self.assertEqual(peak, 2)

    def test_rate_limit_is_recorded_once(self) -> None:
        calls: list[tuple[str, str]] = []

        async def probe(auth, model, *, request_gate=None, request_account_key=''):
            calls.append((auth['uid'], model))
            return {'ok': False, 'category': 'rate_limited', 'code': 6004,
                    'message': 'limited', 'elapsed_ms': 4}

        job = accounts._new_probe_matrix_job(
            [_account('a.json')], ['glm-5.2'], 1, 500)
        asyncio.run(accounts._run_probe_matrix_job(job, probe))
        self.assertEqual(calls, [('uid-a.json', 'glm-5.2')])
        self.assertEqual(next(iter(job['results'].values()))['status'], 'rate_limited')

    def test_cancel_stops_future_calls(self) -> None:
        calls = 0

        async def probe(auth, model, *, request_gate=None, request_account_key=''):
            nonlocal calls
            calls += 1
            return {'ok': True, 'category': 'success', 'code': 200,
                    'message': 'ok', 'elapsed_ms': 1}

        async def scenario():
            job = accounts._new_probe_matrix_job(
                [_account('a.json')], ['m1', 'm2', 'm3'], 1, 500)
            task = asyncio.create_task(accounts._run_probe_matrix_job(job, probe))
            await asyncio.sleep(0.08)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return job

        job = asyncio.run(scenario())
        self.assertLess(calls, 3)
        self.assertEqual(job['state'], 'cancelled')
        self.assertIsNotNone(job['finished_at'])
        self.assertEqual(job['accounts'][0]['_auth'], {})
        self.assertFalse(accounts._probe_matrix_active(job))

    def test_unexpected_runner_error_is_terminal_and_scrubs_auth(self) -> None:
        held_cancelled = False

        async def held_probe(auth, model, *, request_gate=None, request_account_key=''):
            nonlocal held_cancelled
            try:
                await asyncio.Event().wait()
            finally:
                held_cancelled = True

        job = accounts._new_probe_matrix_job(
            [_account('a.json', token='ACCESS-TOKEN-SECRET'),
             _account('b.json')], ['m1'], 2, 500)
        # worker 在进入单请求异常保护前读取 realm；删掉它制造外层异常。
        job['accounts'][1].pop('realm')
        with self.assertLogs('workbuddy.accounts', level='ERROR'):
            asyncio.run(accounts._run_probe_matrix_job(job, held_probe))
        self.assertEqual(job['state'], 'failed')
        self.assertIsNotNone(job['finished_at'])
        self.assertIn('内部异常', job['error'])
        self.assertTrue(held_cancelled, '外层异常后必须取消仍在执行的兄弟 worker')
        self.assertTrue(all(a['_auth'] == {} for a in job['accounts']))
        self.assertFalse(accounts._probe_matrix_active(job))
        self.assertEqual(
            job['results'][accounts._probe_result_key('a.json', 'cn', 'm1')]['status'],
            'request_error',
            '未执行的组合也必须离开 pending，避免进度永久未完成',
        )

    def test_snapshot_never_contains_auth(self) -> None:
        job = accounts._new_probe_matrix_job(
            [_account('a.json', token='ACCESS-TOKEN-SECRET')], ['glm-5.2'], 1, 500)
        blob = json.dumps(accounts._probe_matrix_snapshot(job), ensure_ascii=False)
        self.assertNotIn('ACCESS-TOKEN-SECRET', blob)
        self.assertNotIn('"auth"', blob)
        self.assertNotIn('access_token', blob)

    def test_global_account_gets_protocol_prefix(self) -> None:
        job = accounts._new_probe_matrix_job(
            [_account('g.json', realm='global')], ['deepseek-v4.1-flash'], 1, 500)
        item = next(iter(job['results'].values()))
        self.assertEqual(item['request_model'], 'global:deepseek-v4.1-flash')
        self.assertEqual(item['realm'], 'global')


if __name__ == '__main__':
    unittest.main()
