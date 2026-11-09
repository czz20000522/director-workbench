import json

import pytest
from fastapi import HTTPException

from backend import prompt_library, prompt_writer
from backend.routers import prompt_writing


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def test_create_writes_file_and_titles_from_first_sentence(tmp_path):
    target = tmp_path / 'prompt-library.json'
    result = prompt_library.create('她靠在窗边点烟，睡裙滑到肩头。晨光从左侧切进来。', path=target)
    assert result['created'] is True
    assert result['item']['title'] == '她靠在窗边点烟，睡裙滑到肩头'
    stored = json.loads(target.read_text(encoding='utf-8'))
    assert stored['items'][0]['text'].startswith('她靠在窗边')
    assert stored['items'][0]['uses'] == 0


def test_same_text_merges_links_instead_of_duplicating(tmp_path):
    target = tmp_path / 'lib.json'
    prompt_library.create('雨夜街角，霓虹在水面上碎开。', tags=['夜景'], path=target,
                          links={'project_id': 'p1', 'aspect': 'landscape',
                                 'materials': [{'role': '服装', 'label': '黑色皮夹克',
                                                'material_id': 'm1'}]})
    again = prompt_library.create('雨夜街角，霓虹在水面上碎开。', tags=['雨天'], path=target,
                                  links={'seed': 42, 'result': '/out/a.png'})
    assert again['created'] is False
    assert len(prompt_library.load(target)['items']) == 1
    item = again['item']
    assert item['tags'] == ['夜景', '雨天']
    assert item['links']['project_id'] == 'p1'
    assert item['links']['aspect'] == 'landscape'
    assert item['links']['seed'] == 42
    assert item['links']['result'] == '/out/a.png'
    assert item['links']['materials'][0]['label'] == '黑色皮夹克'


def test_search_matches_text_tag_material_and_project(tmp_path):
    target = tmp_path / 'lib.json'
    prompt_library.create('雨夜街角，霓虹。', tags=['夜景'], path=target, links={'project_id': 'p1'})
    prompt_library.create('清晨厨房，煎蛋。', tags=['日景'], path=target,
                          links={'project_id': 'p2',
                                 'materials': [{'role': '脸型', 'label': '于乘风半身'}]})
    assert prompt_library.search(query='霓虹', path=target)['matched'] == 1
    assert prompt_library.search(query='于乘风', path=target)['matched'] == 1
    assert prompt_library.search(tag='#日景', path=target)['matched'] == 1
    assert prompt_library.search(project_id='p1', path=target)['matched'] == 1
    summary = prompt_library.search(path=target)
    assert summary['total'] == 2
    assert {entry['tag'] for entry in summary['tags']} == {'夜景', '日景'}


def test_update_and_remove(tmp_path):
    target = tmp_path / 'lib.json'
    item = prompt_library.create('雨夜街角。', path=target)['item']
    updated = prompt_library.update(item['id'], title='雨夜', tags=['霓虹'], note='试过一次', path=target)
    assert updated['title'] == '雨夜'
    assert updated['tags'] == ['霓虹']
    assert updated['note'] == '试过一次'
    assert prompt_library.remove(item['id'], target) is True
    assert prompt_library.remove(item['id'], target) is False
    with pytest.raises(KeyError):
        prompt_library.update(item['id'], title='x', path=target)


def test_mark_used_counts_and_keeps_last_result(tmp_path):
    target = tmp_path / 'lib.json'
    item = prompt_library.create('雨夜街角。', path=target)['item']
    used = prompt_library.mark_used(item['id'], result='/out/b.png', task_id='t-1', path=target)
    assert used['uses'] == 1
    assert used['links']['result'] == '/out/b.png'
    assert used['links']['task_id'] == 't-1'
    again = prompt_library.mark_used(item['id'], path=target)
    assert again['uses'] == 2
    assert again['links']['result'] == '/out/b.png'


def test_rejects_unknown_link_field(tmp_path):
    with pytest.raises(ValueError, match='未知字段'):
        prompt_library.create('雨夜街角。', path=tmp_path / 'x.json',
                              links={'command': 'rm -rf /'})
    with pytest.raises(ValueError, match='种子'):
        prompt_library.create('雨夜街角。', path=tmp_path / 'x.json', links={'seed': -1})
    with pytest.raises(ValueError, match='为空'):
        prompt_library.create('   ', path=tmp_path / 'x.json')


def test_library_cap_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_library, 'MAX_ITEMS', 2)
    target = tmp_path / 'lib.json'
    prompt_library.create('第一段。', path=target)
    prompt_library.create('第二段。', path=target)
    with pytest.raises(ValueError, match='已满'):
        prompt_library.create('第三段。', path=target)


def test_context_lines_describe_materials_and_aspect():
    lines = prompt_writer.context_lines({
        'materials': [{'role': '服装', 'label': '黑色皮夹克'}],
        'aspect': '竖屏', 'profile': '写实·成人向'})
    assert '黑色皮夹克' in lines[0]
    assert any('竖屏' in line for line in lines)
    assert prompt_writer.context_lines(None) == []
    assert prompt_writer.context_lines({'materials': [{'label': '   '}]}) == []


def test_draft_passes_material_context_to_model(monkeypatch):
    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured.update(json or {})
        return FakeResponse(payload={'message': {'content': '她靠在窗边，皮夹克领口立起。'}})

    monkeypatch.setattr(prompt_writer.httpx, 'post', fake_post)
    result = prompt_writer.draft('她在窗边', context={'materials': [{'role': '服装', 'label': '黑色皮夹克'}]})
    content = captured['messages'][1]['content']
    assert '黑色皮夹克' in content
    assert '她在窗边' in content
    assert result['prompt'] == '她靠在窗边，皮夹克领口立起。'


def test_router_rejects_extra_link_field(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_library, 'document_path', lambda: tmp_path / 'lib.json')
    request = prompt_writing.PromptSaveRequest(text='雨夜街角。', links={'evil': 1})
    with pytest.raises(HTTPException) as error:
        prompt_writing.prompt_library_create(request)
    assert error.value.status_code == 422


def test_router_delete_missing_returns_404(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_library, 'document_path', lambda: tmp_path / 'lib.json')
    with pytest.raises(HTTPException) as error:
        prompt_writing.prompt_library_delete('pl_missing')
    assert error.value.status_code == 404


def test_router_save_then_list(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_library, 'document_path', lambda: tmp_path / 'lib.json')
    saved = prompt_writing.prompt_library_create(prompt_writing.PromptSaveRequest(
        text='雨夜街角，霓虹。', source='draft', idea='雨夜',
        links={'project_id': 'p1', 'profile': 'general'}))
    assert saved['created'] is True
    listing = prompt_writing.prompt_library_list(query='霓虹', project_id='p1')
    assert listing['matched'] == 1
    assert listing['items'][0]['links']['profile'] == 'general'
