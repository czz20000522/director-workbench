"""Local reference index over the owner's own storyboard and prompt documents.

The index is deliberately literal: documents are split at their Markdown
headings, prompts are split at the 13 block cues written in those documents,
and search returns the original sentences that contain the query terms.  It
never rewrites, completes or recommends a prompt; every result carries its
source document and line so the owner can inspect it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from .secure_files import copy_checked


LIBRARY_DIRECTORY = 'reference-library'
CONFIG_NAME = 'reference-library.json'
INBOX_DIRECTORY = 'inbox'
THUMB_DIRECTORY = 'thumbs'
DOCUMENT_SUFFIXES = {'.md', '.txt'}
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_INBOX_DEPTH = 3
MAX_IMAGES = 2000

KIND_ORDER = ('写法', '样张', '角色锚点', '影调', '空气感', '光级', '负面', '连续性', '分镜', '素材映射', '流程', '外部方法', '参考')

_HEADING = re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')
_ANCHOR = re.compile(r'〔([^〕\n]{1,12})〕')
_LIGHT = re.compile(r'\bS[0-5]\b')
_SECTION_REF = re.compile(r'§[\d.]+')
_IMAGE_REF = re.compile(r'@(?:图片|道具)\d+[a-z]?')
_CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬'

NODE_LABELS = (
    '片名·场次·段落',
    '时代·地域·内外景·日夜',
    '地点',
    '景别·机位·构图',
    '人物·细节',
    '服装·状态',
    '道具',
    '表演·动作时序',
    '镜头·焦段·光圈·运动',
    '背景·空气',
    '光线',
    '色调·质感',
    '声音·旁白·负面',
)

_CUE_PATTERNS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (0, re.compile(r'《[^》]{1,30}》|第\s*\d+\s*场')),
    (1, re.compile(r'\d{4}\s*年|[〇零一二三四五六七八九]{3,4}\s*年|外景|内景|日夜|黄昏|清晨|午后|夜晚|白天')),
    (2, re.compile(r'地点|位于|坐落|中国|南方|北方|东方|西方|路上|村口|院|房间|室内|楼|巷|街|山|河|田')),
    (3, re.compile(r'景别|机位|构图|画幅|特写|近景|中景|全景|远景|仰角|俯角|平视|双人|单人|侧面|正面|背面|主体占')),
    (4, re.compile(r'画面(?:左侧|右侧|中央|中间|主体)|人物|男性|女性|男孩|女孩|男子|女子|肤色|眉毛|双眼皮|下颌|短发|寸头|脸')),
    (5, re.compile(r'服装|穿着|身穿|上衣|衬衫|夹克|校服|裤子|短裤|鞋|领口|袖口|衣')),
    (6, re.compile(r'道具|自行车|车把|背包|步枪|手枪|酒瓶|红砖|武装带|军衔|水壶')),
    (7, re.compile(r'表演|动作|时序|先|然后|接着|随后|最后|①|②|③|④|⑤|⑥|⑦')),
    (8, re.compile(r'\d{2,3}\s*mm|f/\d|光圈|焦段|运镜|固定机位|不推不摇|推|摇|移|跟')),
    (9, re.compile(r'背景|空气|浮尘|薄雾|雾气|虚化|光带|景深')),
    (10, re.compile(r'光线|S[0-5]\s*级|侧逆光|逆光|侧光|月光|主光|高光|曝光|色温')),
    (11, re.compile(r'色调|质感|影调|〔调-|〔气-|颗粒|柔光|黑柔|胶片')),
    (12, re.compile(r'声音|环境声|旁白|负面|台词|混响|音效|水印|字幕')),
)

_KIND_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('写法', re.compile(r'写法|模板|十三块|13\s*块|13\s*个固定块|固定顺序|骨架')),
    ('样张', re.compile(r'样张|范例|满密度')),
    ('角色锚点', re.compile(r'^〔|变量卡|锚点|人物设定|人物模型|角色|四视图|特写图|设定图')),
    ('影调', re.compile(r'影调|色调|质感|调-\d+|\{?调')),
    ('空气感', re.compile(r'空气|气-\d+')),
    ('光级', re.compile(r'分光级|光级|S0|S1|S2|S3|S4|S5')),
    ('负面', re.compile(r'负面|不要用否定句')),
    ('连续性', re.compile(r'连贯|承接|连续性|硬规矩|跨镜')),
    ('分镜', re.compile(r'分镜|12\s*栏|十二栏')),
    ('素材映射', re.compile(r'映射表|素材清单|道具与场景|场景图|关键道具|图片资源')),
    ('流程', re.compile(r'流程|步骤|操作指南|规则|管理')),
)

_FEATURED_TITLES: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (re.compile(r'13\s*个固定块|十三块模板|固定顺序'), '写法'),
    (re.compile(r'改进版·通用骨架|通用骨架'), '写法'),
    (re.compile(r'〔默30〕'), '角色锚点'),
    (re.compile(r'〔调-07〕'), '影调'),
    (re.compile(r'〔气-07〕'), '空气感'),
    (re.compile(r'S0[–-]S5|分光级'), '光级'),
    (re.compile(r'负面清单'), '负面'),
    (re.compile(r'六条硬规矩|六条'), '连续性'),
    (re.compile(r'12\s*栏|十二栏'), '分镜'),
)

_cache_lock = threading.RLock()
_cache: dict[str, dict[str, Any]] = {}


def config_path(data_root: str | Path) -> Path:
    return Path(data_root) / 'config' / CONFIG_NAME


def library_root(data_root: str | Path) -> Path:
    return Path(data_root) / LIBRARY_DIRECTORY


def inbox_root(data_root: str | Path) -> Path:
    return library_root(data_root) / INBOX_DIRECTORY


def thumb_root(data_root: str | Path) -> Path:
    return library_root(data_root) / THUMB_DIRECTORY


def empty_config() -> dict[str, Any]:
    return {'schema_version': 1, 'documents': [], 'image_directories': []}


def load_config(data_root: str | Path) -> tuple[dict[str, Any], str | None]:
    path = config_path(data_root)
    if not path.is_file():
        return empty_config(), None
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return empty_config(), f'参考库配置无法读取：{path}：{exc}'
    if not isinstance(raw, dict):
        return empty_config(), f'参考库配置格式不正确：{path}'
    documents = [item for item in raw.get('documents') or [] if isinstance(item, dict) and isinstance(item.get('path'), str)]
    directories = [item for item in raw.get('image_directories') or [] if isinstance(item, dict) and isinstance(item.get('path'), str)]
    return {'schema_version': 1, 'documents': documents, 'image_directories': directories}, None


def save_config(data_root: str | Path, config: dict[str, Any]) -> None:
    path = config_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    invalidate(data_root)


def invalidate(data_root: str | Path | None = None) -> None:
    with _cache_lock:
        if data_root is None:
            _cache.clear()
        else:
            _cache.pop(str(Path(data_root).resolve()), None)


def _resolved(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _work_name(value: str | None, fallback: str) -> str:
    name = (value or '').strip()
    if name:
        return name
    match = re.search(r'《([^》]{1,40})》', fallback or '')
    return f'《{match.group(1)}》' if match else '未归类'


def _inbox_work(path: Path, root: Path, fallback: str) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return fallback
    return relative.parts[0] if len(relative.parts) > 1 else fallback


def _document_sources(data_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(path: Path, title: str | None, origin: str, work: str | None, kind: str | None = None) -> None:
        key = str(path).lower()
        if key in seen or path.suffix.lower() not in DOCUMENT_SUFFIXES:
            return
        seen.add(key)
        label = (title or path.stem).strip()
        sources.append({'path': path, 'title': label, 'origin': origin, 'work': _work_name(work, label), 'kind': (kind or '').strip() or None})

    for item in config['documents']:
        add(_resolved(item['path']), item.get('title') or None, 'configured', item.get('work') or None, item.get('kind') or None)
    inbox = inbox_root(data_root)
    if inbox.is_dir():
        for path in sorted(inbox.rglob('*')):
            if not path.is_file() or path.suffix.lower() not in DOCUMENT_SUFFIXES:
                continue
            if len(path.parts) - len(inbox.parts) > MAX_INBOX_DEPTH:
                continue
            add(path, None, 'inbox', _inbox_work(path, inbox, '资料箱'))
    return sources


def _image_sources(data_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(directory: Path, title: str | None, origin: str, work: str | None) -> None:
        key = str(directory).lower()
        if key in seen or not directory.is_dir():
            return
        seen.add(key)
        label = (title or directory.name).strip()
        sources.append({'path': directory, 'title': label, 'origin': origin, 'work': _work_name(work, label)})

    for item in config['image_directories']:
        add(_resolved(item['path']), item.get('title') or None, 'configured', item.get('work') or None)
    add(inbox_root(data_root), '资料箱', 'inbox', '资料箱')
    return sources


def _kind_of(title: str) -> str:
    for kind, pattern in _KIND_RULES:
        if pattern.search(title):
            return kind
    return '参考'


def _plain(text: str) -> str:
    value = text.replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r'```[^\n]*\n?', '', value)
    value = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', value)
    value = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', value)
    value = value.replace('**', '').replace('__', '').replace('`', '')
    value = re.sub(r'^\s{0,3}>\s?', '', value, flags=re.M)
    value = re.sub(r'^\s*[-*+]\s+', '', value, flags=re.M)
    value = re.sub(r'^\s*\|?[\s:|-]{4,}\|?\s*$', '', value, flags=re.M)
    value = value.replace('|', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def _tags(title: str, body: str) -> list[str]:
    found: list[str] = []
    for source in (title, body):
        for code in _ANCHOR.findall(source):
            tag = f'〔{code}〕'
            if tag not in found:
                found.append(tag)
        for pattern in (_LIGHT, _SECTION_REF, _IMAGE_REF):
            for value in pattern.findall(source):
                if value not in found:
                    found.append(value)
    for word in ('景别', '机位', '构图', '光线', '色调', '质感', '空气', '声音', '旁白', '负面', '承接', '分镜', '模板'):
        if word in title or word in body:
            if word not in found:
                found.append(word)
    return found[:16]


def _document_id(path: Path) -> str:
    return 'doc-' + hashlib.sha1(str(path).lower().encode('utf-8')).hexdigest()[:10]


def parse_document(path: Path, title: str, document_id: str, work: str = '未归类', default_kind: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text = path.read_text(encoding='utf-8-sig', errors='replace')
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    if not headings:
        headings = [(0, 1, title)]
    entries: list[dict[str, Any]] = []
    for position, (line_index, level, heading) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        body = '\n'.join(lines[line_index + 1:end]).strip()
        if not body and position + 1 < len(headings):
            # A heading that only introduces child headings is still useful as a
            # table of contents node, but an empty leaf adds nothing to search.
            continue
        if not body and position + 1 == len(headings):
            continue
        identity = hashlib.sha1(f'{document_id}:{line_index}:{heading}'.encode('utf-8')).hexdigest()[:12]
        plain = _plain(body)
        entries.append({
            'id': 'ref-' + identity,
            'title': heading,
            'heading_level': level,
            'kind': default_kind or _kind_of(heading),
            'tags': _tags(heading, body),
            'work': work,
            'document_id': document_id,
            'document_title': title,
            'document_path': str(path),
            'line': line_index + 1,
            'body': body,
            'plain': plain,
        })
    return entries, {'id': document_id, 'title': title, 'path': str(path), 'work': work, 'sections': len(entries)}


def _excerpt(text: str, terms: Iterable[str], width: int = 220) -> str:
    value = ' '.join(text.split())
    if not value:
        return ''
    positions = [value.find(term) for term in terms if term and value.find(term) >= 0]
    start = min(positions) if positions else 0
    start = max(0, start - 60)
    piece = value[start:start + width]
    return ('…' if start else '') + piece + ('…' if start + width < len(value) else '')


def _terms(query: str) -> list[str]:
    cleaned = re.sub(r'\s+', ' ', query.strip())
    if not cleaned:
        return []
    raw = [part for part in re.split(r'[\s，。、；：,.;:!?/\\|]+', cleaned) if part]
    terms: list[str] = []
    for part in raw:
        if re.fullmatch(r'[\u4e00-\u9fff]+', part):
            if len(part) <= 3:
                terms.append(part)
            else:
                terms.extend(part[index:index + 2] for index in range(len(part) - 1))
        elif len(part) >= 2:
            terms.append(part.lower())
    if cleaned.lower() not in terms:
        terms.insert(0, cleaned.lower())
    seen: set[str] = set()
    return [term for term in terms if not (term in seen or seen.add(term))][:16]


def _score(entry: dict[str, Any], terms: list[str]) -> tuple[int, list[str]]:
    title = entry['title'].lower()
    body = entry['plain'].lower()
    tags = ' '.join(entry['tags']).lower()
    score = 0
    matched: list[str] = []
    for term in terms:
        term_score = 0
        if term in title:
            term_score += 14
        if term in tags:
            term_score += 5
        count = body.count(term)
        if count:
            term_score += min(count, 6)
        if term_score:
            matched.append(term)
            score += term_score
    if matched:
        score += len(set(matched)) * 3
    return score, matched


def _summary(entry: dict[str, Any], terms: list[str] | None = None) -> dict[str, Any]:
    return {
        'id': entry['id'],
        'title': entry['title'],
        'kind': entry['kind'],
        'work': entry.get('work', '未归类'),
        'tags': entry['tags'],
        'document_id': entry['document_id'],
        'document_title': entry['document_title'],
        'line': entry['line'],
        'excerpt': _excerpt(entry['plain'], terms or []),
    }


def _material_title(filename: str) -> tuple[str, str]:
    stem = filename
    while True:
        suffix = Path(stem).suffix
        if suffix.lower() not in IMAGE_SUFFIXES:
            break
        stem = Path(stem).stem
    stem = re.sub(r'^\s*\d+\s*[_\-.、 ]*', '', stem).strip()
    category = '其他'
    for candidate in ('角色', '场景', '道具'):
        if stem.startswith(candidate):
            category = candidate
            stem = stem[len(candidate):].lstrip('_-— 、')
            break
    return (stem or filename), category


def _table_descriptions(entries: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """Map an image title to the prompt text in a Markdown table row."""
    descriptions: dict[str, tuple[str, str]] = {}
    for entry in entries:
        for line in entry['body'].splitlines():
            stripped = line.strip()
            if not (stripped.startswith('|') and stripped.endswith('|')):
                continue
            cells = [cell.strip() for cell in stripped.strip('|').split('|')]
            if len(cells) < 3:
                continue
            for cell in cells:
                title = re.sub(r'^@(?:图片|道具)\d+[a-z]?\s*', '', cell).strip()
                if not title or len(title) > 40:
                    continue
                tail = next((value for value in reversed(cells) if value and value != cell), '')
                if len(tail) < 12 or '\\' in tail or ':/' in tail or tail.startswith('http'):
                    continue
                descriptions.setdefault(title, (tail, entry['id']))
                # Mapping tables often append a use note ("训练场环境（视频1/2/3 通用）");
                # index that variant under the bare image name as well.
                bare = re.split(r'[（(]', title, maxsplit=1)[0].strip()
                if bare and len(bare) >= 2:
                    descriptions.setdefault(bare, (tail, entry['id']))
    return descriptions


def _lookup_description(title: str, descriptions: dict[str, tuple[str, str]]) -> tuple[str | None, str | None]:
    if title in descriptions:
        return descriptions[title]
    for key, value in descriptions.items():
        if key.startswith(title) or title in key:
            return value
    return None, None


def _related_entry(title: str, entries: list[dict[str, Any]]) -> str | None:
    base = title
    for suffix in ('全身', '半身', '特写', '场景', '图'):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[:-len(suffix)]
            break
    if len(base) < 2:
        return None
    for entry in entries:
        if base in entry['title'] and entry['kind'] in {'角色锚点', '素材映射', '参考'}:
            return entry['id']
    for entry in entries:
        if base in entry['title']:
            return entry['id']
    for kind in ('素材映射', '角色锚点', '参考'):
        for entry in entries:
            if entry['kind'] == kind and base in entry['plain']:
                return entry['id']
    return None


def _image_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    try:
        for path in directory.rglob('*'):
            if len(Path(path).relative_to(directory).parts) > MAX_INBOX_DEPTH:
                continue
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                files.append(path)
                if len(files) >= MAX_IMAGES:
                    break
    except OSError:
        return files
    return sorted(files)


def ensure_thumbnail(data_root: Path, material_id: str, source: Path) -> Path | None:
    metadata = source.stat()
    destination = thumb_root(data_root) / f'{material_id}-{metadata.st_mtime_ns}.jpg'
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((560, 560))
            if image.mode not in ('RGB', 'L'):
                image = image.convert('RGB')
            image.save(temporary, 'JPEG', quality=82, optimize=True)
        os.replace(temporary, destination)
    except (OSError, ValueError):
        temporary.unlink(missing_ok=True)
        return None
    return destination


def _scan_materials(data_root: Path, config: dict[str, Any], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    descriptions = _table_descriptions(entries)
    materials: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in _image_sources(data_root, config):
        for path in _image_files(source['path']):
            key = str(path.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            work = source['work']
            if source['origin'] == 'inbox':
                work = _work_name(_inbox_work(path, source['path'], '资料箱'), path.stem)
            title, category = _material_title(path.name)
            material_id = 'mat-' + hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]
            description, description_entry = _lookup_description(title, descriptions)
            related = description_entry or _related_entry(title, entries)
            materials.append({
                'id': material_id,
                'title': title,
                'filename': path.name,
                'category': category,
                'work': work,
                'source_title': source['title'],
                'source_path': str(source['path']),
                'path': str(path),
                'size': path.stat().st_size,
                'description': description,
                'related_entry_id': related,
                'tags': [category, source['title']],
            })
    materials.sort(key=lambda item: (item['category'] != '角色', item['category'] != '场景', item['title']))
    return materials


def _signature(data_root: Path, config: dict[str, Any], sources: list[dict[str, Any]], images: list[dict[str, Any]]) -> tuple[Any, ...]:
    parts: list[Any] = [str(config_path(data_root)), config_path(data_root).stat().st_mtime_ns if config_path(data_root).is_file() else 0]
    for source in sources:
        try:
            metadata = source['path'].stat()
            parts.append((str(source['path']), metadata.st_mtime_ns, metadata.st_size))
        except OSError:
            parts.append((str(source['path']), -1, -1))
    for source in images:
        try:
            metadata = source['path'].stat()
            parts.append((str(source['path']), metadata.st_mtime_ns))
        except OSError:
            parts.append((str(source['path']), -1))
    return tuple(parts)


def _pick_featured(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    featured: list[dict[str, Any]] = []
    used: set[str] = set()
    for title_pattern, kind in _FEATURED_TITLES:
        candidates = [entry for entry in entries if entry['id'] not in used and (kind is None or entry['kind'] == kind)]
        match = next((entry for entry in candidates if title_pattern.search(entry['title'])), None)
        if match is None:
            match = next((entry for entry in candidates if title_pattern.search(entry['body'])), None)
        if match is not None:
            featured.append(_summary(match))
            used.add(match['id'])
    return featured


def _work_record(name: str) -> dict[str, Any]:
    return {
        'id': 'work-' + hashlib.sha1(name.encode('utf-8')).hexdigest()[:10],
        'name': name,
        'documents': [],
        'sections': 0,
        'materials': 0,
        'kinds': {},
        'featured': [],
        'covers': [],
    }


def build_index(data_root: str | Path) -> dict[str, Any]:
    root = Path(data_root).resolve()
    config, config_error = load_config(root)
    documents = _document_sources(root, config)
    images = _image_sources(root, config)
    signature = _signature(root, config, documents, images)
    key = str(root)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached['signature'] == signature:
            return cached['library']
    entries: list[dict[str, Any]] = []
    document_records: list[dict[str, Any]] = []
    notes: list[str] = []
    for source in documents:
        path = source['path']
        if not path.is_file():
            notes.append(f'找不到提示词文档：{path}')
            continue
        try:
            if path.stat().st_size > MAX_DOCUMENT_BYTES:
                notes.append(f'文档过大，已跳过：{path}')
                continue
            parsed, record = parse_document(path, source['title'], _document_id(path), source['work'], source.get('kind'))
        except OSError as exc:
            notes.append(f'无法读取文档 {path}：{exc}')
            continue
        record['origin'] = source['origin']
        document_records.append(record)
        entries.extend(parsed)
    entries.sort(key=lambda entry: (entry['document_title'], entry['line']))
    materials = _scan_materials(root, config, entries) if entries or images else []
    for material in materials:
        try:
            thumbnail = ensure_thumbnail(root, material['id'], Path(material['path']))
        except OSError:
            thumbnail = None
        material['has_thumbnail'] = thumbnail is not None
    by_id = {entry['id']: entry for entry in entries}
    featured = _pick_featured(entries)
    works: dict[str, dict[str, Any]] = {}
    for record in document_records:
        item = works.setdefault(record['work'], _work_record(record['work']))
        short = record['title'][len(record['work']):].strip(' -_·') if record['title'].startswith(record['work']) else record['title']
        item['documents'].append({'id': record['id'], 'short_title': short or record['title'], 'title': record['title'], 'sections': record['sections'], 'kinds': {}})
        item['sections'] += record['sections']
    for entry in entries:
        item = works.setdefault(entry['work'], _work_record(entry['work']))
        item['kinds'][entry['kind']] = item['kinds'].get(entry['kind'], 0) + 1
        document = next((doc for doc in item['documents'] if doc['id'] == entry['document_id']), None)
        if document is not None:
            document['kinds'][entry['kind']] = document['kinds'].get(entry['kind'], 0) + 1
    for material in materials:
        item = works.setdefault(material['work'], _work_record(material['work']))
        item['materials'] += 1
        if len(item['covers']) < 6:
            item['covers'].append(f'/api/reference-library/materials/{material["id"]}/thumb')
    for item in works.values():
        item['kinds'] = {kind: item['kinds'][kind] for kind in KIND_ORDER if item['kinds'].get(kind)}
        work_entries = [entry for entry in entries if entry['work'] == item['name']]
        picked = _pick_featured(work_entries)
        used = {entry['id'] for entry in picked}
        for document in item['documents']:
            first = next((entry for entry in work_entries if entry['document_id'] == document['id'] and entry['id'] not in used), None)
            if first is not None:
                picked.append(_summary(first))
                used.add(first['id'])
        for entry in work_entries:
            if len(picked) >= 6:
                break
            if entry['id'] not in used:
                picked.append(_summary(entry))
                used.add(entry['id'])
        item['featured'] = picked[:9]
    counts = {kind: sum(entry['kind'] == kind for entry in entries) for kind in KIND_ORDER}
    library = {
        'documents': document_records,
        'entries': entries,
        'by_id': by_id,
        'materials': materials,
        'works': list(works.values()),
        'featured': featured,
        'counts': {'documents': len(document_records), 'sections': len(entries), 'materials': len(materials)},
        'kinds': {kind: count for kind, count in counts.items() if count},
        'notes': notes,
        'config_error': config_error,
        'config_path': str(config_path(root)),
        'inbox_path': str(inbox_root(root)),
    }
    with _cache_lock:
        _cache[key] = {'signature': signature, 'library': library}
    return library


def overview(data_root: str | Path) -> dict[str, Any]:
    library = build_index(data_root)
    return {
        'documents': library['documents'],
        'works': library['works'],
        'featured': library['featured'],
        'materials': [public_material(item) for item in library['materials']],
        'counts': library['counts'],
        'kinds': library['kinds'],
        'notes': library['notes'],
        'config_error': library['config_error'],
        'config_path': library['config_path'],
        'inbox_path': library['inbox_path'],
    }


def public_material(material: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': material['id'],
        'title': material['title'],
        'filename': material['filename'],
        'category': material['category'],
        'work': material['work'],
        'source_title': material['source_title'],
        'description': material['description'],
        'related_entry_id': material['related_entry_id'],
        'tags': material['tags'],
        'thumb_url': f'/api/reference-library/materials/{material["id"]}/thumb',
        'file_url': f'/api/reference-library/materials/{material["id"]}/file',
    }


def find_material(data_root: str | Path, material_id: str) -> dict[str, Any] | None:
    return next((item for item in build_index(data_root)['materials'] if item['id'] == material_id), None)


def find_entry(data_root: str | Path, entry_id: str) -> dict[str, Any] | None:
    return build_index(data_root)['by_id'].get(entry_id)


def search(data_root: str | Path, query: str, limit: int = 12, kind: str | None = None, work: str | None = None, document: str | None = None) -> dict[str, Any]:
    library = build_index(data_root)
    candidates = [
        entry for entry in library['entries']
        if (not kind or entry['kind'] == kind)
        and (not work or entry['work'] == work)
        and (not document or entry['document_id'] == document)
    ]
    terms = _terms(query)
    if not terms:
        if kind:
            note = f'按类型「{kind}」列出原文' + (f'，共 {len(candidates)} 段，先列前 {limit} 段。' if len(candidates) > limit else '。')
            return {'query': query, 'items': [_summary(entry) for entry in candidates[:limit]], 'terms': [], 'note': note}
        if document:
            note = '按原文顺序列出这份资料里的段落' + (f'，共 {len(candidates)} 段，先列前 {limit} 段。' if len(candidates) > limit else '。')
            return {'query': query, 'items': [_summary(entry) for entry in candidates[:limit]], 'terms': [], 'note': note}
        scope = next((item for item in library['works'] if item['name'] == work), None) if work else None
        items = (scope['featured'] if scope else library['featured'])[:limit]
        return {'query': query, 'items': items, 'terms': [], 'note': f'显示{work}里的常用原文。' if work else '没有输入检索词，显示常用写法。'}
    scored: list[tuple[int, dict[str, Any], list[str]]] = []
    for entry in candidates:
        score, matched = _score(entry, terms)
        if score:
            scored.append((score, entry, matched))
    scored.sort(key=lambda item: (-item[0], item[1]['document_title'], item[1]['line']))
    items = []
    for score, entry, matched in scored[:limit]:
        item = _summary(entry, matched)
        item['excerpt'] = _excerpt(entry['plain'], matched)
        item['score'] = score
        item['matched_terms'] = matched
        items.append(item)
    return {'query': query, 'items': items, 'terms': terms, 'note': '按原文出现的字词排序，不是模型生成或推荐。'}


def _sentences(paragraph: str) -> list[str]:
    parts = re.split(r'(?<=[。！？；，])', paragraph)
    return [part.strip() for part in parts if part.strip()]


def anatomy(data_root: str | Path, text: str) -> dict[str, Any]:
    source = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    nodes: dict[int, list[str]] = {}
    unclassified: list[str] = []
    if not source:
        return {'nodes': [], 'missing': list(NODE_LABELS), 'unclassified': [], 'anchors': [], 'related': [], 'note': '请先粘贴一段提示词。'}
    indexed = [position for position, character in enumerate(source) if character in _CIRCLED]
    if len(indexed) >= 3:
        for position, start in enumerate(indexed):
            number = _CIRCLED.index(source[start]) + 1
            end = indexed[position + 1] if position + 1 < len(indexed) else len(source)
            value = source[start:end].strip()
            value = re.sub(r'^[①-⑬]\s*', '', value)
            value = re.sub(r'^【[^】]{1,30}】\s*', '', value)
            if value:
                nodes.setdefault(number, []).append(value)
    else:
        for paragraph in re.split(r'\n\s*\n', source):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            matched: dict[int, list[str]] = {}
            for sentence in _sentences(paragraph):
                hits = [index for index, pattern in _CUE_PATTERNS if pattern.search(sentence)]
                if hits:
                    matched.setdefault(hits[0] + 1, []).append(sentence)
                else:
                    unclassified.append(sentence)
            for index, sentences in matched.items():
                nodes.setdefault(index, []).extend(sentences)
    node_list = []
    for index in sorted(nodes):
        seen: set[str] = set()
        pieces = []
        for piece in nodes[index]:
            if piece not in seen:
                seen.add(piece)
                pieces.append(piece)
        label = NODE_LABELS[index - 1] if 1 <= index <= len(NODE_LABELS) else f'第 {index} 块'
        node_list.append({'id': index, 'label': label, 'text': '\n'.join(pieces)})
    present = {node['label'] for node in node_list}
    missing = [label for label in NODE_LABELS if label not in present]
    library = build_index(data_root)
    anchors = []
    seen_codes: set[str] = set()
    for code in _ANCHOR.findall(source):
        if code in seen_codes:
            continue
        seen_codes.add(code)
        related = next((entry for entry in library['entries'] if entry['title'].startswith(f'〔{code}〕')), None)
        anchors.append({'code': f'〔{code}〕', 'entry_id': related['id'] if related else None, 'title': related['title'] if related else None})
    related_results = search(data_root, source[:160], limit=4)
    note = '按你文档里的 13 块结构切分原文；只换行、不改写，也不生成新句子。缺少的块会单独列出。'
    return {'nodes': node_list, 'missing': missing, 'unclassified': unclassified, 'anchors': anchors, 'related': related_results['items'], 'note': note}


def resolve_material_file(data_root: str | Path, material_id: str) -> tuple[Path, dict[str, Any]]:
    material = find_material(data_root, material_id)
    if material is None:
        raise FileNotFoundError(material_id)
    source = Path(material['path'])
    if not source.is_file():
        raise FileNotFoundError(str(source))
    return source, material


def thumbnail_file(data_root: str | Path, material_id: str) -> tuple[Path, dict[str, Any]]:
    source, material = resolve_material_file(data_root, material_id)
    thumbnail = ensure_thumbnail(Path(data_root).resolve(), material_id, source)
    return (thumbnail or source), material


def stage_material(data_root: str | Path, material_id: str, workspace: Path) -> tuple[Path, dict[str, Any]]:
    source, material = resolve_material_file(data_root, material_id)
    suffix = source.suffix.lower() if source.suffix.lower() in IMAGE_SUFFIXES else '.png'
    destination = (workspace / 'assets' / 'reference-library' / f'{material_id}{suffix}').resolve()
    if workspace.resolve() not in destination.parents:
        raise ValueError('目标目录不在当前作品内')
    if not destination.is_file() or destination.stat().st_size != source.stat().st_size:
        copy_checked(source, destination)
    return destination, material
