"""Scratch directories for tests that have to write inside the machine runtime tree.

The machine runtime tree also holds live roots — accounts, agent pages, the checkout's
relocation backup — so a test does not invent a directory there on its own: it asks
here and gets a directory this module owns and prunes.  Both ways in are bounded:

* Under the suite harness the caller exports ``DIRECTOR_TEST_RUNTIME``, that run's run
  root.  Scratch directories land inside it and go away with the run root, which
  ``tools/run_project_tests.py`` prunes as a unit.
* A hand-run interpreter has no run root, so the historical fixed directory under
  ``paths.RUNTIME_ROOT`` is used and it keeps only the newest entries.

Pruning touches entries whose name starts with the caller's prefix, so anything else
dropped in the same directory — and any entry another run created moments ago — is
left alone.  Diagnostic artifacts of the newest runs therefore survive a run; only
the accumulation does not.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths
from tools.remove_tree import remove_tree

# Exported by tools/run_project_tests.py for the suites that run the project's own
# pytest suite, where the scratch directories are a subset of that run's artifacts.
OVERRIDE = 'DIRECTOR_TEST_RUNTIME'
KEEP = 4
MIN_AGE_SECONDS = 900.0


def base_root() -> Path:
    """Where scratch directories go: this run's run root, else the runtime tree.

    The checkout is refused because test artifacts must never enter the published
    tree, and a stray exported variable is a cheap way for them to do exactly that.
    """
    value = os.environ.get(OVERRIDE)
    root = Path(value).resolve() if value else Path(paths.RUNTIME_ROOT)
    if root == Path(paths.PROJECT_ROOT).resolve() or root.is_relative_to(Path(paths.PROJECT_ROOT).resolve()):
        raise ValueError(f'{OVERRIDE} points inside the checkout ({root}); '
                         'test artifacts must not enter the published tree')
    return root


def _mtime(entry: Path) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def prune(root: Path, *, prefix: str = 'tmp', keep: int = KEEP,
          min_age: float = MIN_AGE_SECONDS, now: float | None = None) -> list[Path]:
    """Remove ``<prefix>*`` entries of ``root`` beyond the newest ``keep``.

    Entries younger than ``min_age`` are kept too: a second run started in parallel
    must not have the directory it is using deleted underneath it.  Returns what was
    removed.

    A junction is removed but not followed: ``is_symlink`` is False for one, so it does
    reach the removal below, where rmtree unlinks a reparse point rather than reading
    through it (tests/test_runtime_scratch.py asserts that for both removal sites).
    """
    if keep < 0:
        raise ValueError('keep must not be negative')
    now = time.time() if now is None else now
    entries = [entry for entry in root.iterdir()
               if entry.name.startswith(prefix) and not entry.is_symlink()]
    entries.sort(key=_mtime, reverse=True)
    removed = []
    for entry in entries[keep:]:
        if now - _mtime(entry) < min_age:
            continue
        try:
            # remove_tree covers what plain rmtree does not: the read-only git objects
            # a fixture can leave behind, and a junction, which is not a symlink.
            remove_tree(entry)
        except OSError:
            continue
        removed.append(entry)
    return removed


def scratch_root(name: str, *, prefix: str = 'tmp', keep: int = KEEP) -> Path:
    """Create and return the pruned ``name`` scratch directory for one test module."""
    root = base_root() / name
    root.mkdir(parents=True, exist_ok=True)
    prune(root, prefix=prefix, keep=keep)
    return root
