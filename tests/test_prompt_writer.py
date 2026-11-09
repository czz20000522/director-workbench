import httpx
import pytest
from fastapi import HTTPException

from backend import prompt_writer
from backend.routers import prompt_writing


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def offline_ollama(monkeypatch, tmp_path):
    """单元测试不碰本机 Ollama：服务探测与拉起一律打桩。

    没有这道桩，测试结果会随「本机装没装、开没开」变化——开发机上通过、
    干净机器上失败，而且 `ensure_service` 真会去拉一个进程起来。
    配置也指到临时目录，免得测试改到用户真正的 prompt-writer.json。
    """
    monkeypatch.setattr(prompt_writer, 'config_path', lambda: tmp_path / 'prompt-writer.json')
    monkeypatch.setattr(prompt_writer, '_LAST_RELEASE', 0.0)
    monkeypatch.setattr(prompt_writer, '_service_models', lambda **kwargs: [])
    # 默认当作「服务已在跑」：draft 里的真 ensure_service 会立刻返回，不会拉进程。
    monkeypatch.setattr(prompt_writer, 'service_alive', lambda **kwargs: True)


def test_clean_strips_thinking_block():
    raw = "<think>先想一下镜头。</think>她靠在窗边，晨光从侧面切进来。"
    assert prompt_writer._clean(raw) == '她靠在窗边，晨光从侧面切进来。'


def test_clean_strips_thinking_block_with_end_token():
    raw = "<think>想镜头。<｜end▁of▁thinking｜>提示词：「她靠在窗边。」"
    assert prompt_writer._clean(raw) == '她靠在窗边。'


def test_clean_collapses_paragraphs():
    assert prompt_writer._clean('第一句。\n\n第二句。') == '第一句。第二句。'


def test_draft_rejects_blank_idea():
    with pytest.raises(ValueError, match='想法'):
        prompt_writer.draft('   ')


def test_draft_returns_cleaned_prompt(monkeypatch):
    monkeypatch.setattr(prompt_writer.httpx, 'post',
                        lambda *a, **k: FakeResponse(payload={'message': {'content': '她坐在窗台，睡裙滑到肩头。'}}))
    result = prompt_writer.draft('她在窗边')
    assert result['prompt'] == '她坐在窗台，睡裙滑到肩头。'
    assert result['model'] == prompt_writer.MODEL_ID
    assert result['elapsed_ms'] >= 0


def test_draft_maps_missing_model_to_unavailable(monkeypatch):
    monkeypatch.setattr(prompt_writer.httpx, 'post', lambda *a, **k: FakeResponse(status_code=404))
    with pytest.raises(prompt_writer.WriterUnavailable, match='ollama pull'):
        prompt_writer.draft('她在窗边')


