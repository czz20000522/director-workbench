"""剧本库：草稿只在本机配置里，归档才写进素材库。"""
import pytest

from backend import app as backend
from backend import script_library


@pytest.fixture
def lib(preset_client_factory, tmp_path, monkeypatch):
    client, _ = preset_client_factory()
    assets = tmp_path / '素材库'
    assets.mkdir()
    monkeypatch.setattr(backend, 'ASSETS_ROOT', assets)
    return client, assets


def test_drafts_live_only_in_the_config_until_archived(lib):
    client, assets = lib
    created = client.post('/api/script-library/drafts', json={'title': '雨天独白', 'content': '第一场\n雨夜。'})
    assert created.status_code == 201, created.text
    draft = created.json()['draft']
    listing = client.get('/api/script-library').json()
    assert [item['title'] for item in listing['drafts']] == ['雨天独白']
    assert listing['archived'] == []
    assert not (assets / '剧本').exists()

    updated = client.put(f"/api/script-library/drafts/{draft['id']}", json={'title': '雨天独白 v2', 'content': '改过的正文'})
    assert updated.status_code == 200
    assert updated.json()['draft']['content'] == '改过的正文'

    archived = client.post(f"/api/script-library/drafts/{draft['id']}/archive")
    assert archived.status_code == 200, archived.text
    body = archived.json()
    assert body['path'].startswith('素材库/剧本/') and body['name'].endswith('雨天独白 v2.md')
    saved = assets / '剧本' / body['name']
    assert saved.read_text(encoding='utf-8') == '改过的正文'
    after = client.get('/api/script-library').json()
    assert after['drafts'] == []
    assert [item['name'] for item in after['archived']] == [body['name']]


def test_archive_never_silently_overwrites(lib, monkeypatch):
    client, assets = lib
    first = client.post('/api/script-library/drafts', json={'title': '同名', 'content': 'A'}).json()['draft']
    second = client.post('/api/script-library/drafts', json={'title': '同名', 'content': 'B'}).json()['draft']
    one = client.post(f"/api/script-library/drafts/{first['id']}/archive").json()
    two = client.post(f"/api/script-library/drafts/{second['id']}/archive").json()
    assert one['name'] != two['name']
    assert (assets / '剧本' / one['name']).read_text(encoding='utf-8') == 'A'
    assert (assets / '剧本' / two['name']).read_text(encoding='utf-8') == 'B'


def test_validation_and_missing_drafts(lib):
    client, _ = lib
    assert client.post('/api/script-library/drafts', json={'title': ' ', 'content': 'x'}).status_code == 422
    assert client.post('/api/script-library/drafts', json={'title': 'a' * 81, 'content': 'x'}).status_code == 422
    assert client.put('/api/script-library/drafts/missing', json={'title': 'x', 'content': 'y'}).status_code == 422
    assert client.delete('/api/script-library/drafts/missing').status_code == 404
    assert client.post('/api/script-library/drafts/missing/archive').status_code == 422


def test_import_is_just_a_new_draft(lib):
    client, assets = lib
    body = '第一场 日 内\n\n桌上的杯子还冒着热气。\n'
    created = client.post('/api/script-library/drafts', json={'title': '导入的剧本', 'content': body})
    assert created.status_code == 201
    assert created.json()['draft']['content'] == body
    assert not (assets / '剧本').exists()


def test_restore_archived_returns_to_drafts(lib, monkeypatch):
    """撤档：正文回到草稿，归档那份进回收站，不会两处各留一份。"""
    client, assets = lib
    from backend import trash

    def fake_recycle(paths, **_):
        removed = []
        for path in paths:
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        return removed, []

    monkeypatch.setattr(trash, 'recycle', fake_recycle)
    draft = client.post('/api/script-library/drafts', json={'title': '雨夜独白', 'content': '第一场 雨夜。'}).json()['draft']
    archived = client.post(f"/api/script-library/drafts/{draft['id']}/archive").json()

    restored = client.post('/api/script-library/archived/restore', json={'name': archived['name']})
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body['draft']['title'] == '雨夜独白'
    assert body['draft']['content'] == '第一场 雨夜。'
    listing = client.get('/api/script-library').json()
    assert [item['title'] for item in listing['drafts']] == ['雨夜独白']
    assert listing['archived'] == []
    assert not (assets / '剧本' / archived['name']).exists()


def test_restore_accepts_hand_dropped_file(lib, monkeypatch):
    """手动放进 素材库\\剧本 的稿子也能收回草稿，日期前缀不进标题。"""
    client, assets = lib
    from backend import trash
    monkeypatch.setattr(trash, 'recycle', lambda paths, **_: ([str(p) for p in paths], []))
    folder = assets / '剧本'
    folder.mkdir()
    dropped = folder / '2026-11-12_手写稿.md'
    dropped.write_text('手写的内容', encoding='utf-8')

    restored = client.post('/api/script-library/archived/restore', json={'name': dropped.name})
    assert restored.status_code == 200, restored.text
    assert restored.json()['draft']['title'] == '手写稿'
    listing = client.get('/api/script-library').json()
    assert listing['drafts'][0]['content'] == '手写的内容'


