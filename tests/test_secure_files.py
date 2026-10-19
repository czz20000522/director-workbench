import pytest

from backend.secure_files import (
    VerifiedFileResponse,
    checked_directory,
    checked_file,
    copy_checked,
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


def test_copy_checked_keeps_existing_destination_when_source_changes(tmp_path, monkeypatch):
    source = tmp_path / 'source.bin'
    destination = tmp_path / 'destination.bin'
    source.write_bytes(b'original')
    destination.write_bytes(b'previous')
    _, identity = checked_file(source)
    from backend import secure_files
    real_verify = secure_files.verify_identity

    def replace_before_final_check(path, expected):
        # A different size is used deliberately: identity compares size, inode and
        # mtime, and a same-size rewrite could land inside the filesystem timestamp
        # granularity and go undetected, making this test flaky by construction.
        source.write_bytes(b'replaced-with-different-size')
        return real_verify(path, expected)

    monkeypatch.setattr(secure_files, 'verify_identity', replace_before_final_check)
    with pytest.raises(AccessDenied):
        copy_checked(source, destination, identity)
    assert destination.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.destination.bin.*.tmp'))


async def _messages(response: VerifiedFileResponse, method: str = 'GET', range_header: str | None = None):
    messages = []

    async def receive():
        return {'type': 'http.request'}

    async def send(message):
        messages.append(message)

    headers = [] if range_header is None else [(b'range', range_header.encode())]
    # Starlette 1.x streams FileResponse in a collapsed task group and watches
    # ``receive()`` for ``http.disconnect`` only when the scope does not announce
    # ASGI response-send/2.4.  This fake server has no connection lifecycle, so a
    # 2.0 scope leaves it busy-looping on the request forever; announcing 2.4
    # takes the direct send path this test is actually about.
    scope = {'type': 'http', 'method': method, 'headers': headers, 'extensions': {},
             'asgi': {'version': '3.0', 'spec_version': '2.4'}}
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
async def test_verified_response_single_range_terminates_after_short_read(tmp_path, monkeypatch):
    path = tmp_path / 'range.bin'
    path.write_bytes(b'x' * 64)
    candidate, identity = checked_file(path)
    response = VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat())
    response.chunk_size = 4
    reads = iter((b'abcd', b''))

    async def short_read(handle, size):
        return next(reads)

    monkeypatch.setattr(response, '_read', short_read)
    messages = await _messages(response, range_header='bytes=0-9')
    assert dict(messages[0]['headers'])[b'connection'] == b'close'
    assert messages[-1] == {'type': 'http.response.body', 'body': b'', 'more_body': False}
    assert [message['body'] for message in messages[1:] if message['type'] == 'http.response.body'] == [b'abcd', b'']


@pytest.mark.anyio
async def test_verified_response_multipart_range_terminates_after_short_read(tmp_path, monkeypatch):
    path = tmp_path / 'range.bin'
    path.write_bytes(b'x' * 64)
    candidate, identity = checked_file(path)
    response = VerifiedFileResponse(candidate, identity=identity, stat_result=candidate.stat())
    response.chunk_size = 4
    reads = iter((b'abcd', b''))

    async def short_read(handle, size):
        return next(reads)

    monkeypatch.setattr(response, '_read', short_read)
    messages = await _messages(response, range_header='bytes=0-9,20-29')
    assert dict(messages[0]['headers'])[b'connection'] == b'close'
    assert messages[-1] == {'type': 'http.response.body', 'body': b'', 'more_body': False}
    body = b''.join(message['body'] for message in messages[1:] if message['type'] == 'http.response.body')
    assert body.endswith(b'abcd')


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
