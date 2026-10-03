import json
from pathlib import Path

import pytest
from backend import atomic_files, app as backend


@pytest.mark.parametrize('winerror',[5,32])
def test_transient_replace_keeps_prepared_revision_and_only_retries_same_bytes(tmp_path,monkeypatch,winerror):
    path=tmp_path/'plan.json';path.write_text('{"revision":4}',encoding='utf-8')
    real=Path.replace;attempts=[]
    def busy(self,target):
        attempts.append((self,target,self.read_bytes()))
        if len(attempts)<3:
            error=PermissionError('temporary Windows sharing failure');error.winerror=winerror;raise error
        return real(self,target)
    monkeypatch.setattr(Path,'replace',busy)
    monkeypatch.setattr(atomic_files.time,'sleep',lambda duration:None)
    backend.write_plan_version(path,{'revision':4,'segments':[]})
    assert len(attempts)==3 and attempts[0]==attempts[1]==attempts[2]
    assert json.loads(path.read_text(encoding='utf-8'))['revision']==5


@pytest.mark.parametrize('winerror,attempts',[(5,4),(32,4),(None,1),(87,1)])
def test_persistent_or_other_permission_failure_stays_explicit_and_original_intact(tmp_path,monkeypatch,winerror,attempts):
    source=tmp_path/'prepared.tmp';destination=tmp_path/'original.json'
    source.write_bytes(b'new');destination.write_bytes(b'old');calls=[]
    def fail(self,target):
        calls.append((self,target));error=PermissionError('denied');error.winerror=winerror;raise error
    monkeypatch.setattr(Path,'replace',fail)
    monkeypatch.setattr(atomic_files.time,'sleep',lambda duration:None)
    with pytest.raises(PermissionError): atomic_files.replace_prepared(source,destination)
    assert len(calls)==attempts and destination.read_bytes()==b'old' and source.read_bytes()==b'new'