def test_restore_rejects_unknown_or_unsafe_names(lib):
    client, _ = lib
    assert client.post('/api/script-library/archived/restore', json={'name': 'nope.md'}).status_code == 409
    assert client.post('/api/script-library/archived/restore', json={'name': '../escape.md'}).status_code == 409


def test_module_rejects_bad_documents(tmp_path):
    broken = tmp_path / 'library.json'
    broken.write_text('{', encoding='utf-8')
    with pytest.raises(ValueError, match='无法读取'):
        script_library.load_drafts(broken)
    broken.write_text('{"drafts": "x"}', encoding='utf-8')
    with pytest.raises(ValueError, match='格式'):
        script_library.load_drafts(broken)


def test_breakdown_items_are_normalized_and_voice_is_derived():
    items = script_library.items_from_extraction({
        'characters': [
            {'name': '林夏', 'look': '28 岁女性，窄脸', 'voice': '女声偏冷，语速慢'},
            {'name': '林夏', 'look': '重复的角色'},
            {'name': '', 'look': '没有名字'},
        ],
        'scenes': [{'name': '便利店门口', 'look': '深夜冷光'}, {'name': '', 'look': '无效'}],
        'props': [{'name': '旧保温杯', 'look': '银色划痕'}],
        'music': [{'name': '雨夜', 'style': '低频脉冲，无人声', 'seconds': 30}],
    })
    assert [(item['type'], item['title']) for item in items] == [
        ('character', '林夏'), ('voice', '林夏 · 声线'),
        ('scene', '便利店门口'), ('prop', '旧保温杯'), ('music', '雨夜')]
    assert items[1]['prompt'] == '女声偏冷，语速慢'
    assert items[1]['note'].startswith('从角色「林夏」派生')
    assert items[4]['note'] == '建议 30 秒'
    assert all(item['enabled'] for item in items)


def test_breakdown_json_parser_survives_code_fences_and_garbage():
    parsed = script_library.parse_breakdown_json('```json\n{"scenes":[{"name":"街口","look":"夜"}]}\n```')
    assert parsed['scenes'][0]['name'] == '街口'
    with pytest.raises(ValueError):
        script_library.parse_breakdown_json('模型只回了一句话，没有 JSON')


def test_breakdown_api_extracts_saves_and_marks_stale(lib, monkeypatch):
    client, _ = lib
    draft = client.post('/api/script-library/drafts', json={
        'title': '雨夜', 'content': '第一场 雨夜，林夏站在便利店门口，雨水顺着伞骨往下滴。'}).json()['draft']

    def fake_breakdown(text, provider='auto'):
        return {'text': '{"characters":[{"name":"林夏","look":"28 岁女性，窄脸","voice":"女声偏冷"}],'
                        '"scenes":[{"name":"便利店门口","look":"深夜冷光"}],"props":[],"music":[]}',
                'provider': 'local', 'model': 'fake-model', 'elapsed_ms': 12,
                'source_chars': len(text), 'truncated': False}

    monkeypatch.setattr(backend.prompt_writer, 'breakdown', fake_breakdown)
    response = client.post(f'/api/script-library/drafts/{draft["id"]}/breakdown', json={'provider': 'auto'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['count'] == 3
    assert [item['title'] for item in body['draft']['breakdown']['items']] == ['林夏', '林夏 · 声线', '便利店门口']
    assert body['draft']['breakdown']['confirmed'] is False

    first = body['draft']['breakdown']['items'][0]
    saved = client.put(f'/api/script-library/drafts/{draft["id"]}/breakdown', json={
        'items': [{**first, 'prompt': '改成窄脸、齐肩短发'}], 'confirmed': True})
    assert saved.status_code == 200, saved.text
    assert saved.json()['draft']['breakdown']['confirmed'] is True
    assert saved.json()['draft']['breakdown']['items'][0]['prompt'] == '改成窄脸、齐肩短发'

    # 正文改过之后，清单要能标出「对不上」，否则用户会拿旧清单去生成。
    client.put(f'/api/script-library/drafts/{draft["id"]}', json={'title': '雨夜', 'content': '完全换了一场戏，角色也换了。'})
    detail = client.get(f'/api/script-library/drafts/{draft["id"]}').json()['draft']
    assert detail['stale'] is True
    listing = client.get('/api/script-library').json()
    assert listing['drafts'][0]['items'] == 1
    assert listing['drafts'][0]['confirmed'] is True


def test_breakdown_refuses_a_too_short_script(lib, monkeypatch):
    client, _ = lib
    draft = client.post('/api/script-library/drafts', json={'title': '空', 'content': '太短'}).json()['draft']
    response = client.post(f'/api/script-library/drafts/{draft["id"]}/breakdown', json={'provider': 'auto'})
    assert response.status_code == 422
