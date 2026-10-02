"""Filesystem nodes.

The tests are weighted towards refusal. An automation tool that can be pointed
outside its own directory by a webhook payload is a remote file primitive, and
the whole point of the containment check is that it cannot be talked out of by a
plausible-looking path. Each of the escape shapes below is one that has worked
on somebody's file manager:

* ``../`` — the obvious one, and the one a naive string-prefix check catches.
* An absolute path — ``PROJECT_ROOT / "/etc/passwd"`` is ``/etc/passwd``,
  because ``Path.__truediv__`` with an absolute right-hand side discards the
  left entirely. A check written as ``str(candidate).startswith(root)`` sees
  ``/etc/passwd`` and passes, because the prefix is *absent*, not present.
* A symlink pointing out — invisible to both, since the string is clean.
* Trailing slashes and ``..`` that only appear after resolution.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.flow import files
from src.flow.context import FlowContext, FlowScope
from src.flow.registry import get_node_registry
from src.flow.schema import NodeError

REG = get_node_registry()


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A fake project root, so nothing here can touch the real repository."""
    import src.flow.builtin_nodes as builtin

    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    monkeypatch.setattr(builtin, "PROJECT_ROOT", root)
    return root


def run(node_type, params, **services):
    instance = REG.get(node_type).handler(params)
    instance.ctx = FlowContext(scope=FlowScope({}), run_id="r", flow_id="f")
    for name, value in services.items():
        instance.ctx.services[name] = value
    return instance.execute()


