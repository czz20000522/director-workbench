"""删除时的连带清除与可选归档：不勾选就全删，勾选的进素材库并登记。"""
import json
import time
from pathlib import Path

import pytest

from backend import app as backend
from backend import creative_settings, library_archive, preset_library

TIMBRE = '音色样本 · 待试听'


@pytest.fixture
def libs(preset_client_factory, tmp_path, monkeypatch):
    client, project = preset_client_factory()
    assets = tmp_path / '素材库'
    output = tmp_path / 'comfy-output'
    assets.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    monkeypatch.setattr(backend, 'ASSETS_ROOT', assets)
    monkeypatch.setattr(backend, 'WORKS_ARCHIVE_ROOT', tmp_path / '作品')
    monkeypatch.setattr(backend, 'OUTPUT_ROOT', output)
    monkeypatch.setattr(backend, 'RUNTIME', tmp_path / 'runtime')
    return client, project, assets, output


def _project(client, project_id='lib-work', title='归档测试'):
    response = client.post('/api/projects/create', json={
        'creation_mode': 'advanced', 'series': '测试', 'title': title, 'project_id': project_id, 'select': False})
    assert response.status_code == 200, response.text
    return project_id


def _register(project_id, asset_id, kind, path, *, subtitle='', title='', owner=None, owner_field='' ):
    manifest = backend.require_project_manifest(project_id)
    record = {'id': asset_id, 'kind': kind, 'title': title or asset_id,
              'subtitle': subtitle, 'ready': True, 'sources': {'A': str(path)}}
    if owner and owner_field:
        record[owner_field] = owner
    manifest['assets'].append(record)
    backend.save_project_manifest(manifest)


def _insert_task(project_id, task_id, payload=None, asset_id=None):
    with backend.DB_LOCK, backend.db() as connection:
        connection.execute(
            'INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,project_id) VALUES (?,?,?,?,?,?,?)',
            (task_id, asset_id or task_id, 'succeeded', time.time(), time.time(), json.dumps(payload or {}), project_id))
        connection.commit()


def test_only_images_and_timbre_samples_are_archivable(libs):
    client, project, assets, _ = libs
    work = _project(client)
    image = assets / 'shot.png'
    sample = assets / 'voice.wav'
    line = assets / 'line.wav'
    for path in (image, sample, line):
        path.write_bytes(b'x')
    _register(work, 'img', 'image', image, title='关键帧 · 夜街')
    _register(work, 'timbre', 'audio', sample, subtitle=TIMBRE, title='音色 · 沙哑老者')
    _register(work, 'line', 'audio', line, subtitle='声音候选 · 待试听', title='台词 · 别回头')
    items = library_archive.candidates(work)
    assert [item['target'] for item in items] == ['reference', 'timbre']
    assert all(item['project_id'] == work for item in items)


def test_archive_moves_into_the_library_and_registers_both_kinds(libs):
    client, project, assets, _ = libs
    work = _project(client)
    image = assets / 'shot.png'
    voice = assets / 'voice.wav'
    for path in (image, voice):
        path.write_bytes(b'x')
    _register(work, 'img', 'image', image, title='夜街参考')
    _register(work, 'timbre', 'audio', voice, subtitle=TIMBRE, title='沙哑老者')
    manifest = backend.require_project_manifest(work)
    next(asset for asset in manifest['assets'] if asset['id'] == 'timbre')['origin'] = 'VoxCPM2'
    backend.save_project_manifest(manifest)
    done = library_archive.archive(library_archive.candidates(work))
    assert {item['target'] for item in done} == {'reference', 'timbre'}
    kept = {Path(item['path']) for item in done}
    assert all(path.is_file() and path.parent.parent.name == '素材库' for path in kept)
    assert not image.exists() and not voice.exists()
    cards = [card for card in preset_library.cards('timbre') if card['builtin'] is False]
    assert [card['params']['engine'] for card in cards] == ['indextts-2.5']
    # 样本是 VoxCPM2 做的，卡片要如实记下生成引擎（即便带入时用 IndexTTS 的参考模式）。
    assert cards[0]['params']['generated_by'] == 'voxcpm-2'
    assert cards[0]['params']['voice_reference'].startswith('素材库/音色/')
    config = json.loads((backend.ROOT / 'config/reference-library.json').read_text(encoding='utf-8'))
    assert config['image_directories'][0]['path'].endswith('参考图')


