import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_scratch as scratch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths
from tools import run_project_tests as harness
from tools.remove_tree import remove_tree


def aged(path: Path, when: float) -> Path:
    """Give an existing entry a known age so retention is asserted, not inferred."""
    os.utime(path, (when, when))
    return path


def old_directory(root: Path, name: str, when: float) -> Path:
    (root / name).mkdir(parents=True)
    return aged(root / name, when)


def make_junction(link: Path, target: Path) -> bool:
    """Create a real NTFS junction at ``link``; False where the platform will not.

    A junction matters here rather than a symlink: ``Path.is_symlink`` reports False for
    one, so the guard in ``prune`` does not catch it and it reaches the removal code.
    mklink answers in the console code page, so its output is left undecoded.
    """
    created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                             capture_output=True)
    return created.returncode == 0 and os.path.isjunction(link)


class RemoveTreeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='remove-tree-'))
        self.addCleanup(remove_tree, self.root)

    def test_a_readonly_file_does_not_strand_the_tree(self):
        # A live run hit exactly this: pytest's git fixtures leave .git/objects read-only,
        # rmtree refuses them on Windows, and the whole run root survived the prune.
        stale = self.root / 't-aa11bb22'
        objects = stale / '.git/objects/56'
        objects.mkdir(parents=True)
        blocked = objects / '8c6374fcf06ab22ebc79d816af6f9292b100df'
        blocked.write_bytes(b'x')
        os.chmod(blocked, stat.S_IREAD)
        remove_tree(stale)
        self.assertFalse(stale.exists())

    def test_a_link_goes_and_its_target_stays(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('not a test artifact\n', encoding='utf-8')
        holder = self.root / 'holder'
        holder.mkdir()
        if not make_junction(holder / 'linked', outside):
            self.skipTest('junctions are unavailable for this account')
        remove_tree(holder)
        self.assertFalse(holder.exists())
        self.assertTrue((outside / 'keep.txt').is_file())

    def test_a_plain_file_is_removed(self):
        loose = self.root / 'scratch.json'
        loose.write_text('{}\n', encoding='utf-8')
        remove_tree(loose)
        self.assertFalse(loose.exists())


class ScratchRootTests(unittest.TestCase):
    def setUp(self):
        self.elsewhere = Path(tempfile.mkdtemp(prefix='scratch-tests-'))

    def test_override_wins_over_the_runtime_tree(self):
        with patch.dict(os.environ, {scratch.OVERRIDE: str(self.elsewhere)}):
            root = scratch.scratch_root('audio-edit-tests', prefix='case-')
        # scratch_root resolves its root, so a runner whose temp path keeps an 8.3 short name
        # (RUNNER~1) must be compared after resolving both sides.
        self.assertEqual(root, (self.elsewhere / 'audio-edit-tests').resolve())
        self.assertTrue(root.is_dir())

    def test_override_inside_the_checkout_is_refused(self):
        inside = Path(harness.PROJECT) / 'tests'
        with patch.dict(os.environ, {scratch.OVERRIDE: str(inside)}):
            with self.assertRaises(ValueError):
                scratch.scratch_root('audio-edit-tests')

    def test_a_hand_run_fallback_is_project_owned(self):
        # A hand-run pytest exports no run root. Scratch still belongs to the project:
        # it lands in the checkout's ignored .t directory, not the OS temp dir, a drive
        # root, or the machine runtime tree.
        with patch.dict(os.environ):
            os.environ.pop(scratch.OVERRIDE, None)
            root = scratch.base_root()
            created = scratch.scratch_root('audio-edit-tests')
        checkout = Path(harness.PROJECT).resolve()
        self.assertEqual(root, checkout / scratch.FALLBACK_DIRNAME)
        self.assertTrue(created.is_relative_to(checkout))
        self.assertNotEqual(root, Path(paths.RUNTIME_ROOT).resolve())

    def test_override_inside_the_project_scratch_is_allowed(self):
        allowed = Path(harness.PROJECT) / scratch.FALLBACK_DIRNAME
        with patch.dict(os.environ, {scratch.OVERRIDE: str(allowed)}):
            self.assertEqual(scratch.base_root(), allowed.resolve())

    def test_prune_keeps_the_newest_and_leaves_foreign_names(self):
        root = self.elsewhere / 'audio-edit-tests'
        root.mkdir(parents=True)
        for index in range(6):
            old_directory(root, f'case-{index}', 1000 + index)
        (root / 'notes.txt').write_text('left by a human\n', encoding='utf-8')
        foreign = aged(root / 'notes.txt', 1)
        removed = scratch.prune(root, prefix='case-', keep=2, min_age=0)
        self.assertEqual(sorted(path.name for path in removed),
                         [f'case-{index}' for index in range(4)])
        self.assertEqual(sorted(path.name for path in root.iterdir()),
                         ['case-4', 'case-5', 'notes.txt'])
        self.assertTrue(foreign.is_file())

    def test_prune_spares_entries_a_parallel_run_created(self):
        root = self.elsewhere / 'audio-edit-tests'
        root.mkdir(parents=True)
        old_directory(root, 'case-old', 1000)
        old_directory(root, 'case-live', 5000)
        removed = scratch.prune(root, prefix='case-', keep=0, min_age=900, now=5500)
        self.assertEqual([path.name for path in removed], ['case-old'])
        self.assertTrue((root / 'case-live').is_dir())

    def test_prune_skips_symlinks_and_rejects_a_negative_keep(self):
        root = self.elsewhere / 'audio-edit-tests'
        root.mkdir(parents=True)
        target = old_directory(root, 'foreign', 1000)
        link = root / 'case-link'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks are unavailable for this account')
        scratch.prune(root, prefix='case-', keep=0, min_age=0)
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.is_dir())
        with self.assertRaises(ValueError):
            scratch.prune(root, keep=-1)

    def test_prune_removes_a_junction_not_what_it_points_at(self):
        # A junction reaches the removal code, because is_symlink() is False for one.
        root = self.elsewhere / 'audio-edit-tests'
        root.mkdir(parents=True)
        outside = old_directory(root, 'outside', 5000)
        (outside / 'keep.txt').write_text('not a test artifact\n', encoding='utf-8')
        case = root / 'case-linked'
        case.mkdir()
        if not make_junction(case / 'linked', outside):
            self.skipTest('junctions are unavailable for this account')
        # Linked first, then aged: creating an entry inside a directory ages the directory.
        aged(case, 1000)
        removed = scratch.prune(root, prefix='case-', keep=0, min_age=0)
        self.assertEqual([path.name for path in removed], ['case-linked'])
        self.assertEqual([entry.name for entry in root.iterdir()], ['outside'])
        self.assertTrue((outside / 'keep.txt').is_file())


class RunRootRetentionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='run-roots-'))

    def layout(self, *names: str) -> Path:
        for index, name in enumerate(names):
            old_directory(self.root, name, 1000 + index)
        return self.root

    def test_only_the_newest_run_roots_survive(self):
        self.layout('t-18a44e3a', 't-22860a837bd3', 't-e65862b6e9ce', 't-f1f67053')
        removed, errors = harness.prune_run_roots(self.root, keep=2)
        self.assertEqual(errors, [])
        self.assertEqual([path.name for path in removed], ['t-22860a837bd3', 't-18a44e3a'])
        self.assertEqual(sorted(entry.name for entry in self.root.iterdir()),
                         ['t-e65862b6e9ce', 't-f1f67053'])

    def test_other_roots_in_the_tree_are_never_candidates(self):
        # The runtime tree is shared with live roots; a prefix test would eat them.
        self.layout('t-18a44e3a', 'relocation-backup-1792221329', 'audio-edit-tests',
                    'private-session-tests', 'user', 't-fe68fd51e174-backup', 't-nothex')
        removed, errors = harness.prune_run_roots(self.root, keep=0)
        self.assertEqual(errors, [])
        self.assertEqual([path.name for path in removed], ['t-18a44e3a'])
        self.assertEqual(len(list(self.root.iterdir())), 6)

    def test_a_missing_runtime_tree_is_not_an_error(self):
        self.assertEqual(harness.prune_run_roots(self.root / 'absent', keep=0), ([], []))

    def test_pruning_a_run_root_leaves_a_junction_target_alone(self):
        # The suite's own fixtures contain junctions, so pruning walks over them.
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('not a run artifact\n', encoding='utf-8')
        stale = self.root / 't-aa11bb22'
        stale.mkdir()
        if not make_junction(stale / 'linked', outside):
            self.skipTest('junctions are unavailable for this account')
        aged(stale, 1000)
        old_directory(self.root, 't-bb22cc33', 2000)
        removed, errors = harness.prune_run_roots(self.root, keep=1)
        self.assertEqual(errors, [])
        self.assertEqual([path.name for path in removed], ['t-aa11bb22'])
        self.assertFalse(stale.exists())
        self.assertTrue((outside / 'keep.txt').is_file())

    def test_a_run_keeps_its_own_root(self):
        # keep_runs counts the root the run is about to create, so 0 would delete it.
        with self.assertRaises(ValueError):
            harness.run('core', keep_runs=0)


if __name__ == '__main__':
    unittest.main()
