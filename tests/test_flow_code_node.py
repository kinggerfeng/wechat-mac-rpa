"""The ``code`` node sandbox.

The refusal tests are the point of this file. A sandbox that has never been
shown a real escape attempt is a sandbox whose guarantees are a claim, and the
techniques below are the published ones — they are here so a future change that
loosens the allow-list fails a test rather than passing review.
"""

from __future__ import annotations

import sys

import os
from pathlib import Path

import pytest

from apps.engine.code_node import (
    MAX_STREAM_CHARS,
    CodeRefused,
    CodeTimeout,
    check_source,
    run_code,
)


def run(src: str, **kw):
    return run_code(src, root=kw.pop("root", "."), **kw)


# ── it actually runs code ───────────────────────────────────────────────────

def test_bind_to_output_returns_the_value():
    result = run("output = 1 + 2")
    assert result["ok"] is True
    assert result["value"] == 3
    assert result["value_from"] == "output"
    assert result["error"] == ""


def test_main_wins_over_a_bare_output_binding():
    result = run("output = 'ignored'\ndef main():\n    return 'from main'")
    assert result["value"] == "from main"
    assert result["value_from"] == "main"


def test_inputs_are_visible_as_bare_names():
    result = run("output = rows[0] * factor", inputs={"rows": [2, 4], "factor": 3})
    assert result["value"] == 6


def test_calling_a_builtin_is_allowed():
    # ast.Expr and ast.keyword used to be missing from the allow-list, which
    # made "sum(x, start=0)" a syntax refusal rather than an answer.
    result = run("output = sum([1, 2, 3], start=10)")
    assert result["value"] == 16


def test_fstring_and_comprehension_work():
    result = run(
        "names = ['a', 'bb']\n"
        "output = ', '.join(f'{n}:{len(n)}' for n in names)"
    )
    assert result["value"] == "a:1, bb:2"


def test_json_round_trips():
    result = run("output = json.loads('[1, 2]')")
    assert result["value"] == [1, 2]


def test_stdout_is_captured():
    result = run("print('hello')\noutput = 1")
    assert result["stdout"].strip() == "hello"
    assert result["truncated"] is False


def test_a_loop_is_allowed_and_finishes():
    result = run("total = 0\nfor i in range(1000):\n    total += i\noutput = total")
    assert result["value"] == 499500


# ── refusals: policy is checked before anything runs ───────────────────────

def test_import_is_refused():
    with pytest.raises(CodeRefused, match="不允许 import"):
        run("import os\noutput = 1")


def test_from_import_is_refused():
    with pytest.raises(CodeRefused):
        run("from os import system\noutput = 1")


def test_the_class_walk_is_refused_by_the_attribute_rule():
    with pytest.raises(CodeRefused, match="下划线开头的属性"):
        run("output = ().__class__")


def test_the_class_walk_is_refused_even_when_empty_parents():
    with pytest.raises(CodeRefused):
        run("output = [].__class__.__base__.__subclasses__()")


def test_getattr_with_a_dunder_string_is_refused():
    # The route the attribute rule cannot see: the name arrives as a literal.
    with pytest.raises(CodeRefused, match="字符串常量"):
        run("output = getattr(1, '__class__')")


def test_dunder_string_is_refused_even_as_an_ordinary_value():
    with pytest.raises(CodeRefused):
        run("output = '__init__'")


def test_private_name_is_refused():
    with pytest.raises(CodeRefused, match="下划线开头的名字"):
        run("output = __builtins__")


def test_open_is_not_in_the_namespace():
    # Not refused by the AST — simply absent, so it fails where the author wrote
    # it rather than somewhere confusing.
    result = run("output = open('/etc/passwd').read()")
    assert result["ok"] is False
    assert result["error_type"] == "NameError"
    assert "__builtins__" in result["error"] or "open" in result["error"]


