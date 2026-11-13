"""单步任务：隐藏容器、以提示词命名的列表，以及跨作品批量删除。"""
import json
from pathlib import Path

import pytest

from backend import app as backend


@pytest.fixture
def quick_client(preset_client, monkeypatch):
    client, project = preset_client
    monkeypatch.setattr(backend, 'keyframe_capability', lambda **kwargs: {'available': True})
    return client, project


def _submit(client, prompt, seed=11):
    workspace = client.get('/api/quick-workspace').json()['project_id']
    response = client.post(f'/api/projects/{workspace}/keyframe-tasks', json={
        'mode': 'text', 'prompt': prompt, 'width': 768, 'height': 1024, 'seed': seed,
        'idempotency_key': f'quick-{seed}',
    })
    assert response.status_code == 202, response.text
    return response.json()


def test_quick_container_is_created_once_and_never_shows_as_a_work(quick_client):
    client, _ = quick_client
    first = client.get('/api/quick-workspace').json()['project_id']
    assert first == backend.QUICK_PROJECT_ID
    assert client.get('/api/quick-workspace').json()['project_id'] == first
    assert first not in [item['id'] for item in client.get('/api/projects').json()['projects']]
    assert 'quick-tasks' not in json.dumps(client.get('/api/projects').json(), ensure_ascii=False)
    assert (Path(backend.load_project_manifest(first)['workspace_root'].split('/')[-1]) == Path('_单步任务'))


def test_quick_tasks_are_titled_by_the_prompt_that_made_them(quick_client):
    client, _ = quick_client
    long_prompt = '我想生成一张雨夜里的城市街角，霓虹灯在积水里拉长，主角撑伞从画面右侧走入，' \
                  '镜头缓慢推进，背景有电车经过，色调偏冷但窗光偏暖，最后停在她的侧脸'
    _submit(client, long_prompt)
    _submit(client, '猫', seed=12)

    body = client.get('/api/quick-tasks').json()
    assert body['project_id'] == backend.QUICK_PROJECT_ID
    titles = {item['title'] for item in body['tasks']}
    assert '猫' in titles
    assert len(body['tasks']) == 2
    assert all(item['tool'] == '图片' for item in body['tasks'])
    assert all(item['deletable'] for item in body['tasks'])
    excerpt = next(item['title'] for item in body['tasks'] if item['title'] != '猫')
    assert excerpt.endswith('…') and len(excerpt) <= 47 and excerpt.startswith('我想生成一张雨夜里的城市街角')


def test_quick_outputs_can_be_adopted_into_a_work_without_duplicates(quick_client):
    """草稿箱的产物要能归入正式作品：复制一份登记成该作品的资产，重复归入不复制第二份。"""
    client, _ = quick_client
    task = _submit(client, '西湖边的人像', seed=31)
    draft_root = backend.resolve_registered_path(
        str(backend.load_project_manifest(backend.QUICK_PROJECT_ID)['workspace_root']))
    draft = draft_root / 'assets' / 'draft.png'
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_bytes(b'draft image bytes')
    backend.set_task(task['id'], status='succeeded', result=json.dumps({'output': str(draft)}))

    created = client.post('/api/projects/create', json={
        'series': '测试', 'title': '归入测试', 'project_id': 'adopt-test', 'creation_mode': 'advanced'})
    assert created.status_code == 200, created.text
    pid = created.json()['project']['id']
    url = f"/api/quick-tasks/{task['id']}/adopt"

    first = client.post(url, json={'project_id': pid})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body['adopted'] == 1 and body['already_adopted'] == 0
    asset = body['assets'][0]
    assert asset['kind'] == 'image' and asset['image_path'].endswith('.png')
    assert asset['adopted_from']['task_id'] == task['id']
    copied = backend.resolve_registered_path(asset['image_path'])
    assert copied.is_file() and copied.read_bytes() == b'draft image bytes'
    assert copied.is_relative_to(backend.resolve_registered_path(
        str(backend.load_project_manifest(pid)['workspace_root'])))

    again = client.post(url, json={'project_id': pid}).json()
    assert again['adopted'] == 0 and again['already_adopted'] == 1
    adopted = [item for item in backend.load_project_manifest(pid)['assets']
               if isinstance(item, dict) and str(item.get('id', '')).startswith('adopt-')]
    assert len(adopted) == 1
    # 草稿箱自己不能作为归入目标。
    assert client.post(url, json={'project_id': backend.QUICK_PROJECT_ID}).status_code == 422


