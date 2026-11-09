"""把文件送进 Windows 回收站，不引入第三方依赖。

工作台以前用 ``Path.unlink()`` 做单步任务删除；一旦确认就没法捞回来。
这里改成 shell32 的 ``SHFileOperationW(FOF_ALLOWUNDO)``，语义和用户预期一致。

两条安全约定：

* **不静默回退**。回收站不可用时返回失败列表，调用方保留原文件；
  绝不能「以为进了回收站、其实被永久删了」。
* **只搬文件**。空目录由调用方自行 rmdir，不值得占用回收站条目。

关于重试：Windows 上刚写完的文件常被 Defender、搜索索引或生成进程短暂持有句柄，
第一次 SHFileOperation 会整批报 0x20（共享冲突）；等一两百毫秒再试通常就过了。
所以这里对「仍在原位」的那部分最多重试 3 次，每次只重试没成功的。
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Iterable

log = logging.getLogger('uvicorn.error')

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_FLAGS = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI

# 最近一次失败的 Shell 返回码：0x20 = 共享冲突（文件被占用），0x2 = 文件不存在。
LAST_ERROR = 0


class _SHFILEOPSTRUCTW(ctypes.Structure):
    # 64 位 Windows 上 fFlags 是 WORD，后面由 ctypes 按 C 规则补 padding。
    _fields_ = [
        ('hwnd', wintypes.HWND),
        ('wFunc', wintypes.UINT),
        ('pFrom', wintypes.LPCWSTR),
        ('pTo', wintypes.LPCWSTR),
        ('fFlags', ctypes.c_uint16),
        ('fAnyOperationsAborted', wintypes.BOOL),
        ('hNameMappings', ctypes.c_void_p),
        ('lpszProgressTitle', wintypes.LPCWSTR),
    ]


def available() -> bool:
    return os.name == 'nt'


def describe(code: int) -> str:
    """把 Shell 错误码翻成一句人话，写进日志比裸数字有用。"""
    return {
        0: '成功',
        0x2: '文件不存在',
        0x3: '路径不存在',
        0x20: '文件正被占用（共享冲突）',
        0x21: '文件被锁定',
        0x5: '拒绝访问',
        0x7C: '路径不存在',
        0x402: '源文件被占用或不可用',
    }.get(code, f'Shell 返回 {code:#x}')


def _shell_recycle(paths: list[Path]) -> tuple[int, bool]:
    """调一次 SHFileOperationW，返回 (返回码, 是否被中止)。"""
    # pFrom 是以 NUL 分隔、双 NUL 结尾的宽字符串列表。
    source = '\0'.join(str(path.resolve()) for path in paths) + '\0\0'
    operation = _SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = source
    operation.pTo = None
    operation.fFlags = FOF_FLAGS
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None

    initialized = False
    try:
        # FastAPI 的请求跑在线程池线程里，COM 未必初始化过；SHFileOperation 内部
        # 会用到 shell 的 COM 对象，先 CoInitialize 一下更稳（S_FALSE 也算成功）。
        try:
            initialized = ctypes.windll.ole32.CoInitializeEx(None, 0x2) in (0, 1)
        except (AttributeError, OSError):
            initialized = False
        shell32 = ctypes.WinDLL('shell32', use_last_error=True)
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        code = shell32.SHFileOperationW(ctypes.byref(operation))
    except (AttributeError, OSError):
        return 0x402, False
    finally:
        if initialized:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except (AttributeError, OSError):
                pass
    return code, bool(operation.fAnyOperationsAborted)


def recycle(paths: Iterable[Path], *, attempts: int = 5) -> tuple[list[str], list[str]]:
    """返回 ``(已移入回收站, 未能移入)``。失败的文件保持原位。

    对仍在原位的文件最多重试 ``attempts`` 次：刚生成的产物、刚写完的快照 json
    经常被 Defender / 索引器占着几百毫秒，一次失败不代表删不掉。
    """
    global LAST_ERROR
    existing = [path for path in paths if path.is_file()]
    if not existing:
        return [], []
    if not available():
        return [], [str(path) for path in existing]

    remaining = existing
    code = 0
    for index in range(max(1, attempts)):
        code, aborted = _shell_recycle(remaining)
        still = [path for path in remaining if path.exists()]
        if not still:
            moved = [str(path) for path in existing if not path.exists()]
            if index:
                log.info('回收站第 %s 次尝试成功（%s 个文件）', index + 1, len(moved))
            return moved, []
        if aborted:
            break
        remaining = still
        if index + 1 < attempts:
            # 0.4 / 0.8 / 1.6 / 3.2 秒：刚出完图时 Defender、索引器和浏览器读取
            # 都会短暂占着文件，只等 0.25 秒不够（实测）。
            time.sleep(0.4 * (2 ** index))

    LAST_ERROR = code
    moved = [str(path) for path in existing if not path.exists()]
    failed = [str(path) for path in existing if path.exists()]
    if failed:
        log.warning('回收站未能处理 %s 个文件：%s（%s）',
                    len(failed), describe(code), '; '.join(Path(p).name for p in failed[:4]))
    return moved, failed
