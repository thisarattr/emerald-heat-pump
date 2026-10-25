import pytest

from app.keygen import ensure


def test_ensure_preserves_existing_key(tmp_path):
    path = tmp_path / "keys" / "emerald.key"
    ensure(path)
    original = path.read_bytes()
    ensure(path)
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize("invalid", ["permissions", "contents", "symlink"])
def test_ensure_rejects_unsafe_existing_key_without_replacing_it(tmp_path, invalid):
    path = tmp_path / "emerald.key"
    ensure(path)
    if invalid == "permissions":
        path.chmod(0o644)
    elif invalid == "contents":
        path.chmod(0o600)
        path.write_bytes(b"broken key")
    else:
        original = tmp_path / "original.key"
        path.rename(original)
        path.symlink_to(original)
    original_bytes = path.read_bytes()
    with pytest.raises(ValueError):
        ensure(path)
    assert path.read_bytes() == original_bytes