def test_draft_video_with_adopt_into_is_adopted_after_success(quick_client, monkeypatch, tmp_path):
    """「生成到作品」的视频任务：仍落草稿箱，成功后由执行器自动复制归入该作品。"""
    import time
    import uuid
    client, _ = quick_client
    graph = tmp_path / 'video.json'
    graph.write_text('{}', encoding='utf-8')
    task_id = str(uuid.uuid4())
    payload = {'kind': 'workflow', 'workflow': str(graph), 'project_id': backend.QUICK_PROJECT_ID,
               'asset_id': 'S01', 'adopt_into': 'some-work',
               'pipeline_stage_id': 'motion-text-native-landscape-generation',
               'plan_path': str(tmp_path / 'plan.json'), 'request': {'output_prefix': 'draft'}}
    now = time.time()
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute(
            "INSERT INTO tasks (id, asset_id, status, created_at, updated_at, payload, project_id) VALUES (?,?,?,?,?,?,?)",
            (task_id, 'S01', 'preparing', now, now, json.dumps(payload), backend.QUICK_PROJECT_ID))
        connection.commit()
    adopted: list[tuple[str, str]] = []
    monkeypatch.setattr(backend.comfy_engine, 'ensure_online', lambda: {'started': False})
    monkeypatch.setattr(backend.prompt_writer, 'release_quietly', lambda: None)
    monkeypatch.setattr(backend, 'sample_resources', lambda _: None)
    monkeypatch.setattr(backend, 'request_json',
                        lambda path, body=None: {'prompt_id': task_id} if path == '/prompt' else {})
    monkeypatch.setattr(backend, 'history_done', lambda *args, **kwargs: {'status': 'success', 'outputs': ['draft.mp4']})
    monkeypatch.setattr(backend, 'publish_task_outputs', lambda task, done: done)
    monkeypatch.setattr(backend, 'record_segment_result', lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, 'adopt_task_outputs',
                        lambda task, project: adopted.append((str(task['id']), project)) or {'adopted': 1})
    backend.run_workflow_submission(task_id, payload)
    assert backend.get_task(task_id)['status'] == 'succeeded'
    assert adopted == [(task_id, 'some-work')]


def test_speech_tasks_are_named_by_the_line_or_the_designed_timbre(quick_client, monkeypatch):
    client, _ = quick_client
    monkeypatch.setattr(backend.speech, 'capability', lambda *args, **kwargs: {'available': True, 'missing': []})
    workspace = client.get('/api/quick-workspace').json()['project_id']
    reference = Path(backend.load_project_manifest(workspace)['workspace_root']) / 'voice.wav'
    reference.write_bytes(b'reference audio')
    endpoint = f'/api/projects/{workspace}/speech-tasks'
    for key, body in {
        'quick-timbre': {'engine': 'voxcpm-2', 'mode': 'design', 'text': '这段声音只用来试音色。',
                         'voice_description': '六十岁老先生，声音沙哑缓慢'},
        'quick-dialogue': {'engine': 'voxcpm-2', 'text': '别回头，先看完这段。', 'voice_reference': str(reference)},
    }.items():
        response = client.post(endpoint, json={**body, 'idempotency_key': key})
        assert response.status_code == 202, response.text
    cards = {item['title']: item['tool'] for item in client.get('/api/quick-tasks').json()['tasks']}
    # 音色设计 writes its own name into the list; an ordinary take stays 配音 and shows the line.
    assert cards['六十岁老先生，声音沙哑缓慢'] == '音色设计'
    assert cards['别回头，先看完这段。'] == '配音'


