"""Tests for sync module."""

import io
import tarfile
import zipfile
import pytest
from pathlib import Path

from cinna.sync import (
    compute_local_manifest,
    diff_manifests,
    create_workspace_tarball,
    extract_workspace_tarball,
    _is_excluded,
    PUSH_EXCLUDES,
    MAX_FILE_SIZE,
)


def test_compute_local_manifest(tmp_path):
    (tmp_path / "main.py").write_text("print('hello')")
    (tmp_path / "lib.py").write_text("x = 1")

    manifest = compute_local_manifest(tmp_path)
    assert "main.py" in manifest
    assert "lib.py" in manifest
    assert "sha256" in manifest["main.py"]
    assert "size" in manifest["main.py"]


def test_compute_local_manifest_excludes_pycache(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"bytecode")
    (tmp_path / "main.py").write_text("code")

    manifest = compute_local_manifest(tmp_path)
    assert "main.py" in manifest
    # __pycache__ contents should be excluded
    assert not any("__pycache__" in k for k in manifest)


def test_compute_local_manifest_skips_symlinks(tmp_path):
    real = tmp_path / "real.py"
    real.write_text("code")
    link = tmp_path / "link.py"
    link.symlink_to(real)

    manifest = compute_local_manifest(tmp_path)
    assert "real.py" in manifest
    assert "link.py" not in manifest


def test_compute_local_manifest_empty_dir(tmp_path):
    manifest = compute_local_manifest(tmp_path)
    assert manifest == {}


def test_diff_no_changes():
    manifest = {"a.py": {"sha256": "abc"}}
    local_changed, remote_changed, conflicts = diff_manifests(manifest, manifest, manifest)
    assert local_changed == []
    assert remote_changed == []
    assert conflicts == []


def test_diff_new_local_file():
    local = {"a.py": {"sha256": "abc"}, "b.py": {"sha256": "def"}}
    remote = {"a.py": {"sha256": "abc"}}
    last = {"a.py": {"sha256": "abc"}}

    local_changed, remote_changed, conflicts = diff_manifests(local, remote, last)
    assert "b.py" in local_changed
    assert remote_changed == []
    assert conflicts == []


def test_diff_new_remote_file():
    local = {"a.py": {"sha256": "abc"}}
    remote = {"a.py": {"sha256": "abc"}, "c.py": {"sha256": "ghi"}}
    last = {"a.py": {"sha256": "abc"}}

    local_changed, remote_changed, conflicts = diff_manifests(local, remote, last)
    assert local_changed == []
    assert "c.py" in remote_changed
    assert conflicts == []


def test_diff_conflict():
    local = {"a.py": {"sha256": "local-version"}}
    remote = {"a.py": {"sha256": "remote-version"}}
    last = {"a.py": {"sha256": "original"}}

    local_changed, remote_changed, conflicts = diff_manifests(local, remote, last)
    assert "a.py" in conflicts


def test_diff_both_changed_same():
    """Both sides changed to the same content — no conflict."""
    local = {"a.py": {"sha256": "same-new"}}
    remote = {"a.py": {"sha256": "same-new"}}
    last = {"a.py": {"sha256": "original"}}

    local_changed, remote_changed, conflicts = diff_manifests(local, remote, last)
    assert conflicts == []
    assert local_changed == []
    assert remote_changed == []


def test_diff_local_delete():
    local = {}
    remote = {"a.py": {"sha256": "abc"}}
    last = {"a.py": {"sha256": "abc"}}

    local_changed, remote_changed, conflicts = diff_manifests(local, remote, last)
    # local no longer has the file (sha256 is None vs last sha256 "abc") → local changed
    assert "a.py" in local_changed


def test_create_workspace_tarball(tmp_path):
    (tmp_path / "script.py").write_text("print('hi')")
    (tmp_path / "data.txt").write_text("some data")

    tarball = create_workspace_tarball(tmp_path, ["script.py"])
    assert len(tarball) > 0

    # Verify contents
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        names = tar.getnames()
        assert "script.py" in names
        assert "data.txt" not in names


def test_extract_workspace_tarball(tmp_path):
    # Create a tarball
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
    """only_files limits which files are extracted from a tarball."""
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


def test_extract_zip_only_files(tmp_path):
    """only_files limits which files are extracted from a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.py", "a")
        zf.writestr("b.py", "b")
        zf.writestr("c.py", "c")
    archive = buf.getvalue()

    extracted = extract_workspace_tarball(archive, tmp_path, only_files={"b.py"})
    assert extracted == ["b.py"]
    assert not (tmp_path / "a.py").exists()
    assert (tmp_path / "b.py").exists()
    assert not (tmp_path / "c.py").exists()


def test_diff_force_push_conflicts():
    """Conflicts are separate from local_changed — caller must merge for --force."""
    local = {"a.py": {"sha256": "local"}, "b.py": {"sha256": "new-local"}}
    remote = {"a.py": {"sha256": "remote"}, "b.py": {"sha256": "unchanged"}}
    last = {"a.py": {"sha256": "original"}, "b.py": {"sha256": "unchanged"}}

    local_changed, remote_changed, conflicts = diff_manifests(local, remote, last)
    assert "a.py" in conflicts
    assert "b.py" in local_changed
    # Simulating --force: extend local_changed with conflicts
    local_changed.extend(conflicts)
    assert "a.py" in local_changed
    assert "b.py" in local_changed


def test_is_excluded():
    assert _is_excluded("__pycache__/mod.pyc", PUSH_EXCLUDES) is True
    assert _is_excluded("main.pyc", PUSH_EXCLUDES) is True
    assert _is_excluded(".DS_Store", PUSH_EXCLUDES) is True
    assert _is_excluded("credentials/creds.json", PUSH_EXCLUDES) is True
    assert _is_excluded("scripts/main.py", PUSH_EXCLUDES) is False
