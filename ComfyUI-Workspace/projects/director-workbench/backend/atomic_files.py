"""Bounded retry of one prepared atomic file replacement on Windows."""
import time
from pathlib import Path


def replace_prepared(temporary: Path, destination: Path) -> None:
    for attempt in range(4):
        try:
            temporary.replace(destination)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in {5, 32} or attempt == 3:
                raise
            # Retry the same bytes, not the operation that prepared them.
            time.sleep(.02 * (attempt + 1))
