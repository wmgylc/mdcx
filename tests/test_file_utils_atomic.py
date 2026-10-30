import asyncio
import errno

from mdcx.utils import file as file_utils


def test_copy_failure_preserves_existing_destination(monkeypatch, tmp_path):
    source = tmp_path / "source.nfo"
    destination = tmp_path / "destination.nfo"
    source.write_text("new metadata", encoding="utf-8")
    destination.write_text("old metadata", encoding="utf-8")

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(file_utils.shutil, "copy", fail_copy)
    success, _ = file_utils.copy_file_sync(source, destination)

    assert not success
    assert source.read_text(encoding="utf-8") == "new metadata"
    assert destination.read_text(encoding="utf-8") == "old metadata"
    assert list(tmp_path.glob("*.tmp")) == []


def test_cross_device_move_failure_preserves_existing_destination(monkeypatch, tmp_path):
    source = tmp_path / "source.nfo"
    destination = tmp_path / "destination.nfo"
    source.write_text("new metadata", encoding="utf-8")
    destination.write_text("old metadata", encoding="utf-8")
    real_replace = file_utils.os.replace
    first_replace = True

    def report_cross_device_once(src, dst):
        nonlocal first_replace
        if first_replace:
            first_replace = False
            raise OSError(errno.EXDEV, "simulated cross-device move")
        return real_replace(src, dst)

    def fail_copy(*_args, **_kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(file_utils.os, "replace", report_cross_device_once)
    monkeypatch.setattr(file_utils.shutil, "copy2", fail_copy)
    success, _ = file_utils.move_file_sync(source, destination)

    assert not success
    assert source.read_text(encoding="utf-8") == "new metadata"
    assert destination.read_text(encoding="utf-8") == "old metadata"
    assert list(tmp_path.glob("*.tmp")) == []


def test_async_copy_replaces_destination_after_staging(tmp_path):
    source = tmp_path / "source.nfo"
    destination = tmp_path / "destination.nfo"
    source.write_text("new metadata", encoding="utf-8")
    destination.write_text("old metadata", encoding="utf-8")

    success, error = asyncio.run(file_utils.copy_file_async(source, destination))

    assert success, error
    assert destination.read_text(encoding="utf-8") == "new metadata"
    assert list(tmp_path.glob("*.tmp")) == []
