import pytest

from backend.secure_files import (
    VerifiedFileResponse,
    checked_directory,
    checked_file,
    open_checked,
    verify_directory_identity,
    verify_identity,
)
from backend.user_context import AccessDenied


def test_open_checked_rejects_a_replaced_file(tmp_path):
    path = tmp_path / 'asset.bin'
    path.write_bytes(b'old')
    _, identity = checked_file(path)
    path.write_bytes(b'new-content')
    with pytest.raises(AccessDenied):
        open_checked(path, identity)


def test_directory_identity_ignores_expected_child_changes(tmp_path):
    folder, identity = checked_directory(tmp_path)
    (folder / 'asset.bin').write_bytes(b'asset')
    verify_directory_identity(folder, identity)


def test_file_identity_rejects_metadata_change(tmp_path):
    path = tmp_path / 'asset.bin'
    path.write_bytes(b'asset')
    _, identity = checked_file(path)
    path.write_bytes(b'changed')
    with pytest.raises(AccessDenied):
        verify_identity(path, identity)


async def _messages(response: VerifiedFileResponse, method: str = 'GET', range_header: str | None = None):
    messages = []

    async def receive():
        return {'type': 'http.request'}

    async def send(message):
        messages.append(message)

    headers = [] if range_header is None else [(b'range', range_header.encode())]
    scope = {'type': 'http', 'method': method, 'headers': headers, 'extensions': {}}
    await response(scope, receive, send)
    return messages


@pytest.mark.anyio
async def test_verified_response_head_does_not_open_file(tmp_path, monkeypatch):
    path = tmp_path / 'large.bin'
    path.write_bytes(b'x' * 128)
    candidate, identity = checked_file(path)
    response = VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat())

    async def fail_open():
        raise AssertionError('HEAD must not open the file')

    monkeypatch.setattr(response, '_open', fail_open)
    messages = await _messages(response, method='HEAD')
    assert messages[0]['type'] == 'http.response.start'
    assert dict(messages[0]['headers'])[b'content-length'] == b'128'
    assert messages[-1] == {'type': 'http.response.body', 'body': b'', 'more_body': False}


@pytest.mark.anyio
async def test_verified_response_multiple_ranges_streams_each_chunk(tmp_path):
    path = tmp_path / 'range.bin'
    path.write_bytes(bytes(range(64)))
    candidate, identity = checked_file(path)
    response = VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat())
    response.chunk_size = 4
    messages = await _messages(response, range_header='bytes=0-9,20-29')

    assert messages[0]['type'] == 'http.response.start'
    assert messages[0]['status'] == 206
    body_messages = [message['body'] for message in messages[1:] if message['type'] == 'http.response.body']
    assert len(body_messages) > 6
    assert bytes(range(4)) in body_messages
    assert bytes(range(20, 24)) in body_messages
    assert max(len(body) for body in body_messages[:-1]) <= 4 or any(b'Content-Range' in body for body in body_messages[:-1])
    assert body_messages[-1].startswith(b'--director-')


@pytest.mark.anyio
async def test_verified_response_rejects_excessive_ranges(tmp_path):
    path = tmp_path / 'range.bin'
    path.write_bytes(b'x' * 64)
    candidate, identity = checked_file(path)
    response = VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat())
    messages = await _messages(response, range_header='bytes=0-0,2-2,4-4,6-6,8-8,10-10,12-12,14-14,16-16')
    assert messages[0]['type'] == 'http.response.start'
    assert messages[0]['status'] == 416


@pytest.mark.anyio
async def test_verified_response_rejects_excessive_total_range_bytes(tmp_path, monkeypatch):
    path = tmp_path / 'range.bin'
    path.write_bytes(b'x' * 64)
    candidate, identity = checked_file(path)
    response = VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat())
    monkeypatch.setattr(VerifiedFileResponse, 'max_total_range_bytes', 8)
    messages = await _messages(response, range_header='bytes=0-9,20-29')
    assert messages[0]['type'] == 'http.response.start'
    assert messages[0]['status'] == 416
