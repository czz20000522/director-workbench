"""Trusted machine storage configuration shared by production and workbench.

Runtime, environments and models remain under the checkout. Only explicitly
configured asset roots are relocated; client requests cannot select roots.
"""
import json
from functools import lru_cache
from pathlib import Path
import stat


def checked_root(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Storage roots must be absolute normalized paths')
    for ancestor in (*reversed(path.parents), path):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Storage roots cannot contain links or junctions')
    return path.resolve()


def storage_roots(root):
    root = Path(root).resolve()
    config = root / 'ComfyUI-Workspace/config/storage.json'
    defaults = {
        'input': root / 'ComfyUI-Shared/input',
        'output': root / 'ComfyUI-Shared/output',
        'workspaces': root / 'ComfyUI-Workspace/projects/director-workbench/workspaces',
        'private': root / 'ComfyUI-Workspace/private-workspaces',
    }
    if not config.exists():
        return defaults  # Existing test fixtures and unconfigured checkouts.
    document = json.loads(config.read_text(encoding='utf-8-sig'))
    if document.get('schema_version') != 1 or set(document.get('roots', {})) != set(defaults):
        raise ValueError('Invalid machine storage configuration')
    roots = {key: checked_root(value) for key, value in document['roots'].items()}
    for key, path in roots.items():
        for other, candidate in roots.items():
            if key == 'workspaces' and other == 'private' and path == candidate / 'user001/series':
                continue  # The legacy single-user UI creates administrator works.
            if key != other and (path == candidate or path.is_relative_to(candidate)):
                raise ValueError('Asset roots must not overlap')
    return roots


def relocate_asset(path, root, roots=None):
    """Translate old asset references without changing historical receipts."""
    root = Path(root).resolve()
    roots = roots or storage_roots(root)
    path = Path(path)
    mappings = (*legacy_mappings(root),
        (root / 'ComfyUI-Shared/input', roots['input']),
        (root / 'ComfyUI-Shared/output', roots['output']),
        (root / 'ComfyUI-Workspace/projects/director-workbench/workspaces', roots['workspaces']),
        (root / 'ComfyUI-Workspace/private-workspaces', roots['private']),
    )
    # Reject traversal before resolve() can erase it.
    if '..' in path.parts:
        raise ValueError('Asset traversal is forbidden')
    for old, new in sorted(mappings, key=lambda pair: len(pair[0].parts), reverse=True):
        if path.is_relative_to(old):
            return new / path.relative_to(old)
    return path


def legacy_mappings(root):
    config = Path(root) / 'ComfyUI-Workspace/config/storage.json'
    if not config.exists():
        return ()
    return _legacy_mappings(config, config.stat().st_mtime_ns)


@lru_cache(maxsize=8)
def _legacy_mappings(config, mtime_ns):
    document = json.loads(config.read_text(encoding='utf-8-sig'))
    return tuple((checked_root(item['source']), checked_root(item['destination']))
                 for item in document.get('legacy_assets', []))
