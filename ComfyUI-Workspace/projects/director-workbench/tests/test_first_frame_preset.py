import pytest
import json
from pathlib import Path

from backend import production_presets as presets
from tests.test_production_presets import preset_client, install, add_shot


def test_first_frame_recipe_removes_only_end_constraint():
    original = presets.load_recipe()
    first = presets.load_recipe(presets.FIRST_PRESET_ID)
    assert len(first) == len(original) - 1
    assert 'ip-video:last-frame:2' not in first
    for key, node in first.items():
        expected = {**original[key], 'inputs': dict(original[key]['inputs'])}
        expected['inputs'].pop('last_frame', None)
        assert node == expected
        for value in node['inputs'].values():
            if isinstance(value, list):
                assert value[0] in first
    assert presets.load_recipe() == original


def test_first_frame_contract_matches_graph_and_preserves_original():
    graph = presets.load_recipe(presets.FIRST_PRESET_ID)
    specs = presets.input_specs(presets.FIRST_PRESET_ID)
    assert 'last-frame' not in {item['id'] for item in specs}
    assert 'last-frame' in {item['id'] for item in presets.input_specs()}
    for item in specs:
        binding = item['binding']
        assert binding['input'] in graph[binding['node_id']]['inputs']
    stage = presets.stage('first.json', 2, presets.FIRST_PRESET_ID)
    assert stage['id'] == 'motion-first-generation'
    assert stage['execution']['references'] == ['first.json']
    assert '尾帧' not in stage['inputs']
    assert presets.stage('old.json', 1)['id'] == 'motion-generation'


def test_first_frame_catalog_exposes_same_model_requirements(tmp_path):
    original = presets.catalog(tmp_path)
    first = presets.catalog(tmp_path, presets.FIRST_PRESET_ID)
    assert first['available']
    assert first['id'] == 'h3-first-guided'
    assert first['missing_models'] == original['missing_models']


@pytest.mark.parametrize('function', [presets.load_recipe, presets.input_specs, presets.stage_id, presets.title])
def test_unknown_preset_is_rejected(function):
    with pytest.raises(ValueError, match='不存在'):
        function('unknown')


@pytest.mark.parametrize('requested_stage,first_only', [(None, True), ('motion-first-generation', True), ('motion-generation', False)])
def test_api_presets_coexist_and_select_independent_frozen_graphs(preset_client, requested_stage, first_only):
    client, project = preset_client
    old = install(client)
    add_shot(client, project, 'S01')
    response = client.post('/api/projects/preset-test/production-presets/h3-first-guided')
    assert response.status_code == 200, response.text
    manifest = response.json()['project']
    assert len(manifest['pipeline']) == 2
    assert manifest['pipeline'][0] == old['pipeline'][0]
    before = client.get('/api/plan').json()
    selected_stage = requested_stage or 'motion-first-generation'
    validated = client.post('/api/projects/preset-test/pipeline/' + selected_stage + '/validate', json={'asset_id': 'S01', 'values': {}})
    assert validated.status_code == 200, validated.text
    assert validated.json()['valid'] is True
    assert validated.json()['binding_count'] == (6 if first_only else 7)
    body = {'asset_id': 'S01'}
    if requested_stage:
        body['pipeline_stage_id'] = requested_stage
    response = client.post('/api/projects/preset-test/tasks', json=body)
    assert response.status_code == 200, response.text
    task = response.json()
    graph = json.loads(Path(task['payload']['workflow']).read_text(encoding='utf-8'))
    assert ('ip-video:last-frame:2' not in graph) == first_only
    assert graph['92']['_meta']['director_preset'] == (presets.FIRST_PRESET_ID if first_only else presets.PRESET_ID)
    assert client.get('/api/plan').json() == before
    assert before['segments'][0]['keyframes']['last']


def test_catalog_api_lists_both_presets(preset_client):
    client, _ = preset_client
    assert {item['id'] for item in client.get('/api/production-presets').json()['presets']} == set(presets.PRESET_IDS)
