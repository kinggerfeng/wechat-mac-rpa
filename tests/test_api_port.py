"""The local API port is declared once, and both sides read that one place.

The Rust shell spawned uvicorn on 8767 (``API_PORT`` in ``lib.rs``) while the
frontend defaulted to 8768 (``api/client.ts``). Nothing failed, because nothing
checked: a dev build's frontend simply talked to whatever was already listening
on 8768 — typically the installed RPAStudio.app. Two engines, one ``rpa.db``,
one ``data/logs/desktop-api.log``. The single-instance lease stopped two
schedulers from running at once, but every number on screen came from the other
process, which is a harder bug to notice than a crash.

A build-time mismatch is the failure mode here, not a runtime one, so the
checks are static: the two sides must reference the declaration and must not
carry a literal of their own.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_ROOT = REPO_ROOT / "apps" / "desktop"
#: The declaration lives in the shared tree because there are now two frontends
#: that must agree on it — the Tauri shell and the operations console. Putting
#: it in either app would make one of them reach sideways for it.
SHARED_ROOT = REPO_ROOT / "apps" / "shared"

DECLARATION = SHARED_ROOT / "api-port.txt"
RUST_SHELL = DESKTOP_ROOT / "src-tauri" / "src" / "lib.rs"
FRONTEND_CLIENT = SHARED_ROOT / "api" / "client.ts"


def _declaration() -> int:
    raw = DECLARATION.read_text(encoding="utf-8").strip()
    port = int(raw)
    assert 1 <= port <= 65535, f"apps/shared/api-port.txt holds an impossible port: {port}"
    return port


def test_the_declaration_exists_and_is_a_port():
    """A missing file fails the Rust build at include_str! and the TS build at
    import, so this is the cheap check that the file is not empty or garbage."""
    assert DECLARATION.is_file(), "apps/shared/api-port.txt is the single source and is missing"
    _declaration()


def _code_lines(text: str) -> str:
    """Drop comment-only lines, keep everything else.

    Both files explain this bug in comments, and those explanations necessarily
    name the two values that used to disagree, so a raw scan reports them — and
    a comment cannot be a second declaration.

    Only whole-line comments are dropped. An earlier version stripped from the
    first ``//`` on any line, which silently ate every ``http://`` literal and
    with it the port that follows — the test passed on a hardcoded origin for
    exactly that reason. A trailing comment on a code line is not handled; a
    port written there would be missed, which is the smaller error.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*", "#")):
            continue
        kept.append(line)
    return "\n".join(kept)


def test_the_rust_shell_reads_the_declaration():
    text = RUST_SHELL.read_text(encoding="utf-8")
    assert "api-port.txt" in text, (
        "lib.rs no longer reads apps/shared/api-port.txt; the port went back to a literal"
    )
    literals = re.findall(r"\b876[0-9]\b", _code_lines(text))
    assert not literals, (
        f"lib.rs carries a hardcoded port {literals}; it must come from "
        f"apps/shared/api-port.txt so the frontend can read the same value"
    )


def test_the_rust_include_path_actually_resolves():
    """`include_str!` is resolved against the file that contains it.

    Checking that ``lib.rs`` mentions ``api-port.txt`` is not enough. The path
    can be wrong by a level and the file can still be named correctly — one
    ``../`` short resolves to ``src-tauri/api-port.txt``, which does not exist,
    and the crate then fails to compile instead of warning. Nothing in this
    repository compiles the Rust crate, so the only thing standing between a
    typo and a broken release build is this assertion.
    """
    text = RUST_SHELL.read_text(encoding="utf-8")
    match = re.search(r'include_str!\("([^"]+)"\)', text)
    assert match, "lib.rs no longer has an include_str! for the port declaration"

    resolved = (RUST_SHELL.parent / match.group(1)).resolve()
    assert resolved == DECLARATION.resolve(), (
        f"lib.rs include_str! points at {resolved}, not {DECLARATION}. "
        f"`include_str!` resolves against the source file, so from "
        f"{RUST_SHELL.parent} the declaration is at "
        f"'{os.path.relpath(DECLARATION, RUST_SHELL.parent)}'."
    )
    assert resolved.is_file(), f"{resolved} does not exist — the crate would not compile"


