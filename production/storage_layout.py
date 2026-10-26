"""Trusted machine storage configuration shared by production and workbench.

``root`` is the workbench data root; ``shared_root`` is the external ComfyUI
shared media root. Runtime, environments and models remain under the checkout
or the ComfyUI root. Only explicitly configured asset roots are relocated;
client requests cannot select roots.
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


def storage_roots(root, shared_root=None):
    root = Path(root).resolve()
    shared = Path(shared_root).resolve() if shared_root is not None else root
    config = root / 'config/storage.json'
    defaults = {
        'input': shared / 'input',
        'output': shared / 'output',
        'workspaces': root / 'projects/director-workbench/workspaces',
        'private': root / 'private-workspaces',
        # 用户素材库（音色/配乐/参考图归档）。旧配置没有这一项时退回数据根下。
        'assets': shared / '素材库',
        # 归档成片：按作品与日期分文件夹，删除临时作品时不能碰这里。
        'works_archive': shared / '作品',
    }
    if not config.exists():
        return defaults  # Existing test fixtures and unconfigured checkouts.
    document = json.loads(config.read_text(encoding='utf-8-sig'))
    required = set(defaults) - {'assets', 'works_archive'}
    configured = document.get('roots', {})
    if document.get('schema_version') != 1 or set(configured) not in (required, set(defaults)):
        raise ValueError('Invalid machine storage configuration')
    roots = {key: checked_root(value) for key, value in {**defaults, **configured}.items()}
    for key, path in roots.items():
        for other, candidate in roots.items():
            if key == 'workspaces' and other == 'private' and path == candidate / 'user001/series':
                continue  # The legacy single-user UI creates administrator works.
            if key != other and (path == candidate or path.is_relative_to(candidate)):
                raise ValueError('Asset roots must not overlap')
    return roots


def relocate_asset(path, root, roots=None, shared_root=None):
    """Translate old asset references without changing historical receipts."""
    root = Path(root).resolve()
    shared = Path(shared_root).resolve() if shared_root is not None else root
    roots = roots or storage_roots(root, shared_root=shared)
    path = Path(path)
    mappings = (*legacy_mappings(root),
        (shared / 'input', roots['input']),
        (shared / 'output', roots['output']),
        (root / 'projects/director-workbench/workspaces', roots['workspaces']),
        (root / 'private-workspaces', roots['private']),
    )
    # Reject traversal before resolve() can erase it.
    if '..' in path.parts:
        raise ValueError('Asset traversal is forbidden')
    for old, new in sorted(mappings, key=lambda pair: len(pair[0].parts), reverse=True):
        if path.is_relative_to(old):
            return new / path.relative_to(old)
    return path


def legacy_mappings(root):
    config = Path(root) / 'config/storage.json'
    if not config.exists():
        return ()
    return _legacy_mappings(config, config.stat().st_mtime_ns)


@lru_cache(maxsize=8)
def _legacy_mappings(config, mtime_ns):
    document = json.loads(config.read_text(encoding='utf-8-sig'))
    return tuple((checked_root(item['source']), checked_root(item['destination']))
                 for item in document.get('legacy_assets', []))
