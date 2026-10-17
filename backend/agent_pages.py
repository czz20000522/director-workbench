"""User/tab-bound presentation receipts, independent of any cloud assistant."""
import hashlib
import json
import secrets
import threading
import time
import uuid
import re
from pathlib import Path

from fastapi import HTTPException
from .atomic_files import replace_prepared


class PageStore:
    def __init__(self,root):
        self.root=Path(root); self.lock=threading.RLock()

    def _path(self,owner): return self.root/(hashlib.sha256(owner.encode()).hexdigest()+'.json')

    def _load(self,owner):
        path=self._path(owner)
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'pages':[]}

    def _save(self,owner,data):
        self._write(self._path(owner),data)

    def _write(self,path,data):
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        replace_prepared(temporary,path)

    def _archive_path(self,owner,pid):
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',pid): raise HTTPException(404,'页面不存在')
        return self.root/self._path(owner).stem/(pid+'.json')

    def _page(self,data,pid,owner=None):
        page=next((row for row in data['pages'] if row['page_id']==pid),None)
        if page is None and owner is not None:
            path=self._archive_path(owner,pid)
            if path.is_file(): page=json.loads(path.read_text(encoding='utf-8'))
        if page is None: raise HTTPException(404,'本人没有对应页面')
        return page

    def _expire(self,page):
        changed=False
        for row in page['actions']:
            if row['status']=='pending' and row['expires_at']<time.time():
                row.update(status='not_presented',message='动作已过期，页面未确认呈现')
                changed=True
        return changed

    def register(self,owner,pid,label,enabled,current_key=None):
        with self.lock:
            data=self._load(owner)
            page=next((row for row in data['pages'] if row['page_id']==pid),None)
            if page is None:
                if len(data['pages'])>=32:
                    inactive=[row for row in data['pages'] if not row['enabled'] or time.time()-row['updated_at']>=90]
                    if not inactive: raise HTTPException(429,'同时活跃页面达到32个，请关闭不需要的引导页')
                    retired=min(inactive,key=lambda row:row['updated_at'])
                    retired['enabled']=False
                    self._expire(retired)
                    for row in retired['actions']:
                        if row['status']=='pending': row.update(status='takeover',message='过期或未允许的页面已退订；不重放')
                    archive=self._archive_path(owner,retired['page_id'])
                    self._write(archive,retired)
                    data['pages'].remove(retired)
                archive=self._archive_path(owner,pid)
                page=json.loads(archive.read_text(encoding='utf-8')) if archive.is_file() else {'page_id':pid,'actions':[],'cursor':0}
                data['pages'].append(page)
            key=page.get('page_key')
            if not key or not current_key or not secrets.compare_digest(key,current_key): key=secrets.token_urlsafe(32)
            page.update(label=label,enabled=enabled,updated_at=time.time(),page_key=key)
            if not enabled:
                for action in page['actions']:
                    if action['status']=='pending': action.update(status='takeover',message='用户接管或页面重新登记；没有重放动作')
            self._save(owner,data)
            return {'page_id':pid,'page_key':page['page_key'],'enabled':enabled,'cursor':page['cursor']}

    def list(self,owner):
        with self.lock:
            return {'pages':[{'page_id':row['page_id'],'label':row['label'],
                              'enabled':row['enabled'] and time.time()-row['updated_at']<90,
                              'cursor':row['cursor']} for row in self._load(owner)['pages']]}

    def read(self,owner,pid,after):
        with self.lock:
            data=self._load(owner);page=self._page(data,pid,owner)
            if self._expire(page): self._save(owner,data)
            rows=[dict(row) for row in page['actions'] if row['seq']>after]
            return {'page_id':pid,'actions':rows,'cursor':page['cursor']}

    def replay(self,owner,pid,action_id,kind,target,values):
        if not pid: return None
        with self.lock:
            data=self._load(owner);page=self._page(data,pid,owner)
            if self._expire(page): self._save(owner,data)
            old=next((row for row in page['actions'] if row['id']==action_id),None)
            if old is not None and any(old[key]!=value for key,value in {'kind':kind,'target':target,'values':values}.items()):
                raise HTTPException(409,'同一页面动作标识不能更换目标或内容')
            return old

    def heartbeat(self,owner,pid,key):
        with self.lock:
            data=self._load(owner); page=self._page(data,pid)
            if not key or not secrets.compare_digest(page['page_key'],key): raise HTTPException(403,'页面绑定凭据无效')
            if time.time()-page['updated_at']>=15:
                page['updated_at']=time.time();self._save(owner,data)

    def create(self,owner,pid,action_id,kind,target,values):
        if not pid:
            return {'id':action_id,'status':'not_presented','message':'没有附属浏览器页面，业务回执不代表页面点击或播放。'}
        with self.lock:
            old=self.replay(owner,pid,action_id,kind,target,values)
            if old is not None: return old
            data=self._load(owner); page=self._page(data,pid,owner)
            scope={'kind':kind,'target':target,'values':values}
            old=next((row for row in page['actions'] if row['id']==action_id),None)
            if old:
                if any(old[key]!=value for key,value in scope.items()): raise HTTPException(409,'同一页面动作标识不能更换目标或内容')
                return old
            if not page['enabled'] or time.time()-page['updated_at']>=90:
                raise HTTPException(409,'目标页没有允许Agent引导或已离线，请用户在该页允许后重试')
            if sum(row['status']=='pending' and row['expires_at']>time.time() for row in page['actions'])>=16:
                raise HTTPException(429,'页面尚有动作待呈现，请等待回执或接管')
            row={'id':action_id,'seq':page['cursor']+1,**scope,'status':'pending','expires_at':time.time()+60}
            page['cursor']=row['seq'];page['actions'].append(row)
            # Keep a bounded receipt archive; no unbounded UI command log.
            page['actions']=page['actions'][-500:]
            self._save(owner,data);return row

    def ack(self,owner,pid,aid,key,status,message):
        with self.lock:
            data=self._load(owner);page=self._page(data,pid,owner)
            if not key or not secrets.compare_digest(key,page['page_key']): raise HTTPException(403,'仅发起标签页可确认实际呈现')
            row=next((row for row in page['actions'] if row['id']==aid),None)
            if row is None: raise HTTPException(404,'页面动作不存在')
            if row['status']!='pending':
                # A manual takeover wins even if an in-flight browser handler
                # reports its earlier result late. Return the real terminal
                # receipt so the client can finish without retrying forever.
                if row['status']=='takeover': return row
                if row['status']!=status or row.get('message','')!=message: raise HTTPException(409,'原呈现回执不能改写')
                return row
            if row['expires_at']<time.time() and status=='presented': raise HTTPException(409,'动作已过期，不能声称已呈现')
            row.update(status=status,message=message,acknowledged_at=time.time(),confirmation='client_report',verification='not_attested')
            if status=='takeover': page['enabled']=False
            self._save(owner,data);return row