def test_batch_delete_previews_then_removes_rows_and_snapshots(quick_client):
    client, _ = quick_client
    first, second = _submit(client, '清晨的渔港', 21), _submit(client, '正午的沙漠', 22)
    snapshots = [Path(task['payload']['workflow']) for task in (first, second)]
    assert all(path.is_file() for path in snapshots)

    plan = client.post('/api/tasks/deletion-plan', json={'task_ids': [first['id'], second['id']]})
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert {entry['task_id'] for entry in body['entries']} == {first['id'], second['id']}
    assert '不会被删除' in body['preserved']

    refused = client.post('/api/tasks/deletion', json={'plan_id': body['plan_id'], 'task_ids': [first['id'], second['id']]})
    assert refused.status_code == 422
    assert refused.json()['detail']['code'] == 'confirmation_required'
    assert all(path.is_file() for path in snapshots)

    applied = client.post('/api/tasks/deletion', json={
        'plan_id': body['plan_id'], 'task_ids': [first['id'], second['id']], 'confirm': True,
    })
    assert applied.status_code == 200, applied.text
    assert sorted(applied.json()['deleted']) == sorted([first['id'], second['id']])
    assert not any(path.exists() for path in snapshots)
    assert client.get('/api/quick-tasks').json()['tasks'] == []
    assert client.get('/api/projects').json()['projects'][0]['id'] == 'preset-test'


def test_batch_delete_refuses_a_wider_scope_or_a_moving_status(quick_client):
    client, _ = quick_client
    task = _submit(client, '黄昏的天台', 31)
    plan = client.post('/api/tasks/deletion-plan', json={'task_ids': [task['id']]}).json()

    widened = client.post('/api/tasks/deletion', json={
        'plan_id': plan['plan_id'], 'task_ids': [task['id'], '0' * 8], 'confirm': True,
    })
    assert widened.status_code == 409

    backend.set_task(task['id'], status='running', prompt_id='remote-9')
    stale = client.post('/api/tasks/deletion-plan', json={'task_ids': [task['id']]})
    assert stale.status_code == 409
    assert '停止' in stale.json()['detail']


def test_batch_delete_keeps_a_snapshot_still_used_by_another_task(quick_client):
    client, _ = quick_client
    kept = _submit(client, '共用快照的任务', 41)
    dropped = _submit(client, '要被删掉的任务', 42)
    shared = Path(kept['payload']['workflow'])
    # 两条任务指向同一份冻结快照时，删除其中一条不能把另一条的执行依据删掉。
    backend.set_task(dropped['id'], payload=json.dumps({
        **dropped['payload'], 'workflow': str(shared),
        'execution_snapshot': {**dropped['payload']['execution_snapshot'], 'api_graph': str(shared)},
    }, ensure_ascii=False))

    plan = client.post('/api/tasks/deletion-plan', json={'task_ids': [dropped['id']]}).json()
    applied = client.post('/api/tasks/deletion', json={
        'plan_id': plan['plan_id'], 'task_ids': [dropped['id']], 'confirm': True,
    })
    assert applied.status_code == 200, applied.text
    # 共享的 workflow 必须留下；被删任务自己独占的 request.json 等记录清掉是正确的。
    assert str(shared) not in applied.json()['removed_snapshots']
    assert shared.is_file()
    assert client.get(f'/api/projects/{backend.QUICK_PROJECT_ID}/tasks/{kept["id"]}').status_code == 200


def test_batch_plan_rejects_unknown_tasks_and_bad_ids(quick_client):
    client, _ = quick_client
    assert client.post('/api/tasks/deletion-plan', json={'task_ids': ['nope']}).status_code == 404
    assert client.post('/api/tasks/deletion', json={
        'plan_id': '../escape', 'task_ids': ['nope'], 'confirm': True,
    }).status_code == 422


def test_image_card_carries_its_own_inputs_back_for_editing(quick_client):
    client, _ = quick_client
    _submit(client, '雨夜街角，霓虹在积水里拉长', seed=77)
    card = client.get('/api/quick-tasks').json()['tasks'][0]
    assert card['reuse'] == {'tool': 'image', 'values': {
        'prompt': '雨夜街角，霓虹在积水里拉长', 'quality': 'native', 'seed': 77, 'aspect': '9:16', 'steps': 8}}


