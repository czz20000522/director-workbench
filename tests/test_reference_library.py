from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import app as backend
from backend import reference_library as library


PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / 'data-root'
    root.mkdir()
    document = root / 'docs' / '写法.md'
    document.parent.mkdir()
    document.write_text(
        '# 写法总纲\n\n'
        '## 三、提示词写法：拆成 13 个固定块\n\n'
        '第一条：雨夜，外景，中近景，固定机位。\n\n'
        '## 3.2 〔调-07〕记忆段\n\n'
        '整段贴〔调-07〕，不要自己另写质感词。\n\n'
        '## 5.2 视频镜用负面\n\n'
        '负面：现代物件、磨皮、卡通感、字幕、水印。\n\n'
        '### 〔默30〕陈默 · 三十岁\n\n'
        '三十岁中国男性，瘦高，短黑发，肤色偏白。\n',
        encoding='utf-8',
    )
    images = root / 'images'
    images.mkdir()
    (images / '01 角色_于乘风全身.png').write_bytes(PNG)
    (images / '02 场景_训练场.png').write_bytes(PNG)
    (root / 'config').mkdir()
    (root / 'config' / 'reference-library.json').write_text(json.dumps({
        'schema_version': 1,
        'documents': [{'title': '写法资料', 'work': '《测试剧》', 'path': str(document)}],
        'image_directories': [{'title': '参考图', 'work': '《测试剧》', 'path': str(images)}],
    }, ensure_ascii=False), encoding='utf-8')
    library.invalidate(root)
    return root


def test_sections_split_at_headings_and_search_returns_source_text(data_root: Path) -> None:
    result = library.search(data_root, '雨夜 固定机位')
    assert result['items']
    top = result['items'][0]
    assert top['title'] == '三、提示词写法：拆成 13 个固定块'
    assert '雨夜' in top['excerpt']
    entry = library.find_entry(data_root, top['id'])
    assert entry is not None
    assert entry['document_title'] == '写法资料'
    assert entry['line'] > 0
    assert '第一条：雨夜，外景，中近景，固定机位。' in entry['body']
    assert entry['kind'] == '写法'


def test_kind_filter_and_featured_cards_come_from_verbatim_sections(data_root: Path) -> None:
    result = library.search(data_root, '', kind='负面')
    assert [item['title'] for item in result['items']] == ['5.2 视频镜用负面']
    overview = library.overview(data_root)
    titles = [item['title'] for item in overview['featured']]
    assert '三、提示词写法：拆成 13 个固定块' in titles
    assert '3.2 〔调-07〕记忆段' in titles
    assert '〔默30〕陈默 · 三十岁' in titles


def test_works_group_documents_and_materials_then_drill_into_each_work(data_root: Path) -> None:
    overview = library.overview(data_root)
    assert [item['name'] for item in overview['works']] == ['《测试剧》']
    work = overview['works'][0]
    assert work['documents'][0]['short_title'] == '写法资料'
    assert work['sections'] == 4
    assert work['materials'] == 2
    assert work['kinds']['写法'] == 1
    assert len(work['covers']) == 2
    assert len(work['featured']) == 4
    assert [item['title'] for item in work['featured']][0] == '三、提示词写法：拆成 13 个固定块'

    scoped = library.search(data_root, '负面', work='《测试剧》')
    assert scoped['items'][0]['work'] == '《测试剧》'
    assert library.search(data_root, '负面', work='《别的剧》')['items'] == []

    document_id = work['documents'][0]['id']
    listing = library.search(data_root, '', work='《测试剧》', document=document_id)
    assert len(listing['items']) == 4
    assert all(item['document_id'] == document_id for item in listing['items'])


def test_anatomy_uses_the_documented_block_labels_and_never_fills_missing_blocks(data_root: Path) -> None:
    text = (
        '①《我回来过一次》第 24 场\n'
        '②二〇〇七年九月，中国南方，外景，黄昏\n'
        '④中近景，双人侧面，固定机位\n'
        '⑤画面左侧是〔默30〕，三十岁男性\n'
        '⑬负面：现代物件、字幕、水印\n'
    )
    result = library.anatomy(data_root, text)
    assert [node['id'] for node in result['nodes']] == [1, 2, 4, 5, 13]
    assert result['nodes'][0]['label'] == '片名·场次·段落'
    assert result['nodes'][-1]['label'] == '声音·旁白·负面'
    assert '服装·状态' in result['missing']
    assert result['nodes'][-1]['text'].startswith('《我回来过一次》') is False
    assert result['anchors'][0]['code'] == '〔默30〕'
    assert result['anchors'][0]['entry_id']


def test_prose_prompt_is_split_at_cues_without_rewriting(data_root: Path) -> None:
    text = (
        '《我回来过一次》第 24 场。二〇〇七年九月，中国南方，外景，黄昏。'
        '中近景，双人侧面，固定机位。画面左侧是〔默30〕。'
        '光线：S3 级。负面：现代物件、磨皮、字幕。'
    )
    result = library.anatomy(data_root, text)
    labels = [node['label'] for node in result['nodes']]
    assert labels[0] == '片名·场次·段落'
    assert '地点' in labels
    assert '景别·机位·构图' in labels
    assert '光线' in labels
    assert '声音·旁白·负面' in labels
    joined = '\n'.join(node['text'] for node in result['nodes'])
    for sentence in ('二〇〇七年九月', '中国南方', '固定机位', '光线：S3 级'):
        assert sentence in joined


def test_materials_are_indexed_thumbnailed_and_copied_only_on_request(data_root: Path, tmp_path: Path) -> None:
    overview = library.overview(data_root)
    assert overview['counts']['materials'] == 2
    material = next(item for item in overview['materials'] if item['title'] == '于乘风全身')
    assert material['category'] == '角色'
    assert material['work'] == '《测试剧》'
    assert material['thumb_url'].endswith('/thumb')

    thumbnail, _ = library.thumbnail_file(data_root, material['id'])
    assert thumbnail.is_file() and thumbnail.stat().st_size > 0
    assert thumbnail.suffix.lower() == '.jpg'

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    staged, _ = library.stage_material(data_root, material['id'], workspace)
    assert staged.is_file()
    assert staged.read_bytes() == PNG
    assert workspace in staged.parents
    assert (workspace / 'assets' / 'reference-library').is_dir()


def test_api_reads_index_and_rejects_unknown_material(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend, 'ROOT', data_root)
    client = TestClient(backend.app)
    overview = client.get('/api/reference-library')
    assert overview.status_code == 200, overview.text
    assert overview.json()['counts']['documents'] == 1
    assert overview.json()['counts']['materials'] == 2
    assert overview.json()['works'][0]['name'] == '《测试剧》'
    material_id = overview.json()['materials'][0]['id']
    assert client.get(f'/api/reference-library/materials/{material_id}/thumb').status_code == 200
    assert client.get('/api/reference-library/materials/mat-does-not-exist/file').status_code == 404
    anatomy = client.post('/api/reference-library/anatomy', json={'text': '④中近景，固定机位。'})
    assert anatomy.status_code == 200
    assert anatomy.json()['nodes'][0]['label'] == '景别·机位·构图'
    search = client.get('/api/reference-library/search', params={'q': '负面', 'kind': '负面'})
    assert search.status_code == 200
    assert search.json()['items'][0]['title'] == '5.2 视频镜用负面'
