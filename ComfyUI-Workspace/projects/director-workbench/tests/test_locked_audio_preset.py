import json
from pathlib import Path

from backend import production_presets as presets
from tests.test_production_presets import preset_client, install, add_shot


def test_locked_audio_uses_masked_source_latent_and_original_delivery_waveform():
    graph = presets.load_recipe(presets.LOCKED_PRESET_ID)
    condition = graph['105:104']
    assert condition['class_type'] == 'MiniMaxH3AudioConditioningT8'
    assert condition['inputs']['audio_mode'] == 'lock_source'
    assert condition['inputs']['audio_denoise_strength'] == 0
    assert condition['inputs']['drive_audio'] == ['aigc:load-audio-guide', 0]
    assert condition['inputs']['task_type'] == 'I2VA'
    assert condition['inputs']['add_source_as_reference'] is False
    assert 'last_frame' not in condition['inputs']
    assert graph['105:14']['inputs']['latent_image'] == ['105:104', 1]
    assert graph['105:16']['inputs']['conditioning'] == ['105:104', 0]
    trim = graph['aigc:trim-locked-output']['inputs']
    assert trim['audio'] == ['105:104', 2]
    assert trim['frames'] == ['105:10', 0]
    assert trim['duration_seconds'] == ['105:111', 0]
    assert trim['start_seconds'] == 0 and trim['fps'] == 24
    assert graph['105:91']['inputs']['audio'] == ['aigc:trim-locked-output', 1]
    assert graph['105:91']['inputs']['images'] == ['aigc:trim-locked-output', 0]
    assert not any(node['class_type'] in ('VAEDecodeAudio', 'MiniMaxH3AddGuide') for node in graph.values())
    for node in graph.values():
        for value in node['inputs'].values():
            if isinstance(value, list):
                assert value[0] in graph


def test_locked_contract_preserves_old_recipes_and_models(tmp_path):
    before = {key: presets.load_recipe(key) for key in (presets.PRESET_ID, presets.FIRST_PRESET_ID)}
    locked = presets.load_recipe(presets.LOCKED_PRESET_ID)
    specs = presets.input_specs(presets.LOCKED_PRESET_ID)
    assert {spec['id'] for spec in specs} == {spec['id'] for spec in presets.input_specs(presets.FIRST_PRESET_ID)}
    for spec in specs:
        binding = spec['binding']
        assert binding['input'] in locked[binding['node_id']]['inputs']
    assert presets.catalog(tmp_path, presets.LOCKED_PRESET_ID)['missing_models'] == presets.catalog(tmp_path)['missing_models']
    assert len({presets.stage_id(key) for key in presets.PRESET_IDS}) == len(presets.PRESET_IDS)
    assert presets.stage('locked.json', 3, presets.LOCKED_PRESET_ID)['execution']['state'] == '待小样验证'
    for key, graph in before.items():
        assert presets.load_recipe(key) == graph


def test_install_locked_preserves_existing_stages_and_freezes_source_audio_route(preset_client):
    client, project = preset_client
    install(client)
    add_shot(client, project, 'S01')
    first = client.post('/api/projects/preset-test/production-presets/h3-first-guided')
    assert first.status_code == 200, first.text
    previous = first.json()['project']['pipeline']
    result = client.post('/api/projects/preset-test/production-presets/' + presets.LOCKED_PRESET_ID)
    assert result.status_code == 200, result.text
    pipeline = result.json()['project']['pipeline']
    assert pipeline[:2] == previous and len(pipeline) == 3
    validation = client.post('/api/projects/preset-test/pipeline/' + presets.stage_id(presets.LOCKED_PRESET_ID) + '/validate',
                             json={'asset_id': 'S01', 'values': {}})
    assert validation.status_code == 200 and validation.json()['valid'], validation.text
    submitted = client.post('/api/projects/preset-test/tasks', json={'asset_id': 'S01'})
    assert submitted.status_code == 200, submitted.text
    graph = json.loads(Path(submitted.json()['payload']['workflow']).read_text(encoding='utf-8'))
    assert graph['105:104']['inputs']['audio_mode'] == 'lock_source'
    assert graph['105:91']['inputs']['audio'] == ['aigc:trim-locked-output', 1]
    assert graph['92']['_meta']['director_preset'] == presets.LOCKED_PRESET_ID