def test_enhancement_rows_are_grouped_as_enhancement_not_video(quick_client):
    client, _ = quick_client
    submitted = _submit(client, '要被清晰化的原图', seed=88)
    source = str(Path(backend.OUTPUT_ROOT) / f'{submitted["id"]}_00001_.png').replace('\\', '/')
    # 清晰化 also runs a ComfyUI graph, so its payload kind is 'workflow'; the row must
    # still land in the 清晰化 block, link its result and offer the source for another pass.
    backend.set_task(submitted['id'], status='succeeded', payload=json.dumps({
        'kind': 'workflow', 'pipeline_stage_id': 'media-enhancement', 'project_id': backend.QUICK_PROJECT_ID,
        'asset_id': f'HD-{submitted["id"]}', 'workflow': submitted['payload']['workflow'],
        'execution_snapshot': {'api_graph': submitted['payload']['workflow']},
        'enhancement': {'source': source, 'source_facts': {'kind': 'image'}, 'model': 'RealESRGAN_x2plus.pth', 'scale': 2},
    }, ensure_ascii=False), result=json.dumps({
        'status': 'success', 'outputs': ['导演工作台工作目录/quick-tasks/enhancement/out_00001_.png'],
    }, ensure_ascii=False))

    card = client.get('/api/quick-tasks').json()['tasks'][0]
    assert card['tool'] == '清晰化'
    assert card['video_mode'] is None
    assert card['output'] == 'output/导演工作台工作目录/quick-tasks/enhancement/out_00001_.png'
    # 清晰化 has no prompt; the row is named by the file it was made from.
    assert card['title'] == f'{submitted["id"]}_00001_'
    assert card['reuse']['tool'] == 'enhance'
    assert card['reuse']['values']['source'].endswith('_00001_.png')


def test_timbre_and_music_cards_reuse_their_frozen_parameters(quick_client, monkeypatch):
    client, _ = quick_client
    monkeypatch.setattr(backend.speech, 'capability', lambda *args, **kwargs: {'available': True, 'missing': []})
    workspace = client.get('/api/quick-workspace').json()['project_id']
    response = client.post(f'/api/projects/{workspace}/speech-tasks', json={
        'engine': 'voxcpm-2', 'mode': 'design', 'text': '这段声音只用来试音色。',
        'voice_description': '六十岁老先生，声音沙哑缓慢', 'seed': 4242, 'idempotency_key': 'reuse-timbre',
    })
    assert response.status_code == 202, response.text
    task = backend.get_task(response.json()['id'])
    assert backend.quick_task_reuse(task['payload']) == {'tool': 'timbre', 'values': {
        'timbre': '六十岁老先生，声音沙哑缓慢', 'text': '这段声音只用来试音色。', 'seed': 4242}}

    frozen = {'tags': 'lofi, rain', 'lyrics': '', 'duration_seconds': 12.0, 'bpm': 84, 'keyscale': 'C major',
              'timesignature': '4', 'language': 'unknown', 'seed': 99, 'steps': 8, 'cfg': 1.0, 'shift': 3.0,
              'generate_audio_codes': True, 'encode_cfg_scale': 2.0, 'temperature': 0.85, 'top_p': 0.9,
              'top_k': 0, 'min_p': 0.0, 'format': 'mp3', 'quality': 'V0'}
    reuse = backend.quick_task_reuse({'kind': 'music', 'music': frozen})
    assert reuse['tool'] == 'music' and reuse['values']['seed'] == 99
    # 表单里叫 output_format；带回来的名字要跟表单一致，否则参数看着像带回了其实没带上。
    assert reuse['values']['music']['output_format'] == 'mp3'
    assert 'format' not in reuse['values']['music']
    assert reuse['values']['music']['tags'] == 'lofi, rain'


