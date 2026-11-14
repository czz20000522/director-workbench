"""媒体文件地址：历史结果里的相对输出路径也要能打开。

同一份生成记录里，有的产物存绝对路径，有的存「导演工作台工作目录/…」这种相对路径；
后者按共享根解析会落到错误位置，页面上就是一块空白。这里锁住兜底规则。
"""
import base64

from fastapi.testclient import TestClient

from backend import app as backend

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


def test_media_file_resolves_relative_output_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'OUTPUT_ROOT', tmp_path)
    target = tmp_path / '导演工作台工作目录' / 'quick-tasks' / '关键帧' / 'shot.png'
    target.parent.mkdir(parents=True)
    target.write_bytes(PNG)
    client = TestClient(backend.app)

    response = client.get('/media-file', params={'path': '导演工作台工作目录/quick-tasks/关键帧/shot.png'})
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('image/')

    # 反斜杠形式同样认（后端结果里有两种写法）。
    backslash = client.get('/media-file', params={'path': '导演工作台工作目录\\quick-tasks\\关键帧\\shot.png'})
    assert backslash.status_code == 200


def test_media_file_still_refuses_to_escape_the_output_root(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, 'OUTPUT_ROOT', tmp_path)
    outside = tmp_path.parent / 'escape.png'
    outside.write_bytes(PNG)
    client = TestClient(backend.app)
    response = client.get('/media-file', params={'path': '../escape.png'})
    assert response.status_code == 403
