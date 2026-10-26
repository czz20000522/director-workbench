"""Prompt-stated quality is authoritative over the picker for image and video tools."""
from backend import app as backend, production_presets, quality_intent
from support import add_shot, reference_image


def test_detects_explicit_tiers_and_synonyms():
    assert quality_intent.detect('雨夜街头，4K 成片')['resolution'] == '4k'
    assert quality_intent.detect('输出 2160p')['resolution'] == '4k'
    assert quality_intent.detect('要超高清的雪山')['resolution'] == '4k'
    assert quality_intent.detect('要 2K 分辨率')['resolution'] == '2k'
    assert quality_intent.detect('只要 1K 预览')['resolution'] == 'native'
    assert quality_intent.detect('做成 8K 海报')['resolution'] == '8k'


def test_prefers_the_highest_tier_and_ignores_negation_and_false_positives():
    assert quality_intent.detect('720p 预览，4K 成片')['resolution'] == '4k'
    assert quality_intent.detect('超高清，别降成高清')['resolution'] == '4k'
    assert quality_intent.detect('不要 4K，要高清就行')['resolution'] == '2k'
    assert quality_intent.detect('不要 4K') is None
    assert quality_intent.detect('非常高清')['resolution'] == '2k'
    assert quality_intent.detect('不错，高清画质')['resolution'] == '2k'
    assert quality_intent.detect('18K 金的手表') is None
    assert quality_intent.detect('HDR 调色') is None


def test_resolution_record_explains_override_and_video_cap():
    resolution, record = quality_intent.resolve('4K 成片', 'native')
    assert resolution == '4k' and record['source'] == 'prompt' and not record['capped']
    assert quality_intent.resolve('普通画面', 'native') == ('native', None)
    assert quality_intent.resolve('8K 海报', '4k', video=True)[0] == '4k'
    assert quality_intent.resolve('8K 海报', '4k', video=True)[1]['capped'] is True
    assert quality_intent.resolve('8K 海报', 'native', video=False)[0] == '8k'
    assert quality_intent.resolve('普通画面', '8k', video=True)[1]['source'] == 'limit'


def test_image_prompt_quality_overrides_the_selection(preset_client, monkeypatch):
    client, _ = preset_client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    endpoint = '/api/projects/preset-test/keyframe-tasks'
    args = {'mode': 'text', 'prompt': '清晨的小镇，4K 超清', 'idempotency_key': 'prompt-quality'}
    task = client.post(endpoint, json={**args, 'output_resolution': 'native'}).json()
    assert task['payload']['output_size'] == {'resolution': '4k', 'aspect_ratio': '9:16'}
    assert task['payload']['request']['output_resolution'] == '4k'
    assert task['payload']['quality_intent']['source'] == 'prompt'
    # The picker no longer matters: both submissions resolve to the same 4K request.
    assert client.post(endpoint, json={**args, 'output_resolution': '8k'}).json()['id'] == task['id']
    assert client.post(endpoint, json={**args, 'prompt': '清晨的小镇，8K 超清'}).status_code == 409


def test_reference_mode_records_that_prompt_quality_cannot_change_size(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    task = client.post('/api/projects/preset-test/keyframe-tasks', json={
        'prompt': '雨夜挥手，4K', 'reference_image': str(reference_image(project)), 'idempotency_key': 'reference-quality'}).json()
    assert 'output_size' not in task['payload']
    assert task['payload']['quality_intent']['applied'] == 'native'
    assert task['payload']['quality_intent']['capped'] is True


def test_video_prompt_quality_overrides_the_saved_generation_setting(preset_client):
    client, project = preset_client
    installed = client.post('/api/projects/preset-test/production-presets/h3-text-native-landscape')
    assert installed.status_code == 200, installed.text
    add_shot(client, project, 'S01', with_audio=False)
    saved = client.patch('/api/projects/preset-test/plan/segments/S01', json={'prompt': '雨夜街头，4K 成片'})
    assert saved.status_code == 200, saved.text
    stage = production_presets.stage_id('h3-text-native-landscape')
    result = client.post('/api/projects/preset-test/tasks', json={
        'asset_id': 'S01', 'pipeline_stage_id': stage, 'pipeline_values': {}, 'idempotency_key': 'video-quality-rule'})
    assert result.status_code == 200, result.text
    task = result.json()
    assert task['payload']['output_size'] == {'resolution': '4k', 'aspect_ratio': '16:9'}
    assert task['payload']['quality_intent']['source'] == 'prompt'
