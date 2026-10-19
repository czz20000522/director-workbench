"""Point-in-time filesystem identity checks for user-controlled paths.

``no_links`` protects the path walk at the instant it is called.  These helpers
add an identity check around an operation so a concurrent replacement is noticed
before a file is opened or a deletion plan is handed to safe-delete.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile

import anyio
from starlette.responses import FileResponse, JSONResponse

from .user_context import AccessDenied, no_links


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    mode: int


@dataclass(frozen=True)
class DirectoryIdentity:
    device: int
    inode: int
    mode: int


def _identity(path: Path) -> FileIdentity:
    metadata = path.stat()
    attributes = int(getattr(metadata, 'st_file_attributes', 0))
    if stat.S_ISLNK(metadata.st_mode) or attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
        raise AccessDenied('Linked files are not permitted')
    return FileIdentity(
        device=int(metadata.st_dev), inode=int(metadata.st_ino), size=int(metadata.st_size),
        modified_ns=int(metadata.st_mtime_ns), mode=int(metadata.st_mode),
    )


def checked_file(path: str | Path) -> tuple[Path, FileIdentity]:
    """Resolve a plain file and capture the identity used for the next operation."""
    candidate = no_links(Path(path))
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    return candidate, _identity(candidate)


def checked_directory(path: str | Path) -> tuple[Path, DirectoryIdentity]:
    candidate = no_links(Path(path))
    if not candidate.is_dir():
        raise FileNotFoundError(candidate)
    metadata = candidate.stat()
    return candidate, DirectoryIdentity(int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_mode))


def verify_identity(path: str | Path, expected: FileIdentity) -> Path:
    candidate = no_links(Path(path))
    if _identity(candidate) != expected:
        raise AccessDenied('文件在操作期间发生变化，请重新读取后重试')
    return candidate


def verify_directory_identity(path: str | Path, expected: DirectoryIdentity) -> Path:
    candidate = no_links(Path(path))
    metadata = candidate.stat()
    current = DirectoryIdentity(int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_mode))
    if current != expected:
        raise AccessDenied('目录在操作期间发生变化，请重新读取后重试')
    return candidate


def open_checked(path: str | Path, expected: FileIdentity | None = None):
    """Open a file and compare its descriptor identity with the path identity."""
    candidate, captured = checked_file(path)
    if expected is not None and captured != expected:
        raise AccessDenied('文件在打开前发生变化，请重新读取后重试')
    handle = candidate.open('rb')
    try:
        descriptor = os.fstat(handle.fileno())
        opened = FileIdentity(
            device=int(descriptor.st_dev), inode=int(descriptor.st_ino), size=int(descriptor.st_size),
            modified_ns=int(descriptor.st_mtime_ns), mode=int(descriptor.st_mode),
        )
        if opened != captured:
            raise AccessDenied('文件在打开期间发生变化，请重新读取后重试')
        return handle, candidate, captured
    except BaseException:
        handle.close()
        raise


def copy_checked(
    source: str | Path,
    destination: str | Path,
    expected: FileIdentity | None = None,
    *,
    chunk_size: int = 1024 * 1024,
) -> tuple[Path, FileIdentity]:
    """Copy bytes from an identity-checked source and recheck it afterwards.

    The source descriptor remains pinned to the file that was checked before
    the copy.  A path replacement during the copy is detected by the final
    identity check, so callers can reject the operation without registering a
    different file under the requested name.
    """
    handle, candidate, captured = open_checked(source, expected)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{destination_path.name}.', suffix='.tmp', dir=destination_path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, 'wb') as output:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                output.write(chunk)
        try:
            verify_identity(candidate, captured)
        except (FileNotFoundError, OSError) as exc:
            raise AccessDenied('文件在复制期间发生变化，请重新读取后重试') from exc
        if temporary_path.stat().st_size != captured.size:
            raise AccessDenied('素材复制结果大小不一致，请重新读取后重试')
        os.replace(temporary_path, destination_path)
        temporary_path = None
        return destination_path, captured
    finally:
        handle.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


class VerifiedFileResponse(FileResponse):
    """FileResponse variant that reads from the identity-checked open handle."""

    # Keep multipart responses bounded even when a client sends a large Range
    # header.  Single ranges remain streamable regardless of their size.
    max_ranges = 8
    max_total_range_bytes = 64 * 1024 * 1024

    def __init__(self, path: str | Path, *, identity: FileIdentity, **kwargs):
        super().__init__(path, **kwargs)
        self._identity = identity

    async def _open(self):
        return await anyio.to_thread.run_sync(open_checked, self.path, self._identity)

    async def _read(self, handle, size: int):
        return await anyio.to_thread.run_sync(handle.read, size)

    async def _seek(self, handle, offset: int):
        return await anyio.to_thread.run_sync(handle.seek, offset)

    async def _close(self, handle):
        await anyio.to_thread.run_sync(handle.close)

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        except AccessDenied:
            return await JSONResponse(
                {'detail': '素材在读取前发生变化，请重新读取后重试'}, status_code=409,
                headers={'Cache-Control': 'no-store'},
            )(scope, receive, send)

    async def _handle_simple(self, send, send_header_only, send_pathsend):
        if send_header_only:
            await send({'type': 'http.response.start', 'status': self.status_code, 'headers': self.raw_headers})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            return
        handle, _, _ = await self._open()
        try:
            await send({'type': 'http.response.start', 'status': self.status_code, 'headers': self.raw_headers})
            while True:
                chunk = await self._read(handle, self.chunk_size)
                more_body = len(chunk) == self.chunk_size
                await send({'type': 'http.response.body', 'body': chunk, 'more_body': more_body})
                if not more_body:
                    break
        finally:
            await self._close(handle)

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        headers = dict(self.raw_headers)
        headers[b'content-range'] = f'bytes {start}-{end - 1}/{file_size}'.encode()
        headers[b'content-length'] = str(end - start).encode()
        headers[b'connection'] = b'close'
        if send_header_only:
            await send({'type': 'http.response.start', 'status': 206, 'headers': list(headers.items())})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            return
        handle, _, _ = await self._open()
        try:
            await send({'type': 'http.response.start', 'status': 206, 'headers': list(headers.items())})
            await self._seek(handle, start)
            remaining = end - start
            while remaining:
                chunk = await self._read(handle, min(self.chunk_size, remaining))
                if not chunk:
                    # The declared range length can no longer be fulfilled.
                    # Explicitly terminate the ASGI body; the close header
                    # prevents a client from waiting for missing bytes.
                    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                    return
                remaining -= len(chunk)
                await send({'type': 'http.response.body', 'body': chunk, 'more_body': bool(remaining)})
        finally:
            await self._close(handle)

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        boundary = 'director-' + os.urandom(13).hex()
        content_type = self.headers['content-type']
        content_length, header_generator = self.generate_multipart(ranges, boundary, file_size, content_type)
        headers = dict(self.raw_headers)
        headers[b'content-type'] = f'multipart/byteranges; boundary={boundary}'.encode()
        headers[b'content-length'] = str(content_length).encode()
        headers[b'connection'] = b'close'
        if send_header_only:
            await send({'type': 'http.response.start', 'status': 206, 'headers': list(headers.items())})
            await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            return

        # Open only after all headers have been calculated, then keep one
        # identity-checked descriptor for the complete streamed response.
        handle, _, _ = await self._open()
        try:
            await send({'type': 'http.response.start', 'status': 206, 'headers': list(headers.items())})
            for start, end in ranges:
                await send({'type': 'http.response.body', 'body': header_generator(start, end), 'more_body': True})
                await self._seek(handle, start)
                remaining = end - start
                while remaining:
                    chunk = await self._read(handle, min(self.chunk_size, remaining))
                    if not chunk:
                        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                        return
                    remaining -= len(chunk)
                    await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
                await send({'type': 'http.response.body', 'body': b'\r\n', 'more_body': True})
            await send({'type': 'http.response.body', 'body': f'--{boundary}--'.encode(), 'more_body': False})
        finally:
            await self._close(handle)

    @classmethod
    def _parse_range_header(cls, http_range: str, file_size: int):
        if http_range.count(',') + 1 > cls.max_ranges:
            from starlette.responses import RangeNotSatisfiable

            raise RangeNotSatisfiable(file_size)
        ranges = super()._parse_range_header(http_range, file_size)
        if len(ranges) > 1 and sum(end - start for start, end in ranges) > cls.max_total_range_bytes:
            from starlette.responses import RangeNotSatisfiable

            raise RangeNotSatisfiable(file_size)
        return ranges