def test_owned_outputs_only_match_this_task_and_never_shared_files(libs):
    client, project, assets, output = libs
    work = _project(client)
    mine = output / '工作目录' / 'task-a_00001_.png'
    shared = output / '工作目录' / 'shared.png'
    other = output / '工作目录' / 'task-b_00001_.png'
    mine.parent.mkdir(parents=True, exist_ok=True)
    for path in (mine, shared, other):
        path.write_bytes(b'x')
    _insert_task(work, 'task-a', {'result': {'output': str(mine)}, 'extra': str(shared)})
    _insert_task(work, 'task-b', {'result': {'output': str(other)}})
    rows = library_archive.task_rows(task_ids=['task-a'])
    found = library_archive.owned_outputs(rows)
    assert found == [str(mine.resolve())]


def test_project_delete_drops_outputs_and_keeps_what_you_archive(libs):
    client, project, assets, output = libs
    work = _project(client)
    kept = output / f'关键帧/{"a" * 8}-task-keep_00001_.png'
    doom = output / f'关键帧/{"b" * 8}-task-drop_00001_.png'
    kept.parent.mkdir(parents=True, exist_ok=True)
    for path in (kept, doom):
        path.write_bytes(b'x')
    _register(work, 'keep', 'image', kept, title='留下的参考')
    _register(work, 'drop', 'image', doom, title='一起删的参考')
    _insert_task(work, 'task-keep', {'result': {'output': str(kept)}})
    _insert_task(work, 'task-drop', {'result': {'output': str(doom)}})

    plan = client.post('/api/projects/deletion-plan', json={'project_ids': [work]}).json()
    entry = plan['entries'][0]
    assert {item['asset_id'] for item in entry['archivable']} == {'keep', 'drop'}
    assert set(entry['outputs']) == {str(kept), str(doom)}
    applied = client.post('/api/projects/deletion', json={
        'plan_id': plan['plan_id'], 'project_ids': [work], 'confirm': True,
        'archive_asset_ids': ['keep']})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body['deleted'] == [work]
    assert {item['asset_id'] for item in body['archived']} == {'keep'}
    # 归档是移动：原位置不再留副本，库里那份才是留下的。
    assert not kept.exists() and not doom.exists()
    assert Path(body['archived'][0]['path']).is_file()
    assert not (project / 'workspaces' / work).exists()


def test_task_delete_prunes_candidates_and_can_archive_a_timbre(libs):
    client, project, assets, output = libs
    work = _project(client)
    sample = assets / 'voice.wav'
    sample.write_bytes(b'x')
    clip = output / 'task-voice_00001_.wav'
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b'x')
    _register(work, 'timbre', 'audio', sample, subtitle=TIMBRE, title='沙哑老者',
              owner='task-voice', owner_field='speech_task_id')
    _insert_task(work, 'task-voice', {'result': {'output': str(clip)}}, asset_id='timbre')

    plan = client.post('/api/tasks/deletion-plan', json={'task_ids': ['task-voice']}).json()
    assert {item['asset_id'] for item in plan['archivable']} == {'timbre'}
    assert plan['outputs'] == [str(clip.resolve())]
    applied = client.post('/api/tasks/deletion', json={
        'plan_id': plan['plan_id'], 'task_ids': ['task-voice'], 'confirm': True,
        'archive_asset_ids': ['timbre']})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body['deleted'] == ['task-voice']
    assert body['archived'][0]['library'] == '音色'
    assert not clip.exists() and not sample.exists()
    assert Path(body['archived'][0]['path']).is_file()
    left = [asset['id'] for asset in backend.require_project_manifest(work)['assets']]
    assert 'timbre' not in left