def test_pipeline_video_reuses_prompt_duration_and_its_own_preset(quick_client, monkeypatch):
    """文生视频/图生视频走作品管线，payload 没有 kind，也要能「编辑重生成」。

    视频工具必须显式选制作方案，所以方案 id 必须一起带回去；否则表单看着像上次，
    实际按另一条路线（画幅/步数/LoRA 都不同）重跑。
    """
    reuse = backend.quick_task_reuse({
        'pipeline_stage_id': 'motion-text-native-landscape-generation',
        'segment_snapshot': {
            'prompt': '西湖边自拍散步', 'duration_seconds': 4,
            'generation': {'aspect_ratio': '16:9', 'output_resolution': '2k', 'seed': 99,
                           'sound_mode': 'performance_reference'},
            'keyframes': {'first': None, 'last': None},
        },
    })
    assert reuse and reuse['tool'] == 'text-video'
    assert reuse['values'] == {'prompt': '西湖边自拍散步', 'duration': 4, 'aspect': '16:9',
                               'quality': '2k', 'preset': 'h3-text-native-landscape'}
    # 有首帧就是图生视频，方案同样跟着走。首帧能否回填取决于它是否还在登记目录里，
    # 这里只验工具分流与方案映射，路径解析单独由 _portable 的契约保证。
    monkeypatch.setattr(backend, '_portable', lambda value: value)
    framed = backend.quick_task_reuse({
        'pipeline_stage_id': 'motion-first-native-portrait-generation',
        'segment_snapshot': {'prompt': '她在窗边回头', 'duration_seconds': 5,
                             'generation': {'aspect_ratio': '9:16', 'output_resolution': 'native'},
                             'keyframes': {'first': 'workspaces/x/assets/全身.png'}},
    })
    assert framed and framed['tool'] == 'image-video'
    assert framed['values']['preset'] == 'h3-first-native-portrait'
    assert framed['values']['frame'].endswith('全身.png')


def test_reuse_stays_absent_when_the_inputs_cannot_be_reproduced(quick_client):
    # 克隆音色的起点是一段音频，不能凭提示词还原，就不给这个入口。
    assert backend.quick_task_reuse({'kind': 'speech', 'request': {'mode': 'reference', 'voice_reference': 'a.wav'}}) is None
    # 参考图任务没留下任何槽位时同理：带回去是个空表单，比没有入口更糟。
    assert backend.quick_task_reuse({'kind': 'keyframe', 'request': {'mode': 'reference', 'prompt': '猫'}}) is None
    assert backend.quick_task_reuse({'kind': 'workflow'}) is None
    assert backend.quick_task_reuse({'kind': 'audio_swap'}) is None


def test_reference_keyframe_reuses_back_into_the_first_frame_studio(quick_client):
    """参考图任务要能「编辑重生成」，并把当时的槽位一起带回首帧工作台。"""
    reuse = backend.quick_task_reuse({
        'kind': 'keyframe',
        'request': {'mode': 'reference', 'prompt': '猫', 'width': 768, 'height': 1344,
                    'seed': 7, 'creative_profile': 'general',
                    'references': [{'role': '脸型', 'material_id': 'mat-abc123456789', 'label': '正面脸图'}]},
    })
    assert reuse and reuse['tool'] == 'image-video'
    assert reuse['values']['prompt'] == '猫'
    assert reuse['values']['aspect'] == '9:16'
    assert reuse['values']['seed'] == 7
    assert reuse['values']['references'][0]['material_id'] == 'mat-abc123456789'


def test_reference_keyframe_reads_material_slots_from_the_submission_receipt(quick_client):
    """冻结后的槽只留副本路径，material_id 得从提交回执里取回来，否则会误判成单图。"""
    reuse = backend.quick_task_reuse({
        'kind': 'keyframe',
        'request': {'mode': 'reference', 'prompt': '猫', 'width': 768, 'height': 1344,
                    'creative_profile': 'general',
                    'references': [{'role': '脸型', 'label': '正面脸图',
                                    'path': 'workspaces/x/keyframe-tasks/a/reference-1.png'}]},
        'submission_receipt': {'request': {'references': [
            {'role': '脸型', 'material_id': 'mat-abc123456789'}]}},
    })
    assert reuse and reuse['tool'] == 'image-video'
    assert reuse['values']['references'] == [
        {'role': '脸型', 'material_id': 'mat-abc123456789', 'label': '正面脸图'}]


