"""Removal for the directory trees the test tooling owns.

Two facts about those trees are worth stating once instead of at each call site:

* pytest's fixtures contain git repositories, and git marks its loose objects read-only.
  ``rmtree`` refuses a read-only file on Windows, so without the retry below one such
  file strands a whole run root — the retention code reports the failure and the tree
  stays for good, which is the accumulation retention exists to stop.
* a fixture can be a junction.  On the interpreter this project runs, ``rmtree`` treats a
  reparse point as a link and unlinks it rather than descending through it, so a removal
  cannot reach a target outside the tree it was given.  ``Path.is_symlink`` is False for a
  junction, so callers cannot filter them out by that test alone.

``tests/test_runtime_scratch.py`` pins both behaviours down for the callers in
``tools/run_project_tests.py`` and ``tests/runtime_scratch.py``.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat


def _retry_without_readonly(function, path, exc) -> None:
    """``rmtree`` callback: clear the read-only bit and retry that one entry."""
    if not isinstance(exc, PermissionError):
        raise exc
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_tree(path: Path) -> None:
    """Remove ``path`` with whatever it holds: a file, a directory, or a link to either.

    The link itself goes; what it points at is left alone.
    """
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    shutil.rmtree(path, onexc=_retry_without_readonly)
