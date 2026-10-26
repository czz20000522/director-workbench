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


def test_module_rejects_bad_documents(tmp_path):
    broken = tmp_path / 'library.json'
    broken.write_text('{', encoding='utf-8')
    with pytest.raises(ValueError, match='无法读取'):
        script_library.load_drafts(broken)
    broken.write_text('{"drafts": "x"}', encoding='utf-8')
    with pytest.raises(ValueError, match='格式'):
        script_library.load_drafts(broken)