class TestContainment:
    @pytest.mark.parametrize("raw", [
        "../outside.txt",
        "../../etc/passwd",
        "data/../../escape.txt",
        "data/./../../escape.txt",
    ])
    def test_dot_dot_is_refused(self, sandbox, raw):
        with pytest.raises(NodeError, match="解析后落在项目根目录之外"):
            run("write_file", {"path": raw, "content": "x"})

    def test_an_absolute_path_is_refused(self, sandbox, tmp_path):
        """``Path("/root") / "/etc/passwd"`` is ``/etc/passwd``. The left side
        vanishes, so a prefix check on the joined string sees no prefix and — if
        written as "does it start with the root?" — wrongly passes."""
        outside = tmp_path / "outside" / "secret.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("top secret", encoding="utf-8")
        with pytest.raises(NodeError, match="解析后落在项目根目录之外"):
            run("read_file", {"path": str(outside)})
        assert outside.read_text(encoding="utf-8") == "top secret"

    def test_a_symlink_out_of_the_root_is_refused(self, sandbox, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("top secret", encoding="utf-8")
        link = sandbox / "data" / "link.txt"
        link.symlink_to(secret)
        with pytest.raises(NodeError, match="解析后落在项目根目录之外"):
            run("read_file", {"path": "data/link.txt"})
        assert secret.read_text(encoding="utf-8") == "top secret"

    def test_a_symlinked_directory_out_of_the_root_is_refused(self, sandbox, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("top secret", encoding="utf-8")
        (sandbox / "data" / "link").symlink_to(outside)
        with pytest.raises(NodeError):
            run("list_files", {"root": "data/link"})

    def test_a_path_inside_the_root_is_fine(self, sandbox):
        out = run("write_file", {"path": "data/notes.txt", "content": "hello"})
        assert out["written"] is True
        assert out["path"] == "data/notes.txt"
        assert (sandbox / "data" / "notes.txt").read_text(encoding="utf-8") == "hello"

    def test_the_root_itself_is_allowed(self, sandbox):
        assert run("make_dir", {"path": "data/sub"})["created"] is True

    def test_the_message_names_the_resolved_location(self, sandbox):
        """「路径越界」 alone leaves the author guessing which path was meant."""
        with pytest.raises(NodeError) as excinfo:
            run("read_file", {"path": "../../etc/hosts"})
        assert "../../etc/hosts" in str(excinfo.value)


class TestReadWrite:
    def test_read_round_trips(self, sandbox):
        run("write_file", {"path": "data/a.txt", "content": "第一行\n第二行"})
        out = run("read_file", {"path": "data/a.txt"})
        assert out["read"] is True
        assert out["content"] == "第一行\n第二行"
        assert out["lines"] == 2

    def test_append(self, sandbox):
        run("write_file", {"path": "data/a.txt", "content": "a"})
        run("write_file", {"path": "data/a.txt", "content": "b", "mode": "append"})
        assert run("read_file", {"path": "data/a.txt"})["content"] == "ab"

    def test_parents_are_created(self, sandbox):
        run("write_file", {"path": "data/x/y/z.txt", "content": "deep"})
        assert (sandbox / "data" / "x" / "y" / "z.txt").exists()

    def test_missing_file_reports_rather_than_raises(self, sandbox):
        out = run("read_file", {"path": "data/nope.txt"})
        assert out["read"] is False
        assert out["reason"] == "文件不存在"
        assert out["content"] == ""

    def test_a_binary_extension_is_refused_not_mangled(self, sandbox):
        """Reading a binary as UTF-8 with errors=replace yields replacement
        characters and a success report — the data was never actually read."""
        blob = sandbox / "data" / "a.bin"
        blob.write_bytes(bytes(range(256)))
        out = run("read_file", {"path": "data/a.bin"})
        assert out["read"] is False
        assert "白名单" in out["reason"]

    def test_an_oversized_file_is_refused(self, sandbox):
        big = sandbox / "data" / "big.txt"
        big.write_text("x" * (files.MAX_TEXT_BYTES + 10), encoding="utf-8")
        out = run("read_file", {"path": "data/big.txt"})
        assert out["read"] is False
        assert "上限" in out["reason"]

    def test_empty_path_is_an_error(self, sandbox):
        with pytest.raises(NodeError, match="需要 path"):
            run("read_file", {"path": "  "})


class TestListFiles:
    def _tree(self, sandbox):
        (sandbox / "data" / "sub").mkdir(parents=True)
        for name, age in (("old.pdf", 100), ("new.pdf", 200), ("note.txt", 150)):
            path = sandbox / "data" / name
            path.write_text("x", encoding="utf-8")
            os.utime(path, (age, age))
        sub = sandbox / "data" / "sub" / "deep.pdf"
        sub.write_text("x", encoding="utf-8")
        # Every file gets an explicit mtime, including the nested one — a
        # created-now file is the newest, which would mask the ordering.
        os.utime(sub, (300, 300))
        return sandbox

    def test_newest_first_and_recursive(self, sandbox):
        self._tree(sandbox)
        out = run("list_files", {"root": "data"})
        names = [e["name"] for e in out["entries"]]
        # deep.pdf is the newest of all four; ordering has to hold across
        # directories, not just within one.
        assert names == ["deep.pdf", "new.pdf", "note.txt", "old.pdf"]
        assert out["count"] == 4
        assert out["truncated"] is False

    def test_pattern_filters(self, sandbox):
        self._tree(sandbox)
        out = run("list_files", {"root": "data", "pattern": "*.pdf"})
        assert {e["name"] for e in out["entries"]} == {"old.pdf", "new.pdf", "deep.pdf"}
        # Newest first, across directories too.
        assert [e["name"] for e in out["entries"]] == ["deep.pdf", "new.pdf", "old.pdf"]

    def test_non_recursive_stays_shallow(self, sandbox):
        self._tree(sandbox)
        out = run("list_files", {"root": "data", "recursive": False})
        assert "deep.pdf" not in [e["name"] for e in out["entries"]]

    def test_truncation_is_reported_not_silent(self, sandbox):
        """A cap that quietly drops entries is a cap that lies: the flow sees a
        short list and treats it as the whole directory."""
        for index in range(12):
            (sandbox / "data" / f"f{index}.txt").write_text("x", encoding="utf-8")
        out = run("list_files", {"root": "data", "max_results": 5})
        assert out["truncated"] is True
        assert out["count"] == 5

    def test_missing_directory_is_an_error(self, sandbox):
        with pytest.raises(NodeError, match="不是目录"):
            run("list_files", {"root": "data/nope"})

    def test_directories_can_be_included(self, sandbox):
        self._tree(sandbox)
        out = run("list_files", {"root": "data", "include_dirs": True, "pattern": "sub"})
        assert [e["name"] for e in out["entries"]] == ["sub"]
        assert out["entries"][0]["is_dir"] is True


class TestMoveCopyDelete:
    def _files(self, sandbox):
        (sandbox / "in").mkdir(parents=True)
        (sandbox / "out").mkdir(parents=True)
        for name in ("a.txt", "b.txt", "c.pdf"):
            (sandbox / "in" / name).write_text(name, encoding="utf-8")
        return sandbox

    def test_move_renames(self, sandbox):
        self._files(sandbox)
        out = run("move_file", {"source": "in/a.txt", "target": "out/z.txt"})
        assert out["to"] == "out/z.txt"
        assert (sandbox / "out" / "z.txt").read_text(encoding="utf-8") == "a.txt"
        assert not (sandbox / "in" / "a.txt").exists()

    def test_move_refuses_to_clobber_by_default(self, sandbox):
        self._files(sandbox)
        (sandbox / "out" / "z.txt").write_text("existing", encoding="utf-8")
        with pytest.raises(NodeError, match="覆盖"):
            run("move_file", {"source": "in/a.txt", "target": "out/z.txt"})
        assert (sandbox / "out" / "z.txt").read_text(encoding="utf-8") == "existing"

    def test_move_overwrites_when_asked(self, sandbox):
        self._files(sandbox)
        (sandbox / "out" / "z.txt").write_text("existing", encoding="utf-8")
        run("move_file", {"source": "in/a.txt", "target": "out/z.txt", "overwrite": True})
        assert (sandbox / "out" / "z.txt").read_text(encoding="utf-8") == "a.txt"

    def test_copy_does_not_overwrite_by_default(self, sandbox):
        """A batch copy that silently replaces a file somebody had already
        edited is noticed a week later, if ever."""
        self._files(sandbox)
        (sandbox / "out" / "a.txt").write_text("edited", encoding="utf-8")
        out = run("copy_files", {"source": "in", "target": "out", "pattern": "*.txt"})
        assert out["count"] == 2
        assert (sandbox / "out" / "a.txt").read_text(encoding="utf-8") == "edited"
        renamed = [e["to"] for e in out["entries"] if e["to"].endswith("a (1).txt")]
        assert renamed, out["entries"]

    def test_copy_reports_each_file(self, sandbox):
        self._files(sandbox)
        out = run("copy_files", {"source": "in", "target": "out"})
        assert out["count"] == 3
        assert out["failed"] == []

    def test_delete_a_file(self, sandbox):
        self._files(sandbox)
        out = run("delete_path", {"path": "in/a.txt"})
        assert out["deleted"] is True
        assert out["kind"] == "file"
        assert not (sandbox / "in" / "a.txt").exists()

    def test_delete_a_directory_needs_recursive(self, sandbox):
        self._files(sandbox)
        with pytest.raises(NodeError, match="recursive"):
            run("delete_path", {"path": "in"})
        assert (sandbox / "in" / "a.txt").exists()

    def test_delete_a_directory_recursively(self, sandbox):
        self._files(sandbox)
        out = run("delete_path", {"path": "in", "recursive": True})
        assert out["kind"] == "dir"
        assert out["entries"] == 3
        assert not (sandbox / "in").exists()

    def test_delete_a_symlinked_directory_unlinks_the_link_only(self, sandbox, tmp_path):
        """``shutil.rmtree`` on a symlink raises; the naive workaround of
        resolving it first deletes the *target's* contents."""
        outside = tmp_path / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        link = sandbox / "data" / "link"
        link.symlink_to(outside)
        out = files.delete_path(link, recursive=True)
        assert out["kind"] == "symlink"
        assert not link.exists()
        assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"

    def test_deleting_something_absent_reports_it(self, sandbox):
        out = run("delete_path", {"path": "data/never-existed"})
        assert out["deleted"] is False
        assert out["reason"] == "文件不存在"

    def test_file_exists_distinguishes_file_and_dir(self, sandbox):
        self._files(sandbox)
        assert run("file_exists", {"path": "in"}) == {
            "exists": True, "is_dir": True, "is_file": False, "path": "in"}
        assert run("file_exists", {"path": "in/a.txt"})["is_file"] is True
        assert run("file_exists", {"path": "in/zzz"})["exists"] is False