def test_eval_and_exec_are_absent():
    for name in ("eval", "exec", "compile"):
        result = run(f"output = {name}('1')")
        assert result["ok"] is False
        assert result["error_type"] == "NameError", name


def test_dunder_import_is_refused_by_the_name_rule():
    # Refused before it could even be looked up, which is stronger than the
    # NameError the other three get.
    with pytest.raises(CodeRefused, match="下划线开头的名字"):
        run("output = __import__('os')")


def test_reflection_builtins_are_absent():
    for name in ("getattr", "setattr", "vars", "globals", "locals", "dir", "type"):
        # ``type`` is present by design; the rest are not.
        if name == "type":
            continue
        result = run(f"output = {name}(1)")
        assert result["ok"] is False
        assert result["error_type"] == "NameError", name


def test_class_definition_is_refused():
    with pytest.raises(CodeRefused, match="不允许定义类"):
        run("class Evil:\n    pass\noutput = 1")


def test_lambda_is_refused_by_name():
    with pytest.raises(CodeRefused, match="lambda"):
        run("output = (lambda: 1)()")


def test_with_statement_is_refused():
    with pytest.raises(CodeRefused, match="with"):
        run("with open('x') as f:\n    pass")


def test_syntax_error_reports_the_line():
    with pytest.raises(CodeRefused, match="语法错误"):
        run("def broken(:\n    pass")


def test_empty_source_is_refused():
    with pytest.raises(CodeRefused, match="代码为空"):
        run("   \n  ")


def test_oversized_source_is_refused():
    with pytest.raises(CodeRefused, match="代码过长"):
        run("output = 1\n" + "# padding\n" * 60_000)


def test_illegal_input_name_is_refused():
    with pytest.raises(CodeRefused, match="非法输入名"):
        run("output = 1", inputs={"not an identifier": 2})


def test_check_source_returns_a_tree_for_good_code():
    import ast as _ast

    assert isinstance(check_source("output = 1"), _ast.Module)


# ── refusals: the filesystem goes through the same containment rule ─────────

def test_fs_read_stays_inside_the_root(tmp_path: Path):
    (tmp_path / "inside.txt").write_text("ok", encoding="utf-8")
    result = run_code("output = fs.read_text('inside.txt')", root=tmp_path)
    assert result["value"] == "ok"


@pytest.mark.parametrize("escape", ["../outside.txt", "/etc/hostname", "a/../../b"])
def test_fs_read_refuses_to_escape_the_root(tmp_path: Path, escape: str):
    (tmp_path.parent / "outside.txt").write_text("secret", encoding="utf-8")
    result = run_code(f"output = fs.read_text({escape!r})", root=tmp_path)
    assert result["ok"] is False
    assert result["error_type"] == "PathRefused"
    assert "secret" not in str(result)


def test_fs_write_is_refused_when_readonly(tmp_path: Path):
    result = run_code("fs.write_text('out.txt', 'x')", root=tmp_path, writable=False)
    assert result["ok"] is False
    assert result["error_type"] == "PathRefused"
    assert not (tmp_path / "out.txt").exists()


def test_fs_write_works_when_allowed_and_reports_the_relative_path(tmp_path: Path):
    result = run_code("output = fs.write_text('sub/out.txt', 'x')",
                      root=tmp_path, writable=True)
    assert result["ok"] is True
    assert result["value"] == "sub/out.txt"
    assert (tmp_path / "sub" / "out.txt").read_text(encoding="utf-8") == "x"


def test_fs_write_cannot_escape_the_root(tmp_path: Path):
    result = run_code("fs.write_text('../escaped.txt', 'x')", root=tmp_path, writable=True)
    assert result["error_type"] == "PathRefused"
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_fs_refuses_a_symlink_pointing_out_of_the_root(tmp_path: Path):
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir(exist_ok=True)
    (outside / "leak.txt").write_text("THE-SECRET-BODY", encoding="utf-8")
    link = tmp_path / "link"
    if not link.exists():
        os.symlink(outside, link)
    result = run_code("output = fs.read_text('link/leak.txt')", root=tmp_path)
    assert result["error_type"] == "PathRefused"
    # The path may be named in the message — that is how the author finds the
    # offending variable — but the contents must not be in the result.
    assert "THE-SECRET-BODY" not in str(result)


