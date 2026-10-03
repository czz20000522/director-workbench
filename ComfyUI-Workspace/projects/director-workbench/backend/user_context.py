"""Trusted-session and filesystem ownership primitives; no HTTP or task scheduler.

Only server authentication code may supply session_lookup and construct Layout.
TaskOwner must come from server-owned storage, never an uploaded receipt.
Path checks do not provide OS-level isolation against concurrent filesystem edits.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import time
from typing import Callable

PERMITTED_ROOT = Path('D:/Comfy-Desktop/ComfyUI-Workspace')
DEFAULT_ROOT = Path('E:/DirectorWorkspaces')


class AccessDenied(ValueError):
    pass


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise AccessDenied('Invalid identifier')
    if value.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        raise AccessDenied('Reserved Windows filename')
    return value


def directory_label(value: str, fallback: str) -> str:
    """Readable stable labels; immutable project IDs still identify database rows."""
    raw = str(value).strip() or fallback
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', raw).strip(' .')[:28] or fallback
    if clean.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        clean = '_' + clean
    if clean != raw:
        clean += '-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:6]
    return clean


def project_location(series: str, title: str, project_id: str) -> Path:
    return Path('series') / directory_label(series, '未命名系列') / (directory_label(title, '未命名作品') + '--' + identifier(project_id)[-8:])


def no_links(path: Path) -> Path:
    """Reject symlinks and Windows reparse points, including existing ancestors."""
    if not path.is_absolute() or '..' in path.parts:
        raise AccessDenied('Absolute normalized path required')
    for part in (*reversed(path.parents), path):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise AccessDenied('Linked directories/files are not permitted')
    return path.resolve()


@dataclass(frozen=True)
class Layout:
    root: Path = DEFAULT_ROOT
    # Server configuration only. Never populate this allowlist from a request.
    permitted_roots: tuple[Path, ...] = (PERMITTED_ROOT, DEFAULT_ROOT)

    def __post_init__(self):
        root = no_links(Path(self.root))
        allowed = tuple(no_links(Path(p)) for p in self.permitted_roots)
        if not any(root.is_relative_to(p) for p in allowed):
            raise AccessDenied('Workspace root is outside the server allowlist')
        object.__setattr__(self, 'root', root)
        object.__setattr__(self, 'permitted_roots', allowed)


@dataclass(frozen=True)
class SessionIdentity:
    username: str
    expires_at: float


@dataclass(frozen=True)
class UserContext:
    username: str
    layout: Layout

    def __post_init__(self):
        if not isinstance(self.username, str) or not re.fullmatch(r'user[0-9]{3}', self.username):
            raise AccessDenied('Invalid authenticated account')

    @property
    def root(self) -> Path:
        # Recheck on every use, not just when the context was created.
        return no_links(self.layout.root / self.username)

    def path(self, supplied: str | Path, *, must_exist: bool = False) -> Path:
        raw = str(supplied)
        win = PureWindowsPath(raw)
        if not raw or '..' in win.parts or '..' in Path(raw).parts:
            raise AccessDenied('Relative traversal is forbidden')
        if raw.startswith(('\\\\', '//')) or (win.drive and not win.is_absolute()):
            raise AccessDenied('Network/device and drive-relative paths are forbidden')
        # ADS, ambiguous trailing dots/spaces, reserved Windows names are rejected.
        for part in win.parts[1:] if win.anchor else win.parts:
            if ':' in part or part.endswith((' ', '.')) or part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
                raise AccessDenied('Ambiguous Windows path')
        candidate = Path(raw)
        root = self.root
        candidate = candidate if candidate.is_absolute() else root / candidate
        # Leave room for short atomic sibling names and Windows directory APIs.
        # Do not rely on machine-wide long-path settings or add device prefixes.
        if os.name == 'nt' and len(str(candidate)) > 240:
            raise AccessDenied('Path is too long for this Windows workspace; choose a shorter workspace root')
        candidate = no_links(candidate)
        if not candidate.is_relative_to(root):
            raise AccessDenied('Path belongs to another workspace')
        if must_exist and not candidate.exists():
            raise FileNotFoundError(candidate)
        return candidate

    def project(self, project_id: str) -> Path:
        project_id = identifier(project_id)
        registry = no_links(self.layout.root / 'ownership.json')
        if registry.is_file():
            document = json.loads(registry.read_text(encoding='utf-8'))
            location = document.get('locations', {}).get(project_id)
            if location is not None:
                if document.get('projects', {}).get(project_id) != self.username:
                    raise AccessDenied('Project belongs to another account')
                parts = Path(location)
                if parts.is_absolute() or len(parts.parts) != 3 or parts.parts[0] != 'series':
                    raise AccessDenied('Invalid registered project location')
                return self.path(parts)
        # Existing project IDs keep their old location until explicitly migrated.
        return self.path(Path('projects') / project_id)

    def task(self, project_id: str, task_id: str) -> Path:
        return self.path(self.project(project_id) / 'tasks' / identifier(task_id))


def from_session(token: str, session_lookup: Callable[[str], SessionIdentity | None],
                 layout: Layout, *, now: float | None = None) -> UserContext:
    """Lookup must validate revocation/account membership in trusted server state."""
    if not isinstance(token, str) or not token:
        raise AccessDenied('Authentication required')
    identity = session_lookup(token)
    current = time.time() if now is None else now
    if not isinstance(identity, SessionIdentity) or not isinstance(identity.expires_at, (int, float)) or not math.isfinite(identity.expires_at) or identity.expires_at <= current:
        raise AccessDenied('Invalid or expired session')
    return UserContext(identity.username, layout)


@dataclass(frozen=True)
class TaskOwner:
    username: str
    project_id: str
    task_id: str


def verify_receipt(context: UserContext, owner: TaskOwner, receipt: dict) -> Path:
    """Compare untrusted receipt with a trusted frozen task record and output scope.

    This is ownership validation only; the caller still verifies audio/video content,
    frozen generation inputs, status, and provenance before adopting an output.
    """
    if owner.username != context.username:
        raise AccessDenied('Task belongs to another user')
    directory = context.task(owner.project_id, owner.task_id)
    expected = {'owner_user': owner.username, 'project_id': owner.project_id, 'task_id': owner.task_id}
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
        raise AccessDenied('Receipt ownership mismatch')
    if not isinstance(receipt.get('output'), str) or not Path(receipt['output']).is_absolute():
        raise AccessDenied('Receipt requires an absolute output path')
    output = context.path(receipt['output'], must_exist=True)
    if not output.is_relative_to(directory) or not output.is_file():
        raise AccessDenied('Receipt output is outside its task')
    return output