def test_unarchived_task_outputs_are_deleted_and_assets_pruned(libs):
    client, project, assets, output = libs
    work = _project(client)
    image = output / 'task-img_00001_.png'
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b'x')
    _register(work, 'img', 'image', image)
    _insert_task(work, 'task-img', {'result': {'output': str(image)}}, asset_id='img')
    plan = client.post('/api/tasks/deletion-plan', json={'task_ids': ['task-img']}).json()
    applied = client.post('/api/tasks/deletion', json={
        'plan_id': plan['plan_id'], 'task_ids': ['task-img'], 'confirm': True}).json()
    assert applied['archived'] == []
    assert backend.require_project_manifest(work)['assets'] == []
    assert not image.exists()


def test_film_and_music_are_archivable_with_date_and_title(libs, monkeypatch):
    client, project, assets, output = libs
    work = _project(client)
    clip = output / 'task-film_00001_.mp4'
    song = output / 'task-song_00001_.mp3'
    clip.parent.mkdir(parents=True, exist_ok=True)
    for path in (clip, song):
        path.write_bytes(b'x')
    _register(work, 'film', 'video', clip, title='最终成片')
    _register(work, 'song', 'audio', song, subtitle='配乐候选 · 待审核', title='悬疑铺底')
    items = {item['target']: item for item in library_archive.candidates(work)}
    assert set(items) == {'film', 'music'}
    done = {item['target']: item for item in library_archive.archive(list(items.values()))}
    film, music = Path(done['film']['path']), Path(done['music']['path'])
    assert film.is_file() and film.parent.parent.name == '作品'
    assert film.parent.name.endswith('归档测试') and film.name[:4].isdigit()
    assert music.is_file() and music.parent.name == '配乐' and music.stem.split('_')[0].count('-') == 2
    assert not clip.exists() and not song.exists()


def test_archived_files_survive_work_deletion(libs):
    client, project, assets, output = libs
    work = _project(client)
    clip = output / 'task-film_00001_.mp4'
    song = output / 'task-song_00001_.mp3'
    clip.parent.mkdir(parents=True, exist_ok=True)
    for path in (clip, song):
        path.write_bytes(b'x')
    _register(work, 'film', 'video', clip, title='最终成片')
    _register(work, 'song', 'audio', song, subtitle='配乐候选 · 待审核', title='悬疑铺底')
    _insert_task(work, 'task-film', {'result': {'output': str(clip)}}, asset_id='film')
    plan = client.post('/api/projects/deletion-plan', json={'project_ids': [work]}).json()
    applied = client.post('/api/projects/deletion', json={
        'plan_id': plan['plan_id'], 'project_ids': [work], 'confirm': True,
        'archive_asset_ids': ['film', 'song']}).json()
    paths = [Path(item['path']) for item in applied['archived']]
    assert len(paths) == 2 and all(path.is_file() for path in paths)
    # 归档区不在删除范围：作品目录没了，归档文件还在。
    assert not (project / 'workspaces' / work).exists()
    assert all(str(path).startswith(str(assets.parent)) for path in paths)


def test_delete_files_never_touches_the_archive_zones(libs):
    client, project, assets, output = libs
    a = assets / '参考图' / 'keep.png'
    b = assets.parent / '作品' / '2026-10-25_某作' / 'keep.mp4'
    for path in (a, b):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    doomed = output / 'task-x_00001_.png'
    doomed.parent.mkdir(parents=True, exist_ok=True)
    doomed.write_bytes(b'x')
    removed = library_archive.delete_files([str(a), str(b), str(doomed)])
    assert removed == [str(doomed.resolve())]
    assert a.exists() and b.exists()
