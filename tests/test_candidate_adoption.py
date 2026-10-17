import json
import time
from urllib.parse import quote

import pytest

from backend import app as backend
from test_production_presets import preset_client
from test_private_workbench import private_workbench, create

BASE='/api/projects/preset-test'


def candidate_fixture(client, project, *, stage='sample', kind=None):
    asset='MASTER' if stage=='final' else 'S01'
    if not kind and stage!='final':
        assert client.post(BASE+'/plan/segments',json={'segment_id':asset,'duration_seconds':5}).status_code==200
    manifest=backend.load_project_manifest('preset-test')
    document,path=backend.load_project_plan('preset-test')
    media=project/'workspaces/preset-test/assets/candidate.bin'
    media.write_bytes(b'isolated successful media fixture')
    task_id='task-current'
    if kind:
        asset={'keyframe':'keyframe-','speech':'speech-','audio_edit':'audio-edit-'}[kind]+task_id
        field={'keyframe':'keyframe_task_id','speech':'speech_task_id','audio_edit':'audio_edit_task_id'}[kind]
        manifest['assets']=[{'id':asset,'kind':'image' if kind=='keyframe' else 'audio','ready':True,field:task_id,'sources':{'A':str(media)}}]
        backend.save_project_manifest(manifest)
    elif stage=='final':
        document['assembly']={'status':'candidate_generated','output':str(media),'prompt_id':task_id}
    else:
        document['segments'][0].update(status='video_generated',comfyui_task_id=task_id,video={'path':str(media)})
    path.write_text(json.dumps(document),encoding='utf-8')
    payload={'project_id':'preset-test','kind':kind or 'workflow','pipeline_stage_id':'picture-finish' if stage=='finish' else 'motion-generation'}
    if stage == 'final':
        payload={'project_id':'preset-test','asset_id':asset,'assembly_asset_id':asset,'source_plan':str(path),'plan_path':str(path),'workflow':'isolated-assembly.json'}
    with backend.db() as db:
        db.execute('INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,result,project_id) VALUES (?,?,?,?,?,?,?,?)',(task_id,asset,'succeeded',time.time(),time.time(),json.dumps(payload),json.dumps({'status':'success','output':str(media)}),'preset-test'));db.commit()
    return {'asset_id':asset,'stage':stage,'candidate_ref':'/media-file?path='+quote(str(media),safe=''),'expected_review_revision':0,'expected_plan_revision':document.get('revision',0),'note':'Explicit delegated technical selection, not a human viewing claim.','confirm_adoption':True},path,media


@pytest.mark.parametrize('stage,kind',[('sample',None),('finish',None),('final',None),('sample','keyframe'),('sample','speech'),('sample','audio_edit')])
def test_current_successful_candidate_adoption_and_original_revision_retry(preset_client,stage,kind):
    client,project=preset_client
    body,path,media=candidate_fixture(client,project,stage=stage,kind=kind)
    if stage == 'finish':
        backend.persist_review(backend.ReviewRecordRequest(asset_id='S01',stage='sample',status='approved',note='Previously selected sample',candidate_ref=str(media),expected_plan_revision=body['expected_plan_revision']), 'preset-test')
    before=path.read_bytes()
    response=client.post(BASE+'/adoptions',json=body)
    assert response.status_code==200,response.text
    result=response.json();assert result['status']=='approved' and result['revision']==1
    assert backend.adoption_candidate_path(result['candidate_ref'])==media
    records=backend.project_records('preset-test')
    assert client.post(BASE+'/adoptions',json=body).status_code==409
    assert backend.project_records('preset-test')==records
    assert path.read_bytes()==before


@pytest.mark.parametrize('change',[
 {'confirm_adoption':False},{'confirm_adoption':1},{'confirm_adoption':'true'},
 {'candidate_ref':' '},{'note':' '},{'expected_plan_revision':True},
 {'expected_review_revision':0.0},{'expected_plan_revision':-1},{'source':'forged'},
])
def test_strict_adoption_body_rejects_without_review(preset_client,change):
    client,project=preset_client;body,_,_=candidate_fixture(client,project);body.update(change)
    before=backend.project_records('preset-test');assert client.post(BASE+'/adoptions',json=body).status_code==422
    assert backend.project_records('preset-test')==before


@pytest.mark.parametrize('failure',['missing_confirm','plan_revision','review_revision','wrong_asset','wrong_stage','wrong_path','missing_file','unsuccessful_task','foreign_task','wrong_receipt'])
def test_invalid_candidate_does_not_write_review(preset_client,failure):
    client,project=preset_client;body,_,media=candidate_fixture(client,project)
    if failure=='missing_confirm':body.pop('confirm_adoption')
    elif failure=='plan_revision':body['expected_plan_revision']+=1
    elif failure=='review_revision':body['expected_review_revision']=1
    elif failure=='wrong_asset':body['asset_id']='another'
    elif failure=='wrong_stage':body['stage']='finish'
    elif failure=='wrong_path':
        other=media.with_name('old.bin');other.write_bytes(b'old candidate');body['candidate_ref']=str(other)
    elif failure=='missing_file':media.rename(media.with_name('retained.bin'))
    else:
        with backend.db() as db:
            if failure=='unsuccessful_task':db.execute("UPDATE tasks SET status='failed' WHERE id='task-current'")
            elif failure=='foreign_task':db.execute("UPDATE tasks SET project_id='other' WHERE id='task-current'")
            else:db.execute("UPDATE tasks SET result=? WHERE id='task-current'",(json.dumps({'status':'success','output':str(media.with_name('old.bin'))}),))
            db.commit()
    before=backend.project_records('preset-test');assert client.post(BASE+'/adoptions',json=body).status_code in (409,422)
    assert backend.project_records('preset-test')==before


