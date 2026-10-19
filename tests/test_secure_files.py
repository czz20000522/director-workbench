import pytest

from backend.secure_files import checked_directory, checked_file, open_checked, verify_directory_identity, verify_identity
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
