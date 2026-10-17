import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import private_sessions as p
from backend.user_context import Layout, from_session


class SessionsTests(unittest.TestCase):
    def setUp(self):
        root = p.PERMITTED_ROOT / 'runtime/private-session-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=root))
        self.path = self.root / 'accounts-hashed.json'
        self.store = p.AccountStore(self.path)
        self.password = secrets.token_urlsafe(24)
        self.now = 1000.
        self.manager = p.SessionManager(self.store, ttl_seconds=60, clock=lambda: self.now)

    def create(self):
        self.store.create_account('user001', self.password)

    def test_no_default_accounts_or_side_effect_on_init(self):
        self.assertFalse(self.path.exists())
        with self.assertRaises(p.AuthenticationError):
            self.manager.login('user001', self.password)
        self.assertFalse(self.path.exists())

    def test_hash_storage_salts_and_success(self):
        self.create()
        self.store.create_account('user002', self.password)
        raw = self.path.read_text(encoding='utf-8')
        self.assertTrue(self.password not in raw)
        users = json.loads(raw)['users']
        self.assertNotEqual(users['user001']['salt'], users['user002']['salt'])
        self.assertEqual(users['user001']['iterations'], 600000)
        token = self.manager.login('user001', self.password)
        self.assertEqual(self.manager.lookup(token).username, 'user001')

    def test_wrong_password_and_unknown_user_same_failure(self):
        self.create()
        errors = []
        for name, password in [('user001', 'wrong-test-value'), ('user002', 'wrong-test-value')]:
            with self.assertRaises(p.AuthenticationError) as failure:
                self.manager.login(name, password)
            errors.append(str(failure.exception))
        self.assertEqual(errors[0], errors[1])
        with patch.object(p.hmac, 'compare_digest', wraps=p.hmac.compare_digest) as comparison:
            self.store.authenticate('user001', self.password)
            self.assertEqual(comparison.call_count, 1)

    def test_token_digest_only_and_username_not_auth(self):
        self.create()
        token = self.manager.login('user001', self.password)
        self.assertTrue(token not in repr(self.manager._sessions))
        self.assertIsNone(self.manager.lookup('user001'))
        self.assertIsNone(self.manager.lookup(secrets.token_urlsafe(32)))
        context = from_session(token, self.manager.lookup, Layout(self.root / 'users'), now=self.now)
        self.assertEqual(context.username, 'user001')

    def test_expiry_and_logout(self):
        self.create()
        token = self.manager.login('user001', self.password)
        self.now = 1060.
        self.assertIsNone(self.manager.lookup(token))
        token = self.manager.login('user001', self.password)
        self.manager.logout(token)
        self.manager.logout(token)
        self.assertIsNone(self.manager.lookup(token))

    def test_restart_and_revoke(self):
        self.create()
        first = self.manager.login('user001', self.password)
        second = self.manager.login('user001', self.password)
        self.assertNotEqual(first, second)
        self.assertIsNone(p.SessionManager(self.store).lookup(first))
        self.manager.revoke_user('user001')
        self.assertIsNone(self.manager.lookup(first))
        self.assertIsNone(self.manager.lookup(second))

    def test_password_change_revokes_old_tokens(self):
        self.create()
        token = self.manager.login('user001', self.password)
        replacement = secrets.token_urlsafe(24)
        self.store.set_password('user001', replacement)
        self.assertIsNone(self.manager.lookup(token))
        with self.assertRaises(p.AuthenticationError):
            self.manager.login('user001', self.password)
        self.assertIsNotNone(self.manager.lookup(self.manager.login('user001', replacement)))

    def test_removed_account_revokes_session(self):
        self.create()
        token = self.manager.login('user001', self.password)
        self.path.write_text(json.dumps({'schema_version': 1, 'users': {}}), encoding='utf-8')
        self.assertIsNone(self.manager.lookup(token))

    def test_plaintext_schema_not_migrated(self):
        legacy = json.dumps({'users': {'user001': self.password}})
        self.path.write_text(legacy, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'schema'):
            self.store.create_account('user002', self.password)
        with self.assertRaises(p.AuthenticationError):
            self.manager.login('user001', self.password)
        self.assertTrue(self.path.read_text(encoding='utf-8') == legacy)

    def test_atomic_replace_failure_preserves_previous_store(self):
        self.create()
        before = self.path.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('simulated replace failure')):
            with self.assertRaises(OSError):
                self.store.set_password('user001', secrets.token_urlsafe(24))
        self.assertTrue(self.path.read_bytes() == before)
        self.assertIsNotNone(self.store.authenticate('user001', self.password))

    def test_bad_policy_and_root_rejected(self):
        for value in (0, -1, True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                p.SessionManager(self.store, ttl_seconds=value)
        with self.assertRaises(ValueError):
            p.AccountStore(self.root / 'outside.json', permitted_root=self.root / 'restricted')
        with self.assertRaises(ValueError):
            self.store.create_account('../escape', self.password)


if __name__ == '__main__':
    unittest.main()
