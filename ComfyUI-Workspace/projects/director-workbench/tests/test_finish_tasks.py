"""AI finish candidate contract tests using isolated synthetic media."""
import json
from pathlib import Path

from backend import app as backend
from test_production_presets import add_shot, preset_client


def test_finish_task_requires_explicit_sample_approval(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01', with_audio=False)
    plan = client.get('/api/projects/preset-test/plan').json()
    response = client.post('/api/projects/preset-test/finish-tasks', json={
        'asset_id': 'S01', 'expected_plan_revision': plan['revision'],
        'expected_review_revision': 0, 'idempotency_key': 'finish-unapproved',
    })
    assert response.status_code == 409
    assert '采用样片' in response.text


def test_reusable_template_catalog_is_read_only_and_keeps_project_preset(preset_client):
    client, project = preset_client
    response = client.get('/api/projects/preset-test/reusable-templates')
    assert response.status_code == 200
    templates = response.json()['templates']
    assert [item['id'] for item in templates] == ['image', 'text-video', 'video']
    assert all(item['recommended'] for item in templates)
    assert client.get('/api/projects/preset-test').json()['id'] == 'preset-test'


def test_finish_result_is_registered_as_b_without_replacing_a(preset_client, monkeypatch):
    client, project = preset_client
    add_shot(client, project, 'S01', with_audio=False)
    document, plan_path = backend.load_project_plan('preset-test')
    assets = project / 'workspaces' / 'preset-test' / 'assets'
    original = assets / 'original.mp4'
    finish = assets / 'finish.mp4'
    original.write_bytes(b'original')
    finish.write_bytes(b'finish')
    document['segments'][0].update(
        status='video_generated', comfyui_task_id='sample-prompt', current_version_task_id='sample-task',
        video={'path': str(original)},
    )
    plan_path.write_text(json.dumps(document), encoding='utf-8')
    payload = {
        'project_id': 'preset-test', 'finish_model': 'RealESRGAN_x2plus.pth',
        'finish_source': {'path': str(original), 'task_id': 'sample-task', 'prompt_id': 'sample-prompt'},
    }
    monkeypatch.setattr(backend, 'authorized_media_facts', lambda path: {
        'known': True, 'width': 1728, 'height': 960, 'duration_seconds': 4.458,
        'fps': 24.0, 'frames': 107, 'audio_streams': 1,
    })
    done = {'status': 'success', 'outputs': ['finish.mp4'], 'published_outputs': [str(finish)]}
    assert backend.record_finish_result('S01', 'finish-prompt', 'finish.json', done, plan_path,
                                        task_id='finish-task', payload=payload)
    updated = json.loads(plan_path.read_text())['segments'][0]
    assert updated['video']['path'] == str(original)
    assert updated['video']['finish_review']['path'] == str(finish)
    assert updated['video']['finish_review']['variant'] == 'v3-B-finish'
    assert updated['video']['finish_review']['processing']['output_size']['width'] == 1728
    assert updated['video']['finish_review']['processing']['output_size']['height'] == 960
    assert '1728×960' in updated['video']['finish_review']['specs']


def test_project_export_includes_candidate_manifest_for_series_handoff(preset_client):
    client, project = preset_client
    add_shot(client, project, 'S01', with_audio=False)
    exported = client.get('/api/projects/preset-test/export')
    assert exported.status_code == 200
    assert exported.json()['candidate_manifest'][0]['asset_id'] == 'S01'
    context = exported.json()['series_context']
    assert context['current_project_id'] == 'preset-test'
    assert context['works'][0]['id'] == 'preset-test'
    assert context['handoff']['last_segment_id'] == 'S01'