def test_fs_list_dir_reports_a_non_directory_rather_than_guessing(tmp_path: Path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    result = run_code("output = fs.list_dir('f.txt')", root=tmp_path)
    assert result["error_type"] == "PathRefused"


# ── runtime behaviour ──────────────────────────────────────────────────────

def test_a_runtime_error_is_reported_not_raised():
    result = run("output = 1 / 0")
    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error_type"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in result["error"]


def test_try_except_is_usable_inside_the_sandbox():
    # The exception names have to be reachable or nobody can write a guard, and
    # an author forced to route around a try/except builds a bigger flow.
    result = run(
        "try:\n"
        "    output = int('nope')\n"
        "except ValueError:\n"
        "    output = 'caught'\n"
    )
    assert result["ok"] is True
    assert result["value"] == "caught"


def test_system_exit_does_not_escape_the_node():
    result = run("raise SystemExit(3)")
    assert result["ok"] is False
    assert result["error_type"] == "SystemExit"


def test_a_runtime_error_after_a_print_still_keeps_the_output():
    # Silence here would be the project's signature bug: the run trace would
    # show a node that produced nothing and gave no reason.
    result = run("print('before')\noutput = 1 / 0")
    assert result["stdout"].strip() == "before"
    assert result["error_type"] == "ZeroDivisionError"


def test_stdout_is_bounded_and_says_it_was_truncated():
    result = run("print('x' * 5000)", max_output_chars=100)
    assert result["truncated"] is True
    assert len(result["stdout"]) <= 100


def test_default_stream_cap_is_the_module_constant():
    # The number has to be baked into the source: the snippet runs in a
    # namespace that cannot see this module's constants, by design.
    size = MAX_STREAM_CHARS + 5000
    result = run(f"print('y' * {size})")
    assert result["truncated"] is True
    assert len(result["stdout"]) <= MAX_STREAM_CHARS


def test_an_infinite_loop_hits_the_deadline():
    result = run_code("while True:\n    pass", root=".", timeout=0.4)
    assert result["ok"] is False
    assert result["status"] == "timeout"
    assert result["error_type"] == "CodeTimeout"


def test_the_deadline_restores_the_previous_trace_hook():
    import sys

    before = sys.gettrace()
    run_code("output = 1", root=".", timeout=1.0)
    assert sys.gettrace() is before


def test_the_deadline_restores_the_hook_even_after_a_timeout():
    import sys

    before = sys.gettrace()
    run_code("while True:\n    pass", root=".", timeout=0.3)
    assert sys.gettrace() is before


def test_a_nested_timeout_message_names_the_budget():
    result = run_code("while True:\n    pass", root=".", timeout=0.3)
    assert "0.3" in result["error"]


def test_a_zero_or_negative_timeout_falls_back_to_the_default():
    # 0 means "use the default" everywhere else in this codebase; treating it
    # as "no limit" here would be an unbounded loop by accident.
    result = run_code("output = 1", root=".", timeout=0)
    assert result["ok"] is True


def test_timeout_under_a_thread_does_not_leak_the_hook_to_that_thread():
    import sys
    import threading

    seen: list[object] = []

    def worker() -> None:
        run_code("output = 1", root=".", timeout=1.0)
        seen.append(sys.gettrace())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen == [None]


def test_code_timeout_is_importable_and_is_a_timeout_error():
    assert issubclass(CodeTimeout, TimeoutError)


def test_an_exception_from_inside_the_sandbox_keeps_its_own_type():
    result = run("raise ValueError('bad input')")
    assert result["error_type"] == "ValueError"
    assert "bad input" in result["error"]
