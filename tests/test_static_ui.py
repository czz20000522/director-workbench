from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import WorkbenchStaticFiles


def test_entry_revalidates_and_rebuild_selects_new_asset(tmp_path):
    entry = tmp_path / 'index.html'
    entry.write_text('<script src="/assets/old.js"></script>', encoding='utf-8')
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'old.js').write_text('old', encoding='utf-8')
    app = FastAPI()
    app.mount('/', WorkbenchStaticFiles(directory=tmp_path, html=True))
    client = TestClient(app)
    for url in ('/', '/index.html'):
        first = client.get(url)
        assert first.headers['cache-control'] == 'no-cache'
        cached = client.get(url, headers={'If-None-Match': first.headers['etag']})
        assert cached.status_code == 304
        assert cached.headers['cache-control'] == 'no-cache'
    entry.write_text('<script src="/assets/new-build.js"></script>', encoding='utf-8')
    rebuilt = client.get('/', headers={'If-None-Match': first.headers['etag']})
    assert rebuilt.status_code == 200
    assert 'new-build.js' in rebuilt.text
    assert rebuilt.headers['cache-control'] == 'no-cache'
    assert 'cache-control' not in client.get('/assets/old.js').headers
