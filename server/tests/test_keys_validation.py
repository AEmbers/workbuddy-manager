"""密钥输入校验的回归测试（发版前对抗性测试发现的三处）。

三处都不是 5xx，但都让用户**看不懂发生了什么**——比直接报错更费解：

  1. **CIDR 写错 = 密钥永久不可用**。`ip_matches` 对非法 CIDR 一律返回 False
     （fail-closed，方向本来是对的），所以白名单里只要有一个写错的条目，
     这把密钥对**所有**来源都拒绝。而错误只说「不在密钥白名单内」，
     用户根本看不出是自己把 CIDR 写错了（实测确认过这条错误信息）。
  2. **名称只填空格**能建出来。列表里就是一行「没有名字」的密钥，
     管理员认不出它是干什么的，也不知道是自己误操作。
  3. **对不存在的 id 重置用量返回 200**（静默成功）。前端提示「已重置」，
     而实际什么都没发生——密钥可能已被别人删掉。

前两条改为**写入前校验并明确说明该怎么写**，第三条改为 404。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402


class KeyInputValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'k.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        # 直接用已有登录态太重，这里绕过依赖校验，直接测路由函数的校验分支
        from server.main import app
        cls.c = TestClient(app)

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

    # ── 直接测校验函数（不依赖登录态，稳定）────────────────────

    def test_bad_cidr_rejected_with_hint(self) -> None:
        """非法 CIDR 必须在写入前被拒，且说明会有什么后果。"""
        from fastapi import HTTPException

        from server.routers.keys import _check_ip_allowlist
        for bad in ['not-a-cidr', '999.999.999.999/99', '10.0.0.0/999', 'abc/8']:
            with self.assertRaises(HTTPException, msg=bad) as ctx:
                _check_ip_allowlist([bad])
            self.assertEqual(ctx.exception.status_code, 400)
            detail = str(ctx.exception.detail)
            self.assertIn(bad, detail, '要指出是哪一条写错了')
            self.assertIn('CIDR', detail, '要说明正确写法')

    def test_good_cidr_accepted(self) -> None:
        from server.routers.keys import _check_ip_allowlist
        for good in ['10.0.0.0/8', '1.2.3.4', '192.168.1.0/24', '::1', '2001:db8::/32']:
            _check_ip_allowlist([good])       # 不抛即通过
        _check_ip_allowlist([])
        _check_ip_allowlist(None)
        _check_ip_allowlist(['', '  '])       # 空项交给 keysvc 过滤，不算错
        _check_ip_allowlist(['10.0.0.0/8', '1.2.3.4'])   # 多项混合

    def test_blank_name_rejected(self) -> None:
        from fastapi import HTTPException

        from server.routers.keys import _check_name
        for bad in ['   ', '\t', '\n', ' \t \n ']:
            with self.assertRaises(HTTPException, msg=repr(bad)) as ctx:
                _check_name(bad)
            self.assertEqual(ctx.exception.status_code, 400)
        for good in ['a', ' 名称 ', 'x']:
            _check_name(good)
        _check_name(None)      # PATCH 不带 name 时不校验

    def test_bad_cidr_would_lock_key_out(self) -> None:
        """把「为什么必须拦」固化成测试：坏 CIDR 让密钥对所有 IP 都拒绝。

        这是本组校验存在的理由，写下来免得以后有人觉得多余而删掉。
        """
        from server.iputil import ip_matches
        for ip in ['9.9.9.9', '1.2.3.4', '127.0.0.1', '2001:db8::1']:
            self.assertFalse(ip_matches(ip, 'not-a-cidr'),
                             '非法 CIDR 必须匹配不上任何 IP（fail-closed）')

    def test_reset_usage_returns_bool(self) -> None:
        """reset_usage 要能区分「重置了」与「没有这把密钥」。"""
        created = keysvc.create_key('t')
        self.assertTrue(keysvc.reset_usage(created['id']))
        self.assertFalse(keysvc.reset_usage(999999), '不存在的 id 应返回 False')

    def test_custom_token_round_trips_and_resolves(self) -> None:
        token = 'sk-custom-key-for-manager-285'
        created = keysvc.create_key('custom-key', custom_token=token)
        self.assertEqual(created['key'], token)
        self.assertEqual(created['prefix'], token[:12])
        resolved = keysvc.resolve(token)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved['id'], created['id'])

    def test_custom_token_rejects_invalid_values_and_duplicates(self) -> None:
        for bad in ('short', 'has whitespace-inside-123', '非ASCII-custom-key-123'):
            with self.assertRaises(keysvc.InvalidCustomToken, msg=repr(bad)):
                keysvc.normalize_custom_token(bad)

        token = 'sk-duplicate-custom-token-285'
        keysvc.create_key('first-custom', custom_token=token)
        with self.assertRaises(keysvc.DuplicateToken):
            keysvc.create_key('second-custom', custom_token=token)

    def test_database_enforces_custom_token_uniqueness_under_concurrency(self) -> None:
        """落库边界必须原子，不能只依赖 create_key 里两次独立查询的 TOCTOU 检查。"""
        token = 'sk-concurrent-custom-token-285'
        workers = 8
        barrier = threading.Barrier(workers)
        original_exists = keysvc._token_hash_exists

        def synchronized_exists(token_hash, conn=None):
            # 让所有线程都在「预检查」阶段看到不存在，再一起竞争唯一索引。
            barrier.wait(timeout=5)
            return original_exists(token_hash, conn)

        def create_one(index: int):
            try:
                created = keysvc.create_key(
                    f'concurrent-{index}', custom_token=token
                )
                return 'created', created
            except keysvc.DuplicateToken:
                return 'duplicate', None

        with mock.patch.object(keysvc, '_token_hash_exists', synchronized_exists):
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(create_one, range(workers)))

        outcomes = [kind for kind, _ in results]
        self.assertEqual(outcomes.count('created'), 1, outcomes)
        self.assertEqual(outcomes.count('duplicate'), workers - 1, outcomes)
        row = db.query_one(
            'SELECT COUNT(*) AS c FROM api_keys WHERE name LIKE ?',
            ('concurrent-%',),
        )
        self.assertEqual(row['c'], 1)

        index = db.query_one(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_keys_hash' AND sql LIKE '%UNIQUE%'"
        )
        self.assertIsNotNone(index, 'api_keys.key_hash 缺少数据库唯一索引')

    def test_random_token_collision_retries_then_succeeds(self) -> None:
        collision = 'wbk_retry-collision'
        db.execute(
            'INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at) '
            'VALUES(?, ?, ?, 1, ?)',
            ('seed-collision', keysvc._hash(collision), collision[:12],
             int(db.time.time())),
        )
        with mock.patch.object(
            keysvc.secrets, 'token_urlsafe',
            side_effect=['retry-collision', 'fresh-token'],
        ):
            created = keysvc.create_key('after-collision')
        self.assertEqual(created['key'], f'{keysvc.TOKEN_PREFIX}fresh-token')
        self.assertIsNotNone(keysvc.resolve(created['key']))

    def test_random_token_collision_retries_are_bounded(self) -> None:
        collision = 'wbk_bounded-collision'
        db.execute(
            'INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at) '
            'VALUES(?, ?, ?, 1, ?)',
            ('seed-bounded-collision', keysvc._hash(collision), collision[:12],
             int(db.time.time())),
        )
        with (
            mock.patch.object(keysvc, 'RANDOM_TOKEN_MAX_ATTEMPTS', 3),
            mock.patch.object(
                keysvc.secrets, 'token_urlsafe', return_value='bounded-collision'
            ) as token_urlsafe,
        ):
            with self.assertRaises(keysvc.TokenGenerationError):
                keysvc.create_key('bounded-collision')
        self.assertEqual(token_urlsafe.call_count, 3)

    def test_validation_does_not_block_legit_edit(self) -> None:
        """合法编辑不能被新校验误伤（尤其 PATCH 只发部分字段时）。"""
        from server.routers.keys import _check_ip_allowlist, _check_name
        # 只改名字，不带 ip_allowlist → 不应触发 CIDR 校验
        _check_name('新名字')
        # 只改白名单，合法
        _check_ip_allowlist(['10.0.0.0/8'])


class KeyHashMigrationTest(unittest.TestCase):
    """旧库若已被竞态写入重复哈希，升级必须停下并保留现场。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'legacy-duplicate.db'
        if db._conn is not None:
            db._conn.close()
        db._conn = None

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_duplicate_hashes_fail_closed_with_clear_error(self) -> None:
        conn = sqlite3.connect(config.DB_PATH)
        conn.executescript("""
            CREATE TABLE api_keys (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              name          TEXT    NOT NULL,
              key_hash      TEXT    NOT NULL,
              prefix        TEXT    NOT NULL,
              enabled       INTEGER NOT NULL DEFAULT 1,
              expires_at    INTEGER,
              max_ips       INTEGER NOT NULL DEFAULT 0,
              ip_allowlist  TEXT    NOT NULL DEFAULT '[]',
              models        TEXT    NOT NULL DEFAULT '[]',
              quota         INTEGER NOT NULL DEFAULT 0,
              used_tokens   INTEGER NOT NULL DEFAULT 0,
              created_at    INTEGER NOT NULL,
              last_used_at  INTEGER
            );
            INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at)
            VALUES ('first', 'same-hash', 'same-prefix', 1, 1),
                   ('second', 'same-hash', 'same-prefix', 1, 1);
        """)
        conn.commit()
        conn.close()

        with self.assertRaises(RuntimeError) as ctx:
            db.connect()
        message = str(ctx.exception)
        self.assertIn('重复', message)
        self.assertIn('api_keys.key_hash', message)
        self.assertIn('启动已中止', message)
        self.assertIsNone(db._conn, '失败后不能留下半开连接')

        conn = sqlite3.connect(config.DB_PATH)
        count = conn.execute('SELECT COUNT(*) FROM api_keys').fetchone()[0]
        index = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_keys_hash'"
        ).fetchone()
        conn.close()
        self.assertEqual(count, 2, '迁移失败时必须保留全部原始数据')
        self.assertIsNone(index, '有重复数据时不能悄悄跳过唯一索引')


if __name__ == '__main__':
    unittest.main()
