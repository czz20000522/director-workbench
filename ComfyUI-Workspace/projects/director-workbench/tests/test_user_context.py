"""CPU-only identity/path/receipt contracts. No application or GPU imports."""
import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('user_context', Path(__file__).resolve().parents[1] / 'backend/user_context.py')
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.layout = m.Layout(self.base / 'users', (self.base,))
        self.sessions = {'opaque-token': m.SessionIdentity('user001', 100)}
        self.a = m.from_session('opaque-token', self.sessions.get, self.layout, now=10)
        self.b = m.UserContext('user002', self.layout)

    def test_username_is_not_a_session(self):
        for token in ('', 'user001', 'unknown'):
            with self.subTest(token=token), self.assertRaises(m.AccessDenied):
                m.from_session(token, self.sessions.get, self.layout, now=10)

    def test_expired_revoked_and_invalid_expiry(self):
        for expiry in (10, 0, float('nan'), float('inf')):
            self.sessions['opaque-token'] = m.SessionIdentity('user001', expiry)
            with self.assertRaises(m.AccessDenied):
                m.from_session('opaque-token', self.sessions.get, self.layout, now=10)
        self.sessions.clear()
        with self.assertRaises(m.AccessDenied):
            m.from_session('opaque-token', self.sessions.get, self.layout, now=10)

    def test_same_project_id_is_separate(self):
        self.assertNotEqual(self.a.project('same'), self.b.project('same'))
        with self.assertRaises(m.AccessDenied):
            self.b.path(self.a.project('same'))

    def test_traversal_and_windows_aliases(self):
        for path in ('../user002/file', 'assets/../../x', '..\\user002\\file',
                     'C:relative', '\\\\server\\share', 'assets/file:stream', 'assets/NUL.txt', 'assets/name.'):
            with self.subTest(path=path), self.assertRaises(m.AccessDenied):
                self.a.path(path)

    def test_configured_root_must_be_permitted(self):
        with self.assertRaises(m.AccessDenied):
            m.Layout(self.base.parent / 'elsewhere', (self.base,))

    def test_project_ids_cannot_escape(self):
        for value in ('../x', 'a/b', 'C:x', 'NUL', ''):
            with self.subTest(value=value), self.assertRaises(m.AccessDenied):
                self.a.project(value)

    def test_reparse_and_symlink_ancestors_rejected(self):
        # Simulate both NTFS junction metadata and POSIX symlink metadata, without
        # requiring Windows developer mode or creating filesystem links.
        original = Path.lstat
        linked = self.a.root / 'linked'
        for mode, attributes in ((stat.S_IFDIR, stat.FILE_ATTRIBUTE_REPARSE_POINT), (stat.S_IFLNK, 0)):
            def metadata(path, *args, **kwargs):
                if path == linked:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                return original(path, *args, **kwargs)
            with patch.object(Path, 'lstat', metadata), self.assertRaises(m.AccessDenied):
                self.a.path(linked / 'not-yet-created.wav')

    def test_root_rechecked_after_context_created(self):
        root = self.a.root
        original = Path.lstat
        def metadata(path, *args, **kwargs):
            if path == root:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'lstat', metadata), self.assertRaises(m.AccessDenied):
            self.a.project('same')

    def receipt_fixture(self):
        output = self.a.task('same', 'task1') / 'result.wav'
        output.parent.mkdir(parents=True)
        output.write_bytes(b'ownership-test-only')
        return m.TaskOwner('user001', 'same', 'task1'), {
            'owner_user': 'user001', 'project_id': 'same', 'task_id': 'task1', 'output': str(output)}

    def test_receipt_ownership_success(self):
        owner, receipt = self.receipt_fixture()
        self.assertEqual(m.verify_receipt(self.a, owner, receipt), Path(receipt['output']))

    def test_receipt_cannot_forge_task_owner(self):
        owner, receipt = self.receipt_fixture()
        with self.assertRaises(m.AccessDenied):
            m.verify_receipt(self.b, owner, {**receipt, 'owner_user': 'user002'})
        for key in ('owner_user', 'project_id', 'task_id'):
            with self.subTest(key=key), self.assertRaises(m.AccessDenied):
                m.verify_receipt(self.a, owner, {**receipt, key: 'other'})

    def test_receipt_cannot_use_other_task_output(self):
        owner, receipt = self.receipt_fixture()
        other = self.a.task('same', 'task2') / 'other.wav'
        other.parent.mkdir(parents=True)
        other.write_bytes(b'other')
        with self.assertRaises(m.AccessDenied):
            m.verify_receipt(self.a, owner, {**receipt, 'output': str(other)})


class RealJunctionTests(unittest.TestCase):
    fixture_root = None

    def test_real_junction_ancestor_escape(self):
        if self.fixture_root is None:
            self.skipTest('Pass --junction-root with an existing retained Windows fixture')
        base = Path(self.fixture_root).resolve()
        layout = m.Layout(base / 'users')
        context = m.UserContext('user001', layout)
        linked = context.root / 'linked'
        target = base / 'users/user002/private'
        self.assertTrue(linked.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        self.assertEqual(linked.resolve(), target.resolve())
        results = []
        # The final child does not exist: only ancestor inspection can identify
        # the junction before a write. Repeat for relative and absolute inputs.
        for value in ('linked/not-created/child.wav', str(linked / 'not-created/child.wav')):
            with self.assertRaisesRegex(m.AccessDenied, 'Linked directories/files'):
                context.path(value)
            results.append({'input': value, 'rejected': True, 'reason': 'linked ancestor'})
        with self.assertRaisesRegex(m.AccessDenied, 'Linked directories/files'):
            m.Layout(linked / 'nested')
        self.assertEqual(context.path('ordinary/new.wav'), context.root / 'ordinary/new.wav')
        self.assertFalse((target / 'not-created').exists())
        (base / 'verification.json').write_text(json.dumps({
            'junction': str(linked), 'target': str(target), 'real_reparse_point': True,
            'checks': results, 'linked_layout_rejected': True,
            'ordinary_path_accepted': True, 'escape_target_untouched': True,
            'fixture_retained': True}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    if '--junction-root' in sys.argv:
        index = sys.argv.index('--junction-root')
        RealJunctionTests.fixture_root = sys.argv[index + 1]
        del sys.argv[index:index + 2]
    unittest.main()