def test_single_reference_image_reuses_back_into_the_reference_generator(quick_client):
    """参考图生成工具产出的任务只有一张底图：编辑要回「参考图生成」，不是图生视频。"""
    reuse = backend.quick_task_reuse({
        'kind': 'keyframe',
        'request': {'mode': 'reference', 'prompt': '猫', 'width': 1024, 'height': 1024,
                    'refine': False, 'references': [
                        {'role': '底图', 'label': '',
                         'path': 'workspaces/quick/keyframe-tasks/abc/reference-1.png'}]},
    })
    assert reuse and reuse['tool'] == 'reference-image'
    assert reuse['values']['prompt'] == '猫'
    assert reuse['values']['aspect'] == '1:1'
    assert reuse['values']['refine'] is False
    assert str(reuse['values']['image']).endswith('reference-1.png')


def test_reference_image_falls_back_to_the_submitted_path(quick_client):
    """老任务没冻结底图槽时，用提交时的 reference_image 顶上，别把入口丢掉。"""
    reuse = backend.quick_task_reuse({
        'kind': 'keyframe',
        'request': {'mode': 'reference', 'prompt': '猫',
                    'reference_image': 'workspaces/quick/assets/a.png'},
    })
    assert reuse and reuse['tool'] == 'reference-image'
    assert str(reuse['values']['image']).endswith('assets/a.png')



def test_inpaint_rows_are_labelled_as_inpaint_not_text_to_image():
    from backend.routers.quick_tasks import task_tool
    assert task_tool({'kind': 'keyframe', 'request': {'mode': 'inpaint'}}) == '局部重绘'
    assert task_tool({'kind': 'keyframe', 'request': {'mode': 'text'}}) == '图片'
    assert task_tool({'kind': 'keyframe'}) == '图片'
    assert task_tool({'kind': 'speech', 'request': {'mode': 'inpaint'}}) == '配音'


def test_inpaint_rows_are_labelled_as_inpaint_not_text_to_image():
    from backend.routers.quick_tasks import task_tool
    assert task_tool({'kind': 'keyframe', 'request': {'mode': 'inpaint'}}) == '局部重绘'
    assert task_tool({'kind': 'keyframe', 'request': {'mode': 'text'}}) == '图片'
    assert task_tool({'kind': 'keyframe'}) == '图片'
    assert task_tool({'kind': 'speech', 'request': {'mode': 'inpaint'}}) == '配音'


def test_video_tasks_without_a_kind_are_labelled_as_video():
    """视频任务走作品管线，payload 里没有 kind；以前这里落到兜底的「任务」，首页归到「其他」。"""
    from backend.routers.quick_tasks import task_tool
    assert task_tool({'pipeline_stage_id': 'motion-first-realism-people-generation'}) == '视频'
    assert task_tool({'kind': 'workflow', 'pipeline_stage_id': 'x'}) == '视频'
    assert task_tool({'kind': 'keyframe'}) == '图片'


def test_quick_card_reads_first_frame_from_the_materials_block():
    from backend.routers.quick_tasks import quick_task_card
    payload = {'pipeline_stage_id': 'motion-first-realism-people-generation',
               'materials': {'first-frame': r'D:/out/frame.png'},
               'pipeline_values': {'prompt': '雨夜街角'}}
    result = {'outputs': [r'导演工作台工作目录\quick-tasks\video/S01-8.mp4']}
    card = quick_task_card({'id': 't1', 'payload': payload, 'result': result, 'status': 'succeeded',
                            'created_at': 0, 'updated_at': 0, 'error': None})
    assert card['tool'] == '视频'
    assert card['video_mode'] == 'image-video'
    assert card['output'] == r'output/导演工作台工作目录\quick-tasks\video/S01-8.mp4'
    assert card['outputs'] == [r'output/导演工作台工作目录\quick-tasks\video/S01-8.mp4']


def test_quick_card_without_a_first_frame_is_text_to_video():
    from backend.routers.quick_tasks import quick_task_card
    card = quick_task_card({'id': 't2', 'status': 'succeeded', 'created_at': 0, 'updated_at': 0,
                            'payload': {'pipeline_stage_id': 'motion-first-generation'},
                            'result': {'outputs': ['a.mp4', 'b.mp4']}})
    assert card['video_mode'] == 'text-video'
    assert card['output'] is None
    assert card['outputs'] == ['output/a.mp4', 'output/b.mp4']


