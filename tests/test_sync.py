"""Tests for the sync extraction helpers (initial clone)."""

import io
import tarfile
import zipfile

import pytest

from cinna.sync import ensure_workspace_dirs, extract_workspace_tarball


def test_extract_workspace_tarball(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('hello')"
        info = tarfile.TarInfo(name="hello.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tarball = buf.getvalue()

    extracted = extract_workspace_tarball(tarball, tmp_path)
    assert "hello.py" in extracted
    assert (tmp_path / "hello.py").read_bytes() == b"print('hello')"


def test_extract_rejects_path_traversal(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="../../../etc/passwd")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    tarball = buf.getvalue()

    extracted = extract_workspace_tarball(tarball, tmp_path)
    assert len(extracted) == 0
    assert not (tmp_path / "../../../etc/passwd").exists()


def test_extract_workspace_zip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.py", "print('hello')")
        zf.writestr("subdir/data.txt", "some data")
    archive = buf.getvalue()

    extracted = extract_workspace_tarball(archive, tmp_path)
    assert "hello.py" in extracted
    assert "subdir/data.txt" in extracted
    assert (tmp_path / "hello.py").read_text() == "print('hello')"
    assert (tmp_path / "subdir" / "data.txt").read_text() == "some data"


def test_extract_zip_rejects_path_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../../etc/passwd", "evil")
    archive = buf.getvalue()

    extracted = extract_workspace_tarball(archive, tmp_path)
    assert len(extracted) == 0


def test_extract_tar_only_files(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ["a.py", "b.py", "c.py"]:
            data = name.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    tarball = buf.getvalue()

    extracted = extract_workspace_tarball(tarball, tmp_path, only_files={"a.py", "c.py"})
    assert sorted(extracted) == ["a.py", "c.py"]
    assert (tmp_path / "a.py").exists()
    assert not (tmp_path / "b.py").exists()
    assert (tmp_path / "c.py").exists()


def test_ensure_workspace_dirs(tmp_path):
    ensure_workspace_dirs(tmp_path)
    assert (tmp_path / "files").is_dir()
    assert (tmp_path / "knowledge").is_dir()
    assert (tmp_path / "app-data" / "storage").is_dir()
    assert (tmp_path / "app-data" / "uploads").is_dir()
    assert (tmp_path / "app-data" / "cache").is_dir()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_extract_tar_without_extract_filter_support(tmp_path, monkeypatch):
    """Python < 3.10.12 / 3.11.4 has no ``extract(filter=...)`` — the fallback
    path must still extract instead of raising TypeError."""
    from cinna import sync

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"print('hello')"
        info = tarfile.TarInfo(name="hello.py")
        info.size = len(data)
        info.mode = 0o4755  # setuid — must not survive extraction
        tar.addfile(info, io.BytesIO(data))
    tarball = buf.getvalue()

    monkeypatch.setattr(sync, "_HAS_EXTRACT_FILTER", False)
    extracted = extract_workspace_tarball(tarball, tmp_path)

    assert "hello.py" in extracted
    assert (tmp_path / "hello.py").read_bytes() == b"print('hello')"
    assert not (tmp_path / "hello.py").stat().st_mode & 0o4000


def test_extract_tar_skips_special_files(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="pipe")
        info.type = tarfile.FIFOTYPE
        tar.addfile(info)
        data = b"ok"
        good = tarfile.TarInfo(name="good.txt")
        good.size = len(data)
        tar.addfile(good, io.BytesIO(data))
    tarball = buf.getvalue()

    extracted = extract_workspace_tarball(tarball, tmp_path)
    assert extracted == ["good.txt"]
    assert not (tmp_path / "pipe").exists()
