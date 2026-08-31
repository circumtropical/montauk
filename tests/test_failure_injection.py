"""Failure injection for the atomic-write guarantee (spec Appendix B):
a crash anywhere in the write pipeline must leave the canonical file in
its original state -- never partially written -- and must not leave
orphaned temp files behind.

A real SIGKILL-mid-write test would be flaky (local disk writes are
fast enough that reliably hitting the write window requires an
artificial delay this module doesn't have hooks for); monkeypatching
the exact failure points is deterministic and exercises the same
guarantee Appendix B actually specifies: temp file + fsync + atomic
rename, cleanup on any exception.
"""

import os

import pytest

from montauk.markdown_store import atomic_write_text


class TestAtomicWriteFailureInjection:
    def test_crash_during_temp_file_write_leaves_original_untouched(self, tmp_path, monkeypatch):
        path = tmp_path / "person.md"
        path.write_text("original content\n")

        real_open = open

        def failing_open(*args, **kwargs):
            file_obj = real_open(*args, **kwargs)
            if str(args[0]).startswith(str(tmp_path / ".person.md.tmp-")):
                file_obj.write = lambda data: (_ for _ in ()).throw(OSError("simulated crash mid-write"))
            return file_obj

        monkeypatch.setattr("builtins.open", failing_open)

        with pytest.raises(OSError, match="simulated crash mid-write"):
            atomic_write_text(path, "new content that must never land\n")

        assert path.read_text() == "original content\n"
        assert list(tmp_path.glob(".person.md.tmp-*")) == []

    def test_crash_during_atomic_rename_leaves_original_untouched(self, tmp_path, monkeypatch):
        path = tmp_path / "person.md"
        path.write_text("original content\n")

        def failing_replace(*args, **kwargs):
            raise OSError("simulated crash during rename")

        monkeypatch.setattr("os.replace", failing_replace)

        with pytest.raises(OSError, match="simulated crash during rename"):
            atomic_write_text(path, "new content that must never land\n")

        assert path.read_text() == "original content\n"
        assert list(tmp_path.glob(".person.md.tmp-*")) == []

    def test_crash_during_file_fsync_leaves_original_untouched(self, tmp_path, monkeypatch):
        path = tmp_path / "person.md"
        path.write_text("original content\n")

        monkeypatch.setattr("os.fsync", lambda fd: (_ for _ in ()).throw(OSError("simulated fsync failure")))

        with pytest.raises(OSError, match="simulated fsync failure"):
            atomic_write_text(path, "new content that must never land\n")

        assert path.read_text() == "original content\n"
        assert list(tmp_path.glob(".person.md.tmp-*")) == []

    def test_new_file_creation_crash_leaves_no_partial_file(self, tmp_path, monkeypatch):
        # No pre-existing file at all -- a crash must not leave a
        # zero-byte or partially-written file at the destination path.
        path = tmp_path / "brand-new-person.md"
        monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError("simulated crash")))

        with pytest.raises(OSError):
            atomic_write_text(path, "content that must never land\n")

        assert not path.exists()
        assert list(tmp_path.glob(".brand-new-person.md.tmp-*")) == []

    def test_rename_success_survives_a_later_directory_fsync_failure(self, tmp_path, monkeypatch):
        # Documents a real, deliberate nuance: os.replace() itself is
        # atomic and, once it returns, the new content IS on disk. If the
        # *directory* fsync afterward fails, atomic_write_text still
        # raises (conservative -- durability of the rename couldn't be
        # confirmed), but the content itself was not rolled back and
        # should not be assumed lost by a caller catching the exception.
        path = tmp_path / "person.md"
        path.write_text("original content\n")

        real_fsync = os.fsync
        call_count = {"n": 0}

        def flaky_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] == 2:  # 1st call is the file's own fsync; 2nd is the directory's
                raise OSError("simulated directory fsync failure")
            return real_fsync(fd)

        monkeypatch.setattr("os.fsync", flaky_fsync)

        with pytest.raises(OSError, match="simulated directory fsync failure"):
            atomic_write_text(path, "new content\n")

        assert path.read_text() == "new content\n"