def test_video_tasks_without_a_kind_are_labelled_as_video():
    """视频任务走作品管线，payload 里没有 kind；以前这里落到兜底的「任务」，首页归到「其他」。"""
    from backend.routers.quick_tasks import task_tool
    assert task_tool({'pipeline_stage_id': 'motion-first-realism-people-generation'}) == '视频'
    assert task_tool({'kind': 'workflow', 'pipeline_stage_id': 'x'}) == '视频'
    assert task_tool({'kind': 'keyframe'}) == '图片'


def test_quick_card_reads_first_frame_from_the_materials_block():
    from backend.routers.quick_tasks import quick_task_card
    payload = {'pipeline_stage_id': 'motion-first-realism-people-generation',
               'materials': {'first-frame': r'D:\out\frame.png'},
               'pipeline_values': {'prompt': '雨夜街角'}}
    result = {'outputs': [r'导演工作台工作目录\quick-tasks\video/S01-8.mp4']}
    card = quick_task_card({'id': 't1', 'payload': payload, 'result': result, 'status': 'succeeded',
                            'created_at': 0, 'updated_at': 0, 'error': None})
    assert card['tool'] == '视频'
    assert card['video_mode'] == 'image-video'
    assert card['output'] == r'output/导演工作台工作目录\quick-tasks\video/S01-8.mp4'
    assert card['outputs'] == [r'output/导演工作台工作目录\quick-tasks\video/S01-8.mp4']


def test_quick_card_without_a_first_frame_is_text_to_video():
    from backend.routers.quick_tasks import quick_task_card
    card = quick_task_card({'id': 't2', 'status': 'succeeded', 'created_at': 0, 'updated_at': 0,
                            'payload': {'pipeline_stage_id': 'motion-first-generation'},
                            'result': {'outputs': ['a.mp4', 'b.mp4']}})
    assert card['video_mode'] == 'text-video'
    assert card['output'] is None
    assert card['outputs'] == ['output/a.mp4', 'output/b.mp4']


def test_card_duration_uses_started_at_not_created_at():
    """耗时从「真正开始跑」算：排队等 GPU 的那段不该算进生成时间。"""
    card = backend.quick_task_card({
        'id': 't1', 'status': 'succeeded', 'payload': {}, 'result': {},
        'created_at': 100.0, 'started_at': 130.0, 'updated_at': 145.5,
    })
    assert card['started_at'] == 130.0
    assert card['duration_ms'] == 15500


def test_card_duration_falls_back_for_legacy_tasks_without_started_at():
    """11-09 之前入库的任务没有 started_at，退回 created_at 显示，别整块不显示。"""
    card = backend.quick_task_card({
        'id': 't2', 'status': 'failed', 'payload': {}, 'result': {},
        'created_at': 100.0, 'updated_at': 112.0,
    })
    assert card['duration_ms'] == 12000


def test_card_duration_is_live_while_running(monkeypatch):
    monkeypatch.setattr(backend.time, 'time', lambda: 200.0)
    card = backend.quick_task_card({
        'id': 't3', 'status': 'running', 'payload': {}, 'result': {},
        'created_at': 150.0, 'started_at': 180.0, 'updated_at': 181.0,
    })
    assert card['duration_ms'] == 20000


def test_card_outputs_keep_media_object_paths():
    """媒体/剪辑任务的 outputs 是 {path} 对象，不能再拼 output/ 前缀。

    之前统一按 ComfyUI 的字符串产物处理，拼出 `output/{'path': ...}`，
    首页「查看结果」就变成一个打不开的黑框。
    """
    card = backend.quick_task_card({
        'id': 't4', 'status': 'succeeded', 'payload': {'kind': 'video_edit'},
        'result': {'output': 'D:/x/edit.mp4', 'outputs': [{'kind': 'video', 'path': 'D:/x/edit.mp4'}]},
        'created_at': 1.0, 'updated_at': 2.0,
    })
    assert card['outputs'] == ['D:/x/edit.mp4']
    assert card['output'] == 'D:/x/edit.mp4'