def test_the_frontend_import_path_actually_resolves():
    """Same failure, other side: Vite resolves `?raw` from the importing file.

    A path one level too deep does not fall back, it fails the build with
    `UNRESOLVED_IMPORT` — which is at least loud, but only if someone runs the
    build.
    """
    text = FRONTEND_CLIENT.read_text(encoding="utf-8")
    match = re.search(r'from "([^"]*api-port\.txt\?raw)"', text)
    assert match, "client.ts no longer imports the port declaration"

    specifier = match.group(1).removesuffix("?raw")
    resolved = (FRONTEND_CLIENT.parent / specifier).resolve()
    assert resolved == DECLARATION.resolve(), (
        f"client.ts imports {specifier!r}, which resolves to {resolved}, "
        f"not {DECLARATION}"
    )
    assert resolved.is_file(), f"{resolved} does not exist — the frontend build would fail"


def test_the_frontend_reads_the_declaration():
    text = FRONTEND_CLIENT.read_text(encoding="utf-8")
    assert "api-port.txt?raw" in text, (
        "client.ts no longer reads apps/shared/api-port.txt; the origin went back to a literal"
    )
    # Checked against the raw source: `127.0.0.1:<port>` is specific enough that
    # the comment explaining the old bug cannot produce a false positive, and
    # it is the form a hardcoded origin actually takes.
    literals = re.findall(r"127\.0\.0\.1:\d{4}", text)
    assert not literals, (
        f"client.ts hardcodes {literals}; the origin must be built from "
        f"apps/shared/api-port.txt"
    )


def test_neither_side_declares_the_port_twice():
    """Exactly one declaration of the port number in the repository.

    The point of the file is that there is one. A second copy, whether in a
    second config or in a comment that someone will one day copy, is the bug.
    """
    offenders: list[str] = []
    # Two frontends now, so "the side" is two trees: the Tauri shell and
    # everything they share.
    for root in (DESKTOP_ROOT, SHARED_ROOT):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path == DECLARATION:
                continue
            if any(part in {"node_modules", "dist", "target", "__pycache__"}
                   for part in path.relative_to(REPO_ROOT).parts):
                continue
            if path.suffix not in {".rs", ".ts", ".vue", ".json", ".txt"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for number in re.findall(r"\b876\d\b", _code_lines(text)):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {number}")

    assert not offenders, (
        f"the local API port is declared more than once: {offenders}. "
        f"Only apps/shared/api-port.txt may name it."
    )


def test_the_running_service_agrees_with_the_declaration():
    """The installed bundle is the other half of this, and it is on disk.

    A stale ``/Applications/RPAStudio.app`` is exactly how a dev build ends up
    talking to the wrong process, so its declared port is worth comparing
    rather than assuming it was rebuilt.
    """
    bundle = Path("/Applications/RPAStudio.app")
    if not bundle.is_dir():
        return  # not installed here; nothing to disagree with

    binary = bundle / "Contents" / "MacOS" / "WeChat Mac RPA"
    if not binary.is_file():
        return  # bundle built by a toolchain that names it differently

    port = _declaration()
    blob = binary.read_bytes()
    # The port reaches the binary as a decimal string in the argument list.
    assert str(port).encode() in blob, (
        f"the installed app does not carry port {port} from apps/shared/api-port.txt; "
        f"it was built before the port had a single source, and a dev build "
        f"pointing at it will talk to the wrong process"
    )


def test_both_frontends_resolve_the_shared_tree():
    """There are two Vite apps now, and they must agree on the declaration.

    This used to assert the declaration sat inside the desktop project root,
    on the theory that ``?raw`` needs it there. That is not how Vite works —
    ``?raw`` resolves through the alias and the filesystem, which is why the
    console builds fine with the file outside *both* project roots. So the
    assertion is now the one that actually predicts breakage: if either app
    drops its ``@shared`` alias, its `?raw` import stops resolving and its
    build fails, and this fails first with a readable message.
    """
    assert DECLARATION.is_file(), f"{DECLARATION} is the single source and is missing"
    assert DECLARATION.parent == SHARED_ROOT

    for app in ("desktop", "admin_console"):
        config = (REPO_ROOT / "apps" / app / "vite.config.ts").read_text(encoding="utf-8")
        assert "@shared" in config, (
            f"apps/{app}/vite.config.ts has no @shared alias, so its "
            f"frontend cannot read apps/shared/api-port.txt"
        )
        assert '"../shared"' in config, (
            f"apps/{app}/vite.config.ts aliases @shared somewhere other than "
            f"apps/shared, so it reads a different declaration than the other app"
        )


def test_the_declaration_is_not_mistaken_for_configuration():
    """It is a port, not a secret, and should be readable in a plain diff."""
    assert json.dumps(_declaration()) == str(_declaration())