def test_draft_maps_dead_service_to_unavailable(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError('refused')

    monkeypatch.setattr(prompt_writer.httpx, 'post', boom)
    with pytest.raises(prompt_writer.WriterUnavailable, match='Ollama'):
        prompt_writer.draft('她在窗边')


def test_draft_rejects_empty_model_answer(monkeypatch):
    monkeypatch.setattr(prompt_writer.httpx, 'post', lambda *a, **k: FakeResponse(payload={'message': {'content': '  '}}))
    with pytest.raises(prompt_writer.WriterFailed, match='空内容'):
        prompt_writer.draft('她在窗边')


def test_capability_explains_missing_model(monkeypatch):
    monkeypatch.setattr(prompt_writer, '_service_models', lambda **k: ['qwen2.5vl:7b'])
    monkeypatch.setattr(prompt_writer, '_model_manifest', lambda model: None)
    state = prompt_writer.capability()
    assert state['available'] is False
    assert state['running'] is True
    assert 'ollama pull' in state['reason']


def test_capability_explains_dead_service(monkeypatch):
    def boom(**kwargs):
        raise httpx.ConnectError('refused')

    monkeypatch.setattr(prompt_writer, '_service_models', boom)
    monkeypatch.setattr(prompt_writer, '_model_manifest', lambda model: None)
    # 连 exe 都没有，才是真的没救；有 exe 时会提示「点生成自动拉起」。
    monkeypatch.setattr(prompt_writer, 'find_executable', lambda: None)
    state = prompt_writer.capability()
    assert state['available'] is False
    assert 'ollama.exe' in state['reason']


def test_model_manifest_finds_namespaced_model(tmp_path, monkeypatch):
    manifest = tmp_path / 'manifests' / 'registry.ollama.ai' / 'huihui_ai' / 'qwen3-abliterated' / '8b'
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('OLLAMA_MODELS', str(tmp_path))
    assert prompt_writer._model_manifest('huihui_ai/qwen3-abliterated:8b') == manifest


def test_model_manifest_finds_official_model(tmp_path, monkeypatch):
    manifest = tmp_path / 'manifests' / 'registry.ollama.ai' / 'library' / 'qwen2.5vl' / '7b'
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{}', encoding='utf-8')
    monkeypatch.setenv('OLLAMA_MODELS', str(tmp_path))
    assert prompt_writer._model_manifest('qwen2.5vl:7b') == manifest


def test_capability_accepts_listed_model(monkeypatch):
    monkeypatch.setattr(prompt_writer, '_service_models', lambda **k: [prompt_writer.MODEL_ID])
    state = prompt_writer.capability()
    assert state['available'] is True


def test_capability_falls_back_to_another_tag_of_same_family(monkeypatch):
    # 用户在别处拉的可能是 14b 或 4b：同底座就该能用，不能因为标签不同判成没装。
    monkeypatch.setattr(prompt_writer, '_service_models',
                        lambda **k: ['huihui_ai/qwen3-abliterated:14b'])
    state = prompt_writer.capability()
    assert state['available'] is True
    assert state['model'] == 'huihui_ai/qwen3-abliterated:14b'
    assert state['default_model'] == prompt_writer.MODEL_ID


def test_active_model_ignores_other_families(monkeypatch):
    monkeypatch.setattr(prompt_writer, '_service_models',
                        lambda **k: ['qwen2.5vl:7b', 'dolphin3:8b'])
    assert prompt_writer.active_model() == prompt_writer.MODEL_ID


def test_router_maps_unavailable_to_503(monkeypatch):
    def raise_unavailable(*args, **kwargs):
        raise prompt_writer.WriterUnavailable('Ollama 没在运行')

    monkeypatch.setattr(prompt_writer, 'draft', raise_unavailable)
    request = prompt_writing.PromptDraftRequest(idea='她在窗边')
    with pytest.raises(HTTPException) as error:
        prompt_writing.prompt_draft(request)
    assert error.value.status_code == 503


def test_router_rejects_blank_idea():
    with pytest.raises(ValueError):
        prompt_writing.PromptDraftRequest(idea='   ')


def test_active_model_crosses_minor_version(monkeypatch):
    # 本机装的是 qwen3.5-abliterated:9b，默认档写的 qwen3-abliterated:8b：
    # 小版本号不同也算同一条产品线，不能因此判成「没装」。
    monkeypatch.setattr(prompt_writer, '_service_models',
                        lambda **k: ['huihui_ai/qwen3.5-abliterated:9b'])
    assert prompt_writer.active_model() == 'huihui_ai/qwen3.5-abliterated:9b'
    assert prompt_writer.capability()['available'] is True


def test_active_model_skips_embedding_models(monkeypatch):
    # 只有 embedding 模型时宁可报「没装」，也不能拿它写词。
    monkeypatch.setattr(prompt_writer, '_service_models',
                        lambda **k: ['nomic-embed-text:latest'])
    assert prompt_writer.active_model() == prompt_writer.MODEL_ID


def test_ensure_service_reuses_running_instance(monkeypatch):
    monkeypatch.setattr(prompt_writer, 'service_alive', lambda **k: True)

    def must_not_launch(*args, **kwargs):
        raise AssertionError('服务已在跑，不该再拉一个进程')

    monkeypatch.setattr(prompt_writer.subprocess, 'Popen', must_not_launch)
    assert prompt_writer.ensure_service() == {'started': False, 'wait_ms': 0, 'pid': None}


def test_ensure_service_reports_missing_executable(monkeypatch):
    monkeypatch.setattr(prompt_writer, 'service_alive', lambda **k: False)
    monkeypatch.setattr(prompt_writer, 'find_executable', lambda: None)
    with pytest.raises(prompt_writer.WriterUnavailable, match='ollama.exe'):
        prompt_writer.ensure_service()


def test_draft_reports_service_startup(monkeypatch):
    monkeypatch.setattr(prompt_writer, '_service_models',
                        lambda **k: ['huihui_ai/qwen3.5-abliterated:9b'])
    monkeypatch.setattr(prompt_writer, 'ensure_service',
                        lambda **k: {'started': True, 'wait_ms': 1250, 'pid': 4321})
    monkeypatch.setattr(prompt_writer.httpx, 'post',
                        lambda *a, **k: FakeResponse(payload={'message': {'content': '雨夜街角。'}}))
    result = prompt_writer.draft('雨夜街角')
    assert result['started_service'] is True
    assert result['startup_ms'] == 1250
    assert result['model'] == 'huihui_ai/qwen3.5-abliterated:9b'


def test_capability_marks_service_startable(monkeypatch):
    # 服务没跑、但 exe 和模型都在：界面要提示「点生成会自动拉起」，而不是报错。
    monkeypatch.setattr(prompt_writer, '_service_models',
                        lambda **k: (_ for _ in ()).throw(httpx.ConnectError('refused')))
    monkeypatch.setattr(prompt_writer, 'find_executable', lambda: r'D:\AI\Ollama\ollama.exe')
    monkeypatch.setattr(prompt_writer, '_model_manifest', lambda model: object())
    state = prompt_writer.capability()
    assert state['available'] is True
    assert state['running'] is False
    assert state['can_start'] is True
    assert '自动拉起' in state['reason']


def test_polish_uses_its_own_system_prompt(monkeypatch):
    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured['payload'] = json
        return FakeResponse(payload={'message': {'content': '润色后的整段描述。'}})

    monkeypatch.setattr(prompt_writer.httpx, 'post', fake_post)
    result = prompt_writer.draft('她站在窗边，睡裙滑落', mode='polish')
    assert result['mode'] == 'polish'
    system = captured['payload']['messages'][0]['content']
    assert '润色' in system
    assert '一个都不能丢' in system
    # 润色时原文原样送过去，不能加「想法：」把它变成另一回事。
    assert captured['payload']['messages'][1]['content'] == '她站在窗边，睡裙滑落'


def test_compose_uses_writer_prompt(monkeypatch):
    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured['payload'] = json
        return FakeResponse(payload={'message': {'content': '完整描述。'}})

    monkeypatch.setattr(prompt_writer.httpx, 'post', fake_post)
    prompt_writer.draft('雨夜街角', mode='compose')
    assert '提示词写手' in captured['payload']['messages'][0]['content']


def test_polish_rejects_blank_text():
    with pytest.raises(ValueError, match='润色'):
        prompt_writer.draft('   ', mode='polish')


def test_online_draft_calls_openai_compatible_endpoint(monkeypatch, tmp_path):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    prompt_writer.save_config({'online': {'base_url': 'https://api.example.com/v1',
                                          'model': 'deepseek-chat',
                                          'api_key': 'sk-test-1234567890'}})
    captured = {}

    def fake_post(url, json=None, headers=None, **kwargs):
        captured.update(url=url, payload=json, headers=headers)
        return FakeResponse(payload={'choices': [{'message': {'content': '雨夜街头，霓虹碎在水洼里。'}}]})

    monkeypatch.setattr(prompt_writer.httpx, 'post', fake_post)
    result = prompt_writer.draft('雨夜街角', provider='online')
    assert result['prompt'] == '雨夜街头，霓虹碎在水洼里。'
    assert result['provider'] == 'online'
    assert captured['url'] == 'https://api.example.com/v1/chat/completions'
    assert captured['headers']['Authorization'] == 'Bearer sk-test-1234567890'
    assert captured['payload']['model'] == 'deepseek-chat'


def test_online_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(prompt_writer.WriterUnavailable, match='API Key'):
        prompt_writer.draft('雨夜街角', provider='online')


def test_online_maps_401_to_unavailable(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'sk-bad-key-000000')
    monkeypatch.setattr(prompt_writer.httpx, 'post', lambda *a, **k: FakeResponse(status_code=401))
    with pytest.raises(prompt_writer.WriterUnavailable, match='401'):
        prompt_writer.draft('雨夜街角', provider='online')


def test_config_keeps_key_when_patch_omits_it(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    prompt_writer.save_config({'online': {'api_key': 'sk-abcdefghijklmn'}})
    prompt_writer.save_config({'online': {'model': 'deepseek-reasoner'}})
    assert prompt_writer.online_key() == 'sk-abcdefghijklmn'
    state = prompt_writer.config_view()
    assert state['online']['has_key'] is True
    assert state['online']['key_hint'].endswith('klmn')
    # 完整 Key 永远不出现在给界面的视图里。
    assert state['online']['api_key'] == ''
    assert 'abcdefghijklmn' not in str(state)


def test_config_clears_key_with_empty_string(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    prompt_writer.save_config({'online': {'api_key': 'sk-abcdefghijklmn'}})
    prompt_writer.save_config({'online': {'api_key': ''}})
    assert prompt_writer.online_key() == ''
    assert prompt_writer.config_view()['online']['has_key'] is False


def test_release_skips_model_that_is_not_loaded(monkeypatch):
    monkeypatch.setattr(prompt_writer, 'active_model', lambda *a, **k: 'huihui_ai/qwen3.5-abliterated:9b')
    monkeypatch.setattr(prompt_writer, '_loaded_models', lambda **k: [])

    def must_not_post(*args, **kwargs):
        raise AssertionError('没加载就不该发卸载请求，否则会先读 6 GB 权重再卸')

    monkeypatch.setattr(prompt_writer.httpx, 'post', must_not_post)
    result = prompt_writer.release()
    assert result['released'] is True
    assert '没加载' in result['reason']


def test_release_unloads_loaded_model(monkeypatch):
    monkeypatch.setattr(prompt_writer, 'active_model', lambda *a, **k: 'huihui_ai/qwen3.5-abliterated:9b')
    monkeypatch.setattr(prompt_writer, '_loaded_models',
                        lambda **k: ['huihui_ai/qwen3.5-abliterated:9b'])
    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured.update(url=url, payload=json)
        return FakeResponse(payload={'done': True})

    monkeypatch.setattr(prompt_writer.httpx, 'post', fake_post)
    result = prompt_writer.release()
    assert result['released'] is True
    assert captured['url'].endswith('/api/generate')
    assert captured['payload']['keep_alive'] == 0


def test_keep_alive_defaults_to_30s(monkeypatch):
    monkeypatch.setattr(prompt_writer, 'service_alive', lambda **k: True)
    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured['payload'] = json
        return FakeResponse(payload={'message': {'content': '描述。'}})

    monkeypatch.setattr(prompt_writer.httpx, 'post', fake_post)
    prompt_writer.draft('雨夜')
    assert captured['payload']['keep_alive'] == '30s'


def _sample_image(tmp_path, monkeypatch, name='ref.png'):
    """造一张图并把读取白名单收窄到临时目录（不然会被目录校验拦掉）。"""
    target = tmp_path / 'assets' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
    monkeypatch.setattr(prompt_writer, '_image_roots', lambda: [tmp_path])
    return target


def test_describe_image_sends_local_vision_payload(tmp_path, monkeypatch):
    target = _sample_image(tmp_path, monkeypatch)
    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured.update(url=url, payload=json)
        return FakeResponse(payload={'message': {'content': '一位女性站在浅灰墙前，中景，柔和侧光。'}})

    monkeypatch.setattr(prompt_writer.httpx, 'post', fake_post)
    result = prompt_writer.describe_image(str(target))
    assert result['provider'] == 'local'
    assert result['prompt'] == '一位女性站在浅灰墙前，中景，柔和侧光。'
    # 本地走 /api/chat，图片放在 messages[0].images（base64，不带 data: 前缀）。
    assert captured['url'].endswith('/api/chat')
    message = captured['payload']['messages'][0]
    assert message['images'] and not message['images'][0].startswith('data:')
    assert captured['payload']['think'] is False
    assert captured['payload']['keep_alive'] == '30s'


def test_describe_image_rejects_relative_path(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_writer, '_image_roots', lambda: [tmp_path])
    with pytest.raises(ValueError, match='绝对路径'):
        prompt_writer.describe_image('assets/ref.png')


def test_describe_image_rejects_non_image_file(tmp_path, monkeypatch):
    target = _sample_image(tmp_path, monkeypatch, name='notes.txt')
    with pytest.raises(ValueError, match='PNG'):
        prompt_writer.describe_image(str(target))


def test_describe_image_rejects_path_outside_roots(tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'ref.png'
    target.write_bytes(b'\x89PNG\r\n\x1a\n')
    allowed = tmp_path / 'allowed'
    allowed.mkdir()
    monkeypatch.setattr(prompt_writer, '_image_roots', lambda: [allowed])
    with pytest.raises(ValueError, match='作品目录'):
        prompt_writer.describe_image(str(target))


def test_describe_image_online_uses_openai_vision_format(tmp_path, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    prompt_writer.save_config({'online': {'base_url': 'https://api.example.com/v1',
                                          'model': 'deepseek-chat',
                                          'api_key': 'sk-test-1234567890'}})
    target = _sample_image(tmp_path, monkeypatch)
    captured = {}

    def fake_post(url, json=None, headers=None, **kwargs):
        captured.update(url=url, payload=json, headers=headers)
        return FakeResponse(payload={'choices': [{'message': {'content': '图中一位女性，中景。'}}]})

    monkeypatch.setattr(prompt_writer.httpx, 'post', fake_post)
    result = prompt_writer.describe_image(str(target), provider='online')
    assert result['provider'] == 'online'
    assert result['prompt'] == '图中一位女性，中景。'
    assert captured['url'] == 'https://api.example.com/v1/chat/completions'
    assert captured['headers']['Authorization'] == 'Bearer sk-test-1234567890'
    content = captured['payload']['messages'][0]['content']
    image_part = next(part for part in content if part['type'] == 'image_url')
    assert image_part['image_url']['url'].startswith('data:image/png;base64,')


def test_describe_image_maps_400_to_vision_unsupported(monkeypatch, tmp_path):
    target = _sample_image(tmp_path, monkeypatch)
    monkeypatch.setattr(prompt_writer.httpx, 'post',
                        lambda *a, **k: FakeResponse(status_code=400, text='does not support images'))
    with pytest.raises(prompt_writer.WriterFailed, match='不接受图片'):
        prompt_writer.describe_image(str(target))
