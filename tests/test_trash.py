"""回收站：真的把文件送进 Windows 回收站，失败时保持原位。

重试这一段是 11-09 补的：刚写完的产物/快照会被 Defender、索引器或生成进程
短暂占用，一次 SHFileOperation 报 0x20 就把整批判失败，用户会看到
「2 个文件未能移入」。这里把「第一次被占用、第二次成功」固定成用例。
"""
import os

import pytest

from backend import trash

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='只测 Windows 回收站')


def test_recycle_ignores_missing_paths(tmp_path):
    assert trash.recycle([tmp_path / 'nope.txt']) == ([], [])


def test_recycle_moves_file_to_bin(tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('x', encoding='utf-8')
    moved, failed = trash.recycle([target])
    assert moved == [str(target)]
    assert failed == []
    assert not target.exists()


def test_recycle_retries_after_transient_share_violation(tmp_path, monkeypatch):
    target = tmp_path / 'b.txt'
    target.write_text('x', encoding='utf-8')
    monkeypatch.setattr(trash.time, 'sleep', lambda seconds: None)
    calls = {'count': 0}
    real = trash._shell_recycle

    def flaky(paths):
        calls['count'] += 1
        if calls['count'] == 1:
            return 0x20, False  # 第一次装作被占用：文件一个都不动
        return real(paths)

    monkeypatch.setattr(trash, '_shell_recycle', flaky)
    moved, failed = trash.recycle([target], attempts=3)
    assert failed == []
    assert not target.exists()
    assert calls['count'] >= 2


def test_recycle_keeps_file_when_it_really_cannot_move(tmp_path, monkeypatch):
    target = tmp_path / 'c.txt'
    target.write_text('x', encoding='utf-8')
    monkeypatch.setattr(trash.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(trash, '_shell_recycle', lambda paths: (0x20, False))
    moved, failed = trash.recycle([target], attempts=2)
    assert moved == []
    assert failed == [str(target)]
    assert target.exists()
    assert trash.LAST_ERROR == 0x20
    assert '占用' in trash.describe(trash.LAST_ERROR)


def test_describe_explains_common_codes():
    assert '占用' in trash.describe(0x20)
    assert '拒绝访问' in trash.describe(0x5)
    assert trash.describe(0) == '成功'
