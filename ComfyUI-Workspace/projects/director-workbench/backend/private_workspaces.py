"""Trusted project ownership, independent of editable project manifests."""
import json
import shutil
from pathlib import Path
import threading
import uuid

from fastapi import HTTPException

from .private_auth import current_principal
from .user_context import Layout, UserContext, directory_label, no_links, project_location


class PrivateWorkspaces:
    def __init__(self, layout: Layout, legacy_file_allowed=None):
        self.layout = layout
        self.registry = layout.root / 'ownership.json'
        self.lock = threading.RLock()
        self.legacy_file_allowed = legacy_file_allowed

    def read(self):
        no_links(self.registry)
        if not self.registry.exists():
            return {'schema_version': 1, 'projects': {}, 'selected': {}}
        value = json.loads(self.registry.read_text(encoding='utf-8'))
        if value.get('schema_version') != 1 or not isinstance(value.get('projects'), dict) or not isinstance(value.get('selected'), dict):
            raise ValueError('Invalid ownership registry')
        return value

    def write(self, value):
        no_links(self.registry)
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.registry.with_name('.' + uuid.uuid4().hex + '.tmp')
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
        temporary.replace(self.registry)

    def context(self, principal=None):
        principal = principal or current_principal.get()
        if principal is None:
            raise HTTPException(401, '请登录工作台')
        return UserContext(principal.identity.username, self.layout)

    def require(self, project_id, principal=None):
        principal = principal or current_principal.get()
        if principal is None:
            return  # Trusted in-process workers use frozen globally unique IDs.
        with self.lock:
            registry = self.read()
            owner = registry['projects'].get(project_id)
            if project_id in registry.get('retired', {}):
                raise HTTPException(404, '作品不存在')
        if owner != principal.identity.username and not (owner is None and principal.administrator):
            raise HTTPException(404, '作品不存在')
        if owner is not None and project_id in registry.get('locations', {}):
            try:
                if not UserContext(owner, self.layout).project(project_id).is_dir():
                    raise HTTPException(404, '作品目录不存在')
            except ValueError as exc:
                raise HTTPException(404, '作品目录不存在') from exc

    def ids(self, principal=None):
        principal = principal or current_principal.get()
        with self.lock:
            registry = self.read()
            return [key for key, owner in registry['projects'].items() if owner == principal.identity.username and key not in registry.get('retired', {})]

    def series_tree(self):
        """List only the signed-in account's immediate series and work folders."""
        context = self.context()
        series_root = context.path('series')
        if not series_root.exists():
            return []
        with self.lock:
            registry = self.read()
        locations = {
            tuple(Path(location).parts[1:]): project_id
            for project_id, location in registry.get('locations', {}).items()
            if registry['projects'].get(project_id) == context.username and project_id not in registry.get('retired', {})
            and len(Path(location).parts) == 3 and Path(location).parts[0] == 'series'
        }
        result = []
        for series in sorted(series_root.iterdir(), key=lambda item: item.name.casefold()):
            try:
                series = context.path(series)
                if not series.is_dir():
                    continue
                works = []
                for work in sorted(series.iterdir(), key=lambda item: item.name.casefold()):
                    try:
                        work = context.path(work)
                    except (ValueError, OSError):
                        continue
                    if work.is_dir():
                        works.append({'name': work.name, 'project_id': locations.get((series.name, work.name))})
                result.append({'name': series.name, 'works': works})
            except (ValueError, OSError):
                continue
        return result

    def create_series(self, name):
        name = str(name).strip()
        if not name or name != directory_label(name, '未命名系列'):
            raise HTTPException(422, '系列名称不能包含路径符号或超过 28 个字符')
        path = self.context().path(Path('series') / name)
        with self.lock:
            if path.exists():
                raise HTTPException(409, '这个系列已经存在')
            path.mkdir(parents=True, exist_ok=False)
        return {'name': name, 'works': []}

    def deletion_target(self, series_name, work_name=None):
        context = self.context()
        root = context.path('series', must_exist=True)
        series = context.path(root / series_name, must_exist=True)
        if series.parent != root or not series.is_dir():
            raise HTTPException(404, '系列不存在')
        if work_name is None:
            return root, series
        work = context.path(series / work_name, must_exist=True)
        if work.parent != series or not work.is_dir():
            raise HTTPException(404, '作品目录不存在')
        return series, work

    def retire_deleted(self, series_name, work_name=None):
        context = self.context()
        with self.lock:
            registry = self.read()
            for project_id, location in registry.get('locations', {}).items():
                parts = Path(location).parts
                if (registry['projects'].get(project_id) == context.username and len(parts) == 3
                        and parts[0] == 'series' and parts[1] == series_name
                        and (work_name is None or parts[2] == work_name)):
                    registry.setdefault('retired', {})[project_id] = True
                    if registry['selected'].get(context.username) == project_id:
                        registry['selected'].pop(context.username, None)
            self.write(registry)

    def retire_legacy_registration(self, project_id):
        """Hide an administrator's old D-drive registration without deleting shared source files."""
        principal = current_principal.get()
        if principal is None or not principal.administrator:
            raise HTTPException(404, '作品不存在')
        with self.lock:
            registry = self.read()
            if project_id in registry['projects'] or project_id in registry.get('retired', {}):
                raise HTTPException(404, '旧作品登记不存在')
            registry.setdefault('retired', {})[project_id] = True
            if registry['selected'].get(principal.identity.username) == project_id:
                registry['selected'].pop(principal.identity.username, None)
            self.write(registry)

    def archive_media(self, series_name, work_name, limit=200):
        """Preview media in an unregistered work without changing or importing it."""
        context = self.context()
        series = context.path(Path('series') / series_name, must_exist=True)
        work = context.path(series / work_name, must_exist=True)
        if series.parent != context.path('series') or work.parent != series or not work.is_dir():
            raise HTTPException(404, '目录不存在')
        suffixes = {'.mp4': 'video', '.mov': 'video', '.webm': 'video', '.mkv': 'video',
                    '.wav': 'audio', '.mp3': 'audio', '.flac': 'audio', '.m4a': 'audio', '.aac': 'audio',
                    '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image', '.gif': 'image'}
        import os
        files = []
        scanned = 0
        for root, dirs, names in os.walk(work, followlinks=False):
            safe_dirs = []
            for name in dirs:
                if name.startswith('.'):
                    continue
                try:
                    if context.path(Path(root) / name).is_dir():
                        safe_dirs.append(name)
                except (ValueError, OSError):
                    continue
            dirs[:] = sorted(safe_dirs)
            for name in sorted(names):
                scanned += 1
                if scanned > 3000 or len(files) >= limit:
                    return {'files': files, 'limited': True}
                kind = suffixes.get(Path(name).suffix.lower())
                if not kind:
                    continue
                try:
                    path = context.path(Path(root) / name)
                except (ValueError, OSError):
                    continue
                if path.is_file():
                    files.append({'name': name, 'path': str(path), 'kind': kind,
                                  'relative_path': str(path.relative_to(work))})
        return {'files': files, 'limited': False}

    def allocate(self, requested, principal=None, *, series='', title=''):
        principal = principal or current_principal.get()
        context = self.context(principal)
        # Display title stays user-supplied; database identity is server-generated.
        project_id = 'p-' + uuid.uuid4().hex
        location = project_location(series, title or requested, project_id)
        path = context.path(location)
        with self.lock:
            registry = self.read()
            path.mkdir(parents=True, exist_ok=False)
            registry['projects'][project_id] = principal.identity.username
            registry.setdefault('locations', {})[project_id] = location.as_posix()
            self.write(registry)
        return project_id, path

    def selected(self, principal=None):
        principal = principal or current_principal.get()
        with self.lock:
            registry = self.read()
            project_id = registry['selected'].get(principal.identity.username)
        if project_id and project_id in registry.get('retired', {}):
            return None
        if project_id:
            try:
                self.require(project_id, principal)
            except HTTPException as exc:
                if exc.status_code == 404:
                    return None
                raise
        return project_id

    def select(self, project_id, principal=None):
        principal = principal or current_principal.get()
        self.require(project_id, principal)
        with self.lock:
            registry = self.read()
            registry['selected'][principal.identity.username] = project_id
            self.write(registry)

    def file(self, path: Path, principal=None):
        """Private source paths may only address the current account's tree."""
        principal = principal or current_principal.get()
        try:
            return self.context(principal).path(str(path))
        except ValueError as exc:
            if principal and principal.administrator and self.legacy_file_allowed:
                try:
                    candidate = no_links(path)
                    if not candidate.is_relative_to(self.layout.root) and self.legacy_file_allowed(candidate):
                        return candidate
                except (ValueError, OSError):
                    pass
            raise HTTPException(403, '素材不属于当前工作区') from exc

    def freeze_owner(self, payload, project_id):
        with self.lock:
            owner = self.read()['projects'].get(project_id)
        if owner is not None:
            self.require(project_id)
            payload.update(owner_user=owner, project_id=project_id)
        return payload

    def verify_manifest(self, manifest, root):
        with self.lock:
            owner = self.read()['projects'].get(manifest['id'])
        if owner is None:
            return
        expected = UserContext(owner, self.layout).project(manifest['id'])
        try:
            def resolve(value):
                path = Path(value)
                return no_links(path if path.is_absolute() else root / path)
            workspace = resolve(manifest['workspace_root'])
            plan = resolve(manifest['plan_path'])
            if workspace != expected or not plan.is_relative_to(expected):
                raise ValueError('Untrusted project paths')
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise HTTPException(409, '项目目录与服务器归属不一致，已阻止访问') from exc

    def verify_task(self, task):
        with self.lock:
            owner = self.read()['projects'].get(task['project_id'])
        payload = task['payload']
        if owner is not None or payload.get('owner_user') is not None:
            if owner is None or payload.get('owner_user') != owner or payload.get('project_id') != task['project_id']:
                raise HTTPException(409, '任务冻结归属不一致，已阻止执行或登记结果')
        return owner

    def snapshots(self, project_id):
        with self.lock:
            owner = self.read()['projects'].get(project_id)
        if owner is None:
            return None
        context = UserContext(owner, self.layout)
        return context.path(context.project(project_id) / 'snapshots')

    def publish(self, task, source: Path, output_root: Path, index=0):
        """Copy a verified engine result to its frozen owner's project.

        Shared engine files are retained as execution evidence, not exposed as
        general member browsing roots. Publication does not imply review approval.
        """
        owner = self.verify_task(task)
        if owner is None:
            return source
        context = UserContext(owner, self.layout)
        source = no_links(source)
        task_root = context.task(task['project_id'], task['id'])
        if source.is_relative_to(task_root) and source.is_file():
            return source
        allowed = no_links(output_root / '导演工作台工作目录' / task['project_id'])
        if not source.is_relative_to(allowed) or not source.is_file():
            raise HTTPException(409, '生成结果不属于此任务的作品目录')
        destination = task_root / f'{index:02d}{source.suffix.lower()}'
        destination = context.path(destination)
        with self.lock:
            before = source.stat()
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.stat().st_size != before.st_size:
                    raise HTTPException(409, '已发布结果与原任务不一致，请人工核对')
                return destination
            temporary = context.path(destination.with_name('.' + uuid.uuid4().hex[:16] + '.tmp'))
            with source.open('rb') as incoming, temporary.open('xb') as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            after = source.stat()
            if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns or temporary.stat().st_size != before.st_size:
                raise HTTPException(409, '复制期间原结果改变，尚未发布')
            temporary.replace(destination)
        return destination