def test_card_outputs_prefix_comfy_relative_strings():
    card = backend.quick_task_card({
        'id': 't5', 'status': 'succeeded', 'payload': {'kind': 'workflow'},
        'result': {'outputs': ['导演工作台工作目录/quick-tasks/video/a.mp4']},
        'created_at': 1.0, 'updated_at': 2.0,
    })
    assert card['outputs'] == ['output/导演工作台工作目录/quick-tasks/video/a.mp4']


def test_edit_card_title_counts_clips_and_seconds():
    card = backend.quick_task_card({
        'id': 't6', 'status': 'succeeded', 'payload': {
            'kind': 'video_edit',
            'request': {'clips': [
                {'start_seconds': 0.5, 'end_seconds': 2.0},
                {'start_seconds': 1.0, 'end_seconds': 2.5},
            ]},
        },
        'result': {'output': 'D:/x/edit.mp4', 'outputs': [{'path': 'D:/x/edit.mp4'}]},
        'created_at': 1.0, 'updated_at': 2.0,
    })
    assert card['title'] == '剪辑 · 2 段 · 3.0 秒'


def test_reuse_carries_the_linked_voice_and_music(monkeypatch):
    """编辑重生成不该把「关联声音」丢回不关联：配音和配乐都要带回表单。"""
    monkeypatch.setattr(backend, '_portable', lambda value: value)
    reuse = backend.quick_task_reuse({
        'pipeline_stage_id': 'motion-first-locked-audio-generation',
        'music_overlay': 'workspaces/quick-tasks/assets/music.wav',
        'segment_snapshot': {
            'prompt': '她坐在窗边说话', 'duration_seconds': 6,
            'generation': {'aspect_ratio': '9:16', 'output_resolution': 'native'},
            'keyframes': {'first': 'workspaces/quick-tasks/assets/frame.png'},
            'audio': {'guide': 'workspaces/quick-tasks/assets/voice.wav'},
        },
    })
    assert reuse['tool'] == 'image-video'
    assert reuse['values']['preset'] == 'h3-first-locked-audio'
    assert reuse['values']['voice'].endswith('voice.wav')
    assert reuse['values']['music_track'].endswith('music.wav')


def test_result_stays_unread_until_the_user_opens_it(quick_client):
    client, _ = quick_client
    task = _submit(client, '雨夜街角的一盏灯')
    backend.set_task(task['id'], status='succeeded', result=json.dumps({'output': 'out/x.png'}))

    card = next(item for item in client.get('/api/quick-tasks').json()['tasks'] if item['id'] == task['id'])
    assert card['unread'] is True and card['read_at'] is None

    first = client.post(f"/api/tasks/{task['id']}/read")
    assert first.status_code == 200, first.text
    card = next(item for item in client.get('/api/quick-tasks').json()['tasks'] if item['id'] == task['id'])
    assert card['unread'] is False and card['read_at']

    # 重复打开是幂等的：保留第一次阅读时间，不会每次刷新都往后跳。
    again = client.post(f"/api/tasks/{task['id']}/read").json()
    assert again['read_at'] == first.json()['read_at']


def test_unfinished_tasks_are_not_marked_read(quick_client):
    client, _ = quick_client
    task = _submit(client, '还没跑完的镜头')
    response = client.post(f"/api/tasks/{task['id']}/read")
    assert response.status_code == 409
    card = next(item for item in client.get('/api/quick-tasks').json()['tasks'] if item['id'] == task['id'])
    assert card['unread'] is False


def test_bulk_read_only_touches_finished_tasks(quick_client):
    client, _ = quick_client
    done = _submit(client, '已经出图的镜头', seed=21)
    pending = _submit(client, '还在排队的镜头', seed=22)
    backend.set_task(done['id'], status='succeeded', result=json.dumps({'output': 'out/y.png'}))
    body = client.post('/api/tasks/read', json={'task_ids': [done['id'], pending['id']]})
    assert body.status_code == 200, body.text
    assert body.json()['marked'] == 1
    cards = {item['id']: item for item in client.get('/api/quick-tasks').json()['tasks']}
    assert cards[done['id']]['unread'] is False
    assert cards[pending['id']]['read_at'] is None
