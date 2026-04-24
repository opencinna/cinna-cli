"""Tests for the sync extraction helpers (initial clone)."""

import io
import tarfile
import zipfile

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