def test_final_stale_rejected_and_imported_material_without_success_task_rejected(preset_client):
    client,project=preset_client;body,path,_=candidate_fixture(client,project,stage='final')
    plan=json.loads(path.read_text());plan['assembly']['status']='stale';path.write_text(json.dumps(plan))
    assert client.post(BASE+'/adoptions',json=body).status_code==409
    manifest=backend.load_project_manifest('preset-test');manifest['assets']=[{'id':'imported','ready':True,'image_path':body['candidate_ref']}];backend.save_project_manifest(manifest)
    body.update(asset_id='imported',stage='sample')
    assert client.post(BASE+'/adoptions',json=body).status_code==409


def test_private_cross_user_adoption_blocked_before_candidate_lookup(private_workbench):
    a,b,_,_=private_workbench;project=create(a)
    body={'asset_id':'S01','stage':'sample','candidate_ref':'anything','expected_review_revision':0,'expected_plan_revision':0,'note':'test','confirm_adoption':True}
    assert b.post('/api/projects/'+project['id']+'/adoptions',json=body).status_code==404


@pytest.mark.parametrize('alternate,stage', [('rework_candidate','sample'),('finish_review','finish')])
def test_visible_b_candidate_uses_its_task_and_variant(preset_client,alternate,stage):
    client,project=preset_client
    body,path,media=candidate_fixture(client,project,stage=stage)
    document=json.loads(path.read_text())
    video=document['segments'][0]['video']
    video['path']=str(media.with_name('original.bin'))
    media.with_name('original.bin').write_bytes(b'isolated original sample')
    video[alternate]={'path':str(media),'prompt_id':'task-current'}
    document['segments'][0]['comfyui_task_id']='original-task'
    path.write_text(json.dumps(document))
    if stage=='finish':
        assert client.post(BASE+'/adoptions',json=body).status_code==409
        backend.persist_review(backend.ReviewRecordRequest(asset_id='S01',stage='sample',status='approved',note='Previously selected sample',candidate_ref=video['path'],expected_plan_revision=body['expected_plan_revision']), 'preset-test')
    response=client.post(BASE+'/adoptions',json=body)
    assert response.status_code==200,response.text
    assert response.json()['adopted_variant']=='B'
    assert response.json()['candidate_ref']==body['candidate_ref']
    assert backend.adoption_candidate_path(response.json()['candidate_ref'])==media


def test_hidden_finish_candidate_cannot_bypass_visible_rework_or_task_stage(preset_client):
    client,project=preset_client
    body,path,media=candidate_fixture(client,project,stage='finish')
    backend.persist_review(backend.ReviewRecordRequest(asset_id='S01',stage='sample',status='approved',note='Previously selected sample'), 'preset-test')
    document=json.loads(path.read_text())
    document['segments'][0]['video']={'path':str(media.with_name('original.bin')), 'finish_review':{'path':str(media),'prompt_id':'task-current'}, 'rework_candidate':{'path':str(media.with_name('newer.bin')),'prompt_id':'newer'}}
    path.write_text(json.dumps(document))
    assert client.post(BASE+'/adoptions',json=body).status_code==409
    document['segments'][0]['video']['rework_candidate']={'path':str(media),'prompt_id':'task-current'}
    path.write_text(json.dumps(document))
    body['stage']='sample';body['expected_review_revision']=1
    assert client.post(BASE+'/adoptions',json=body).status_code==409


@pytest.mark.parametrize('change',[{'kind':'speech'},{'pipeline_stage_id':'motion-generation'},{'assembly_asset_id':'other'},{'source_plan':None}])
def test_final_requires_assembly_task_contract(preset_client,change):
    client,project=preset_client
    body,_,_=candidate_fixture(client,project,stage='final')
    with backend.db() as db:
        row=db.execute("SELECT payload FROM tasks WHERE id='task-current'").fetchone()
        payload=json.loads(row['payload']);payload.update(change)
        db.execute("UPDATE tasks SET payload=? WHERE id='task-current'",(json.dumps(payload),));db.commit()
    before=backend.project_records('preset-test')
    assert client.post(BASE+'/adoptions',json=body).status_code==409
    assert backend.project_records('preset-test')==before


def test_private_frozen_task_owner_mismatch_cannot_adopt(private_workbench):
    from backend.user_context import UserContext
    a,_,ownership,_=private_workbench
    project=create(a);pid=project['id'];base='/api/projects/'+pid
    assert a.post(base+'/plan/segments',json={'segment_id':'S01','duration_seconds':5}).status_code==200
    workspace=UserContext('user001',ownership.layout).project(pid)
    path=workspace/'plans/plan.json';document=json.loads(path.read_text(encoding='utf-8'))
    media=workspace/'assets/candidate.bin';media.write_bytes(b'private candidate')
    document['segments'][0].update(status='video_generated',comfyui_task_id='bad-owner',video={'path':str(media)})
    path.write_text(json.dumps(document))
    payload={'project_id':pid,'owner_user':'user002','pipeline_stage_id':'motion-generation'}
    with backend.db() as db:
        db.execute('INSERT INTO tasks (id,asset_id,status,created_at,updated_at,payload,result,project_id) VALUES (?,?,?,?,?,?,?,?)',('bad-owner','S01','succeeded',time.time(),time.time(),json.dumps(payload),json.dumps({'status':'success','output':str(media)}),pid));db.commit()
    body={'asset_id':'S01','stage':'sample','candidate_ref':str(media),'expected_plan_revision':document['revision'],'expected_review_revision':0,'note':'explicit selection','confirm_adoption':True}
    before=backend.project_records(pid)
    response=a.post(base+'/adoptions',json=body)
    assert response.status_code==409,response.text
    assert '冻结归属' in response.text
    assert backend.project_records(pid)==before
