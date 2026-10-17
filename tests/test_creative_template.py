from test_production_presets import preset_client


def test_series_template_keeps_source_version_without_inheriting_story_or_approval(preset_client):
    client, _ = preset_client
    endpoint = '/api/projects/preset-test/creative-template'
    assert client.get(endpoint).status_code == 409
    saved = client.post('/api/projects/preset-test/creative', json={
        'expected_revision': 0, 'status': 'approved', 'values': {
            'world': '雨后小镇', 'character': '黄色玩偶', 'voice': '温暖女声', 'intent': '本集专属剧情',
            'adaptation': '本集改编', 'reference_video': '旧片链接', 'notes': '本集备注'}})
    assert saved.status_code == 200, saved.text
    before = client.get('/api/projects/preset-test/state').json()
    template = client.get(endpoint).json()
    assert template['source']['project_id'] == 'preset-test'
    assert template['source']['revision'] == before['creative']['revision']
    assert template['source']['status'] == 'approved'
    assert template['status'] == 'pending_review'
    assert template['values']['character'] == '黄色玩偶'
    assert template['values']['world'] == '雨后小镇'
    assert template['values']['voice'] == '温暖女声'
    assert all(template['values'][field] == '' for field in ('intent', 'adaptation', 'reference_video', 'notes'))
    assert 'preset-test' in template['values']['reference_series']
    assert client.get('/api/projects/preset-test/state').json() == before
