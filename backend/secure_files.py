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


class VerifiedFileResponse(FileResponse):
    """FileResponse variant that reads from the identity-checked open handle."""

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
        handle, _, _ = await self._open()
        try:
            await send({'type': 'http.response.start', 'status': self.status_code, 'headers': self.raw_headers})
            if send_header_only:
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                return
            while True:
                chunk = await self._read(handle, self.chunk_size)
                more_body = len(chunk) == self.chunk_size
                await send({'type': 'http.response.body', 'body': chunk, 'more_body': more_body})
                if not more_body:
                    break
        finally:
            await self._close(handle)

    async def _handle_single_range(self, send, start, end, file_size, send_header_only):
        handle, _, _ = await self._open()
        try:
            headers = dict(self.raw_headers)
            headers[b'content-range'] = f'bytes {start}-{end - 1}/{file_size}'.encode()
            headers[b'content-length'] = str(end - start).encode()
            await send({'type': 'http.response.start', 'status': 206, 'headers': list(headers.items())})
            if send_header_only:
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                return
            await self._seek(handle, start)
            remaining = end - start
            while remaining:
                chunk = await self._read(handle, min(self.chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                await send({'type': 'http.response.body', 'body': chunk, 'more_body': bool(remaining)})
        finally:
            await self._close(handle)

    async def _handle_multiple_ranges(self, send, ranges, file_size, send_header_only):
        # Multiple ranges are uncommon for the workbench UI; retain Starlette's
        # framing while reading all bytes from one identity-checked descriptor.
        handle, _, _ = await self._open()
        try:
            boundary = 'director-' + os.urandom(13).hex()
            content_type = self.headers['content-type']
            parts = []
            for start, end in ranges:
                await self._seek(handle, start)
                body = bytearray()
                while len(body) < end - start:
                    chunk = await self._read(handle, min(self.chunk_size, end - start - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                parts.append(
                    f'--{boundary}\r\nContent-Type: {content_type}\r\n'
                    f'Content-Range: bytes {start}-{end - 1}/{file_size}\r\n\r\n'.encode()
                    + bytes(body) + b'\r\n'
                )
            payload = b''.join(parts) + f'--{boundary}--'.encode()
            headers = dict(self.raw_headers)
            headers[b'content-type'] = f'multipart/byteranges; boundary={boundary}'.encode()
            headers[b'content-length'] = str(len(payload)).encode()
            await send({'type': 'http.response.start', 'status': 206, 'headers': list(headers.items())})
            await send({'type': 'http.response.body', 'body': b'' if send_header_only else payload, 'more_body': False})
        finally:
            await self._close(handle)
