"""Guards for the BusyBox artifact fixture itself.

The fixture is test infrastructure, so its failures are silent unless
something asserts them: a fixture that quietly hands back the wrong binary
would make every downstream matrix test a lie about a version it never ran.

Two classes of guard live here, and the split is deliberate. The version
banner needs a real artifact and an interpreter that can run it, so it is
marked `busybox` and skipped where it cannot execute. Everything else — the
pin bookkeeping, the fetch's failure and publish paths — is network-free and
NEVER skipped, because those are exactly the layers that would otherwise be
exercised only by the marked tier and so go unexercised on the machine where
the tier is skipped.
"""

import ast
import contextlib
import http.client
import json
import os
import re
import stat
import subprocess
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 only, otto's floor
    import tomli as tomllib
from typing_extensions import Self  # `typing.Self` is 3.11+; otto's floor is 3.10

from tests._ambient_env import AMBIENT_OPT_INS
from tests._fixtures import busybox, busybox_rootfs
from tests._fixtures.busybox import (
    BUSYBOX_MATRIX,
    QEMU_HANDLER,
    BusyBoxUnavailableError,
    busybox_binary,
    cache_dir,
    can_run,
    probe_banner,
    require_interpreter,
)
from tests._fixtures.paths import PROJECT_ROOT, TESTS_ROOT

# The repo root. Sourced from tests/_fixtures/paths.py rather than derived
# with Path(__file__).parents[N] here, so a move of this file cannot
# silently re-anchor it.
_REPO_ROOT = PROJECT_ROOT

_MATRIX_PARAMS = [
    pytest.param(
        release,
        id=release.version,
        marks=pytest.mark.skipif(
            not can_run(release.arch),
            reason=(
                f"no usable {release.arch} interpreter: this "
                f"{os.uname().machine} machine has no enabled "
                f"{QEMU_HANDLER[release.arch]} binfmt handler; install "
                f"qemu-user-static (see docs/guide/hosts/busybox.md)"
            ),
        ),
    )
    for release in BUSYBOX_MATRIX
]


@pytest.mark.busybox
@pytest.mark.parametrize("release", _MATRIX_PARAMS)
def test_the_artifact_announces_the_version_it_claims(release):
    """Behaviour, not bytes, is what this fixture exists to pin.

    Upstream publishes no checksums or signatures for the prebuilt binaries,
    so a hash can only say 'the same bytes as last time'. What downstream
    matrix tests actually depend on is that the 1.21.1 artifact parses
    arguments like 1.21.1 — and the banner is the cheapest honest proxy.
    """
    # Re-asserted at RUN time, not just at collection: the skip above read
    # binfmt_misc while pytest was collecting, so a handler registered or
    # disabled since then is invisible to it. Costs a file read; buys a named
    # prerequisite instead of an `Exec format error` from execve.
    require_interpreter(release.arch)

    banner = probe_banner(busybox_binary(release))
    assert f"v{release.version}" in banner, (
        f"artifact for {release.version} announces {banner!r} — the matrix would "
        f"be testing a different release than it reports"
    )


def test_an_unrunnable_artifact_names_the_interpreter_it_needs(tmp_path):
    """The ENOEXEC path must say *why*, because that is the whole diagnosis.

    An x86 artifact on a machine with no interpreter fails inside execve with
    a bare 'Exec format error' — true and useless. The fixture converts it to
    a BusyBoxUnavailableError that names qemu-user-static, so the reader is
    told the missing dependency rather than left to infer it from an errno.
    Asserted with a hand-made non-binary rather than a real artifact so the
    guard holds on x86_64 CI too, where every matrix entry runs natively.
    """
    unrunnable = tmp_path / "busybox-not-really"
    unrunnable.write_bytes(b"MZ this is not an ELF for this machine\n")
    unrunnable.chmod(unrunnable.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(BusyBoxUnavailableError, match="qemu-user-static") as excinfo:
        probe_banner(unrunnable)

    assert str(unrunnable) in str(excinfo.value), (
        "the error must name the artifact that could not be executed — "
        "a matrix run fails one entry at a time"
    )


def test_the_skip_reads_the_handler_for_each_entrys_own_arch(tmp_path):
    """Each entry's skip must consult ITS arch's interpreter, and honour `disabled`.

    Both halves are silent when wrong, and wrong in the dangerous direction —
    they leave a test SELECTED on a machine that cannot run it, so the tier
    reports a missing qemu-user-static on a host that has one. Driven with an
    injected machine and binfmt root because on an x86_64 runner the native
    short-circuit makes every one of these branches dead code.
    """
    (tmp_path / "qemu-x86_64").write_text("enabled\ninterpreter /usr/bin/qemu-x86_64\n")

    assert can_run("x86_64", machine="aarch64", binfmt_root=tmp_path)
    assert not can_run("i686", machine="aarch64", binfmt_root=tmp_path), (
        "an x86_64 handler does not run i686 — 1.16.1 and 1.21.1 would be "
        "selected on a machine that cannot execute them"
    )

    (tmp_path / "qemu-x86_64").write_text("disabled\ninterpreter /usr/bin/qemu-x86_64\n")
    assert not can_run("x86_64", machine="aarch64", binfmt_root=tmp_path), (
        "a registered-but-disabled handler is a file that exists and an "
        "interpreter that will not run"
    )

    assert can_run("i686", machine="x86_64", binfmt_root=tmp_path), (
        "an x86_64 kernel runs both arches natively, handler or not"
    )


# No test asserts that every matrix arch is mapped, and that is deliberate:
# one was written here and deleted, because it CANNOT FAIL. `_MATRIX_PARAMS`
# indexes `QEMU_HANDLER` at import, so an unmapped arch raises KeyError
# during collection and the module never reaches its own guard — verified by
# mutating the mapping and watching `KeyError: 'i686'` replace the assertion.
# The collection error is the enforcement; a test shadowing it would only
# report green forever.


# ─── the interpreter prerequisite: network-free, never skipped ─────────────


def test_a_missing_interpreter_is_named_as_a_dependency_not_an_errno(tmp_path):
    """`require_interpreter` must name the package, the command and the doc.

    This is the FIRST thing a developer hits on a non-x86_64 box, and the raw
    symptom — `Exec format error` from execve on a file they did not know was
    x86_64 — is a twenty-minute detour that ends in an apt install. So the
    message is the deliverable, and each clause below is asserted separately:
    a message that raises the right type while omitting the fix has failed at
    the only job it has. Driven through an injected machine and an EMPTY
    binfmt root so it runs identically on the aarch64 dev VM (where the
    handler is now installed) and on x86_64 CI (where the native
    short-circuit would otherwise make this unreachable).
    """
    with pytest.raises(BusyBoxUnavailableError) as excinfo:
        require_interpreter(machine="aarch64", binfmt_root=tmp_path)
    message = str(excinfo.value)

    assert "qemu-user-static" in message, "the missing dependency must be named"
    assert "apt install qemu-user-static" in message, (
        "naming the package without the command leaves the reader to guess "
        "the packaging system — say the line they should run"
    )
    assert "apt update" in message, (
        "the install 404s on a stale index, which reads as 'no such package' "
        "— the refresh is part of the instruction, not decoration"
    )
    assert "aarch64" in message, "the host's own arch must be quoted back"
    for handler in ("qemu-x86_64", "qemu-i386"):
        assert handler in message, (
            f"{handler} is missing too — a message naming only one of the two "
            f"handlers sends the reader back for a second round"
        )


def test_a_registered_interpreter_is_silent_and_a_disabled_one_is_not(tmp_path):
    """Negative control: `require_interpreter` that always raised would pass the above.

    And the `disabled` half, which is the failure mode with no symptom of its
    own: the handler file exists, so a presence check calls the host capable,
    and the tier then dies in execve on a machine whose interpreter is one
    `echo 1` away from working.
    """
    for handler in QEMU_HANDLER.values():
        (tmp_path / handler).write_text(f"enabled\ninterpreter /usr/bin/{handler}\n")
    require_interpreter(machine="aarch64", binfmt_root=tmp_path)  # silent

    (tmp_path / "qemu-i386").write_text("disabled\ninterpreter /usr/bin/qemu-i386\n")
    with pytest.raises(BusyBoxUnavailableError, match="qemu-i386"):
        require_interpreter(machine="aarch64", binfmt_root=tmp_path)

    # Narrowing to a runnable arch must not trip over a sibling's disabled
    # handler: a caller that only ever runs x86_64 artifacts is unaffected by
    # a broken i686 registration, and saying otherwise would send it to fix
    # something it does not use. Silence here is the assertion.
    require_interpreter("x86_64", machine="aarch64", binfmt_root=tmp_path)


def test_no_arguments_covers_every_arch_the_matrix_declares(tmp_path):
    """The no-argument call is the session-fixture form; it must not check only one arch.

    `require_interpreter()` covering just x86_64 would let a session start on a
    box with `qemu-x86_64` and no `qemu-i386`, declare the prerequisite met,
    and then fail the two i686 entries in execve — the exact failure the
    function exists to pre-empt. Asserted by supplying x86_64 alone and
    demanding the i686 gap still be reported.
    """
    (tmp_path / "qemu-x86_64").write_text("enabled\ninterpreter /usr/bin/qemu-x86_64\n")

    with pytest.raises(BusyBoxUnavailableError, match="qemu-i386") as excinfo:
        require_interpreter(machine="aarch64", binfmt_root=tmp_path)

    assert "qemu-x86_64" not in str(excinfo.value), (
        "only the arches that cannot run may be reported — listing a working "
        "handler as missing sends the reader to fix something that is fine"
    )


# ─── pin bookkeeping: network-free, never skipped ──────────────────────────
#
# `_verify` is reachable from the marked tier only, so on a machine that
# cannot execute the artifacts — and in any `-m 'not busybox'` lane — nothing
# below would otherwise run it at all. These drive it directly.


def test_every_matrix_entry_is_pinned():
    """A sixth entry must not ship unpinned, even though `_verify` tolerates it.

    Deliberately stricter than the runtime rule, and the two do not conflict.
    `_verify` treats a missing pin as 'not yet pinned' so that ADDING an entry
    works locally before its hash is known — you add the matrix row, fetch it
    once, and write down what arrived. This guard is about the COMMITTED state:
    once the entry is in the tree, an absent pin means CI re-derives its trust
    from busybox.net on every cold cache, which is the one thing the pin file
    exists to stop. Equality both ways, so a stale pin for a removed entry is
    caught too.
    """
    pinned = set(json.loads(busybox._PINS.read_text()))
    declared = {release.filename for release in BUSYBOX_MATRIX}
    assert pinned == declared, (
        f"pin file and matrix disagree — unpinned entries: {sorted(declared - pinned)}; "
        f"stale pins: {sorted(pinned - declared)}"
    )


def test_a_wrong_pin_refuses_the_artifact(tmp_path, monkeypatch):
    """The hash layer must fire, and its message must send the reader to the banner.

    Drives `_verify` against a stand-in file with a pin file that disagrees,
    so it runs on every machine — the marked tier cannot cover this where it
    skips. The message assertion is the point as much as the raise: a hash
    mismatch is not a verdict on the artifact, it is an instruction to go and
    ask the behavioural gate (see the module docstring on layer ordering).
    """
    release = BUSYBOX_MATRIX[0]
    artifact = tmp_path / release.filename
    artifact.write_bytes(b"whatever bytes")
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({release.filename: "00" * 32}))
    monkeypatch.setattr(busybox, "_PINS", pins)

    with pytest.raises(BusyBoxUnavailableError, match="hash mismatch") as excinfo:
        busybox._verify(release, artifact)

    message = str(excinfo.value)
    assert "00" * 32 in message, "the pinned value must be shown so it can be compared"
    assert busybox._sha256(artifact) in message, "the actual value must be shown too"
    assert f"banner still reports v{release.version}" in message, (
        "a mismatch must defer to the behavioural gate, not adjudicate on its own"
    )


def test_a_matching_pin_passes_and_a_missing_one_is_tolerated(tmp_path, monkeypatch):
    """Negative control for the guard above, and the 'not yet pinned' rule.

    Without this, `_verify` raising unconditionally would satisfy the mismatch
    test, and the 'add an entry before you know its hash' path — the one the
    recording step depends on — would be untested.
    """
    release = BUSYBOX_MATRIX[0]
    artifact = tmp_path / release.filename
    artifact.write_bytes(b"whatever bytes")
    pins = tmp_path / "pins.json"
    monkeypatch.setattr(busybox, "_PINS", pins)

    pins.write_text(json.dumps({release.filename: busybox._sha256(artifact)}))
    busybox._verify(release, artifact)  # matching pin: silent

    pins.write_text(json.dumps({}))
    busybox._verify(release, artifact)  # unpinned: tolerated, per the docstring


def test_the_cache_override_is_a_declared_harness_opt_in(tmp_path, monkeypatch):
    """The override must be declared, or it is stripped and does nothing.

    `tests/conftest.py` drops every `OTTO_*` name absent from AMBIENT_OPT_INS
    at import time, so an undeclared override is not an error — it is a knob
    that silently does nothing while `cache_dir()` keeps returning the real
    cache (issue #192's shape). Both halves are asserted: the declaration, and
    that `cache_dir` actually reads it. The declaration is what stops the
    strip; reading through `ambient()` is what makes a future undeclaration
    raise here instead of going quiet.
    """
    assert "OTTO_BUSYBOX_CACHE" in AMBIENT_OPT_INS, (
        "undeclared, so tests/conftest.py strips it before any test runs and "
        "`make busybox-cache` would silently write the real cache"
    )
    monkeypatch.setenv("OTTO_BUSYBOX_CACHE", str(tmp_path))
    assert cache_dir() == tmp_path
    monkeypatch.delenv("OTTO_BUSYBOX_CACHE")
    assert cache_dir() == Path.home() / ".cache" / "otto" / "busybox"


# ─── fetch mechanics: no network, no busybox.net ───────────────────────────
#
# These stub the HTTP transport, which must never happen for the VERSION guard
# above and would be dishonest there — a stub proves the fetch handles bytes it
# was handed, not that the artifact is what it claims. Here the claim
# under test IS the fetch's own failure and publish handling, so a stub is the
# only way to reach paths a healthy busybox.net never produces.


class _FakeResponse:
    """Minimal urlopen stand-in: a context manager with a `read()`."""

    def __init__(self, body: "bytes | None", error: "Exception | None" = None) -> None:
        self._body = body
        self._error = error

    def read(self) -> bytes:
        if self._error is not None:
            raise self._error
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _stub_transport(monkeypatch, body=None, error=None):
    monkeypatch.setattr(
        busybox.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _FakeResponse(body, error),
    )


def _stub_attempts(monkeypatch, outcomes):
    """Stub the transport with a per-attempt script, and record what it was asked.

    *outcomes* is one entry per attempt: an Exception to raise, or bytes to
    return. Returns the list the stub appends to, so a test can assert HOW
    MANY attempts happened — the number is the claim under test for a retry,
    and a stub that answers identically forever cannot express it.

    Past the end of the script the LAST outcome repeats, deliberately. Raising
    IndexError there would make every "must not retry" guard fail by crashing
    the harness on attempt two, which looks like a red and is not one: the
    assertion that names the defect would never run. Repeating means an
    unwanted retry is reported as `saw [1, 2, 3]` by the guard itself.
    """
    seen: "list[int]" = []

    def urlopen(*_a, **_kw):
        outcome = outcomes[min(len(seen), len(outcomes) - 1)]
        seen.append(len(seen) + 1)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(busybox.urllib.request, "urlopen", urlopen)
    return seen


def _record_sleeps(monkeypatch):
    """Replace the backoff sleep with a recorder. Tests must never really sleep."""
    slept: "list[float]" = []
    monkeypatch.setattr(busybox.time, "sleep", slept.append)
    return slept


@contextlib.contextmanager
def _closing_http_errors(*codes: int) -> "Iterator[list[urllib.error.HTTPError]]":
    """Build HTTPErrors for the retry scripts, and CLOSE them on the way out.

    `urllib.error.HTTPError` is a file-like object, not a plain exception:
    `urllib.response.addbase` subclasses `tempfile._TemporaryFileWrapper`, so
    every instance carries a temp-file closer. Left unclosed, that closer emits
    a `ResourceWarning` when the collector eventually reaches it — and pytest
    reports an unraisable warning against whatever test happens to be running
    at that moment, not the one that leaked.

    That is not hypothetical. Three errors built here (404, 503, 503) reddened
    `tests/unit/config/test_settings_path_anchoring.py` on every CI lane from
    3.11 up, naming a test that has nothing to do with HTTP. CPython 3.10 —
    this machine's interpreter, and the one lane that stayed green — does not
    warn at all, so the whole class of defect is invisible to a local run.

    Closing is done in a `finally` so a failing assertion inside the block
    still cleans up; an escaping exception would otherwise reintroduce the leak
    on exactly the runs that are already reporting a failure.
    """
    errors = [
        urllib.error.HTTPError(BUSYBOX_MATRIX[0].url, code, "nope", hdrs=None, fp=None)
        for code in codes
    ]
    try:
        yield errors
    finally:
        for error in errors:
            error.close()


def test_the_error_helper_closes_every_error_it_builds():
    """Guard the close, because the leak it prevents surfaces under someone else's name.

    Asserted through `.closed` rather than by provoking a `ResourceWarning`:
    the warning only exists on 3.11+, so a warning-based guard would be inert
    on this machine's 3.10 — green for the wrong reason, in the environment
    where it would actually be run by hand. `.closed` reports the same fact on
    every supported version.

    The first assertion is what stops the second from being vacuous: "all
    closed" is trivially true of an empty list, and would also pass if the
    helper closed its errors immediately and handed back corpses the retry
    script could never raise.
    """
    with _closing_http_errors(503, 404) as errors:
        assert [error.code for error in errors] == [503, 404], (
            "the helper must hand back live, usable errors in the order asked for"
        )
        assert not any(error.closed for error in errors), (
            "the errors must still be open inside the block, or the retry "
            "scripts below are raising already-closed objects"
        )

    assert all(error.closed for error in errors), (
        "the helper leaked an unclosed HTTPError. urllib's HTTPError is a "
        "tempfile wrapper, so the collector reports it as an unraisable "
        "ResourceWarning against an unrelated test on Python 3.11+"
    )


def test_a_truncated_download_still_names_the_recovery(tmp_path, monkeypatch):
    """`IncompleteRead` is not an OSError, and the handler used to miss it.

    A short read against a declared Content-Length raises
    `http.client.IncompleteRead`, which derives from HTTPException — NOT from
    OSError or URLError. Uncaught, it escapes as itself: the caller gets a
    bare 'IncompleteRead(...)' instead of the instructions for priming the
    cache, which is precisely the failure that needs them. Asserted through
    the exception type the stdlib really raises, not a hand-rolled stand-in.
    """
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    _record_sleeps(monkeypatch)
    _stub_transport(monkeypatch, error=http.client.IncompleteRead(b"half a binary"))

    with pytest.raises(BusyBoxUnavailableError, match="make busybox-cache") as excinfo:
        busybox._fetch(release, target)

    assert release.url in str(excinfo.value), "the failing URL must be named"
    assert not target.exists(), "a failed fetch must publish nothing"
    # No `.part` claim here. `IncompleteRead` comes out of `resp.read()`, so
    # `tmp.write_bytes` never runs and there is nothing for the cleanup to
    # remove: an `iterdir() == []` on this path is satisfied by a `_fetch` with
    # no cleanup at all. It was here, and it was inert — deleting the unlink
    # from `_fetch` left the module at 26 passed. The claim now lives in
    # `test_a_write_that_fails_after_creating_the_part_removes_it`, which
    # creates the file first.


def test_a_write_that_fails_after_creating_the_part_removes_it(tmp_path, monkeypatch):
    """The cleanup's only reachable trigger, driven for the first time.

    Every other stub in this file raises from `read()`, i.e. BEFORE
    `tmp.write_bytes` is reached, so no test created a `.part` at all and the
    `tmp.unlink(missing_ok=True)` in `_fetch`'s handler was guarded by nothing
    — deleting it left the module green at 26 passed. A left-behind `.part` is
    not cosmetic: `busybox_binary` decides "already cached" by `target.exists()`
    and the debris sits next to it in a user-owned `~/.cache/otto/busybox`
    forever, one per failed fetch per arch.

    A partial write that then fails is the real shape of this: `_fetch` wraps
    the download AND the write in one `try`, and ENOSPC arrives from
    `write_bytes` after bytes are already on disk. It is also the ONLY shape —
    a successful write breaks the loop, and a local `OSError` is not transient
    (`_is_transient`), so a `.part` can exist only on the final, fatal attempt.
    The unlink sitting inside the loop rather than after it is therefore
    defensive against a future transient-after-write case, not load-bearing
    today; the earlier claim that this guard covered the between-retries case
    was wrong in both directions.
    """
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    part = tmp_path / f"{release.filename}.part"
    _record_sleeps(monkeypatch)
    _stub_transport(monkeypatch, body=b"#!/bin/false\n")

    real_write_bytes = Path.write_bytes
    existed: "list[bool]" = []

    def write_then_fail(self, data):
        real_write_bytes(self, data)
        existed.append(self == part and self.exists())
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", write_then_fail)

    with pytest.raises(BusyBoxUnavailableError, match="after 1 attempt"):
        busybox._fetch(release, target)

    # Premise first: without it this is another "did nothing" pass.
    assert existed == [True], (
        f"the stub must have created {part.name} before failing, or the cleanup "
        f"below is asserted against a file that never existed (saw {existed})"
    )
    assert not part.exists(), (
        "the failed attempt left its .part behind — `_fetch`'s handler must "
        "unlink it, or a user-owned cache accumulates one per failed fetch"
    )
    assert not target.exists(), "and a failed fetch must publish nothing"


def test_the_partial_file_is_named_for_the_whole_artifact(tmp_path, monkeypatch):
    """`with_suffix` would eat the patch digit and the arch, colliding under -n auto.

    `busybox-1.16.1-i686` has no extension — its dots are version separators —
    so `with_suffix('.part')` yields `busybox-1.16.part`, a name shared by
    every arch of every 1.16.x entry. Two xdist workers fetching two arches of
    one version would then write one temp file. Observed by recording what the
    fetch actually writes, rather than by re-deriving the name here.
    """
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    written: "list[Path]" = []
    real_write_bytes = Path.write_bytes

    def spy(self, data):
        written.append(self)
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", spy)
    _stub_transport(monkeypatch, body=b"#!/bin/false\n")

    busybox._fetch(release, target)

    assert written == [tmp_path / f"{release.filename}.part"], (
        f"the temp file must carry the whole artifact name, not {written[0].name!r}"
    )


def test_the_published_artifact_is_executable(tmp_path, monkeypatch):
    """The publish path's actual product: a runnable file at the cache path."""
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    _stub_transport(monkeypatch, body=b"#!/bin/false\n")

    busybox._fetch(release, target)

    assert target.exists(), "a complete fetch must publish"
    assert target.stat().st_mode & stat.S_IXUSR, "a published artifact must be runnable"


def test_a_failed_chmod_publishes_nothing(tmp_path, monkeypatch):
    """Mode before publish, so no window caches a complete non-executable artifact.

    With the chmod after `replace`, an interruption between the two leaves an
    artifact that is cached and hash-VALID but not executable; the next run
    passes `_verify`, fails in execve with EACCES, and `probe_banner` reports
    it as a missing qemu-user-static — a diagnosis pointing at the wrong
    machine entirely. Ordering is pinned by making the chmod fail and
    asserting nothing was published: with the old order the file is already
    there by then.
    """
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    _stub_transport(monkeypatch, body=b"#!/bin/false\n")

    def boom(*_a, **_kw):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "chmod", boom)

    with pytest.raises(PermissionError):
        busybox._fetch(release, target)

    assert not target.exists(), (
        "chmod failed, so nothing may be published — a cached artifact that is "
        "not executable fails the NEXT run, blaming the interpreter"
    )


# ─── the fetch retry: network-class only ───────────────────────────────────
#
# The tier is a BLOCKING CI job and a member of `report-failure.needs`, and it
# deliberately does not cache artifacts between runs, so one transient blip
# from busybox.net on main both reds the merge gate and auto-files an issue.
# These pin the retry that buys back the slack no-cache gave up.


def test_a_transient_failure_is_retried_and_a_later_attempt_publishes(tmp_path, monkeypatch):
    """Two 503s then a body must yield a published artifact, not a raise.

    The claim is the attempt COUNT as much as the outcome: a stub that answers
    identically forever cannot tell a retry from a lucky first try, so the
    transport is scripted per attempt and the script's consumption is asserted.
    """
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    _record_sleeps(monkeypatch)

    with _closing_http_errors(503, 503) as errors:
        attempts = _stub_attempts(monkeypatch, [*errors, b"#!/bin/false\n"])

        busybox._fetch(release, target)

        assert attempts == [1, 2, 3], f"expected three attempts, saw {attempts}"
        assert target.exists(), "the succeeding attempt must publish"
        assert target.stat().st_mode & stat.S_IXUSR, "and publish something runnable"


def test_a_deterministic_failure_is_not_retried(tmp_path, monkeypatch):
    """404 is a wrong URL. Three attempts at a wrong URL is 15s spent on a typo.

    This is the half the Makefile's `web-install` loop calls "fail fast on
    everything else", and the half a naive `for _ in range(3)` gets wrong. A
    404 here means the matrix entry's subdir or remote_name is wrong — 1.35.0
    is the live example, since upstream serves it as a plain `busybox` under a
    differently-shaped directory than the other four
    (`docs/superpowers/specs/2026-08-11-busybox-host-support-design.md`) — and
    retrying only delays the report.
    """
    release = BUSYBOX_MATRIX[0]
    slept = _record_sleeps(monkeypatch)

    with _closing_http_errors(404) as errors:
        attempts = _stub_attempts(monkeypatch, errors)

        with pytest.raises(BusyBoxUnavailableError, match="after 1 attempt"):
            busybox._fetch(release, tmp_path / release.filename)

        assert attempts == [1], f"a 404 must not be retried, saw {attempts} attempts"
        assert slept == [], "and must not sleep before failing"


def test_a_local_write_failure_is_not_retried(tmp_path, monkeypatch):
    """ENOSPC arrives as OSError, which `_fetch` catches for the `.part` write.

    `_fetch` wraps the download AND the write to disk in one `try`, so bare
    OSError is in the caught set for local reasons. Treating the whole set as
    network-class would sleep fifteen seconds before reporting a full disk.
    """
    release = BUSYBOX_MATRIX[0]
    slept = _record_sleeps(monkeypatch)
    attempts = _stub_attempts(monkeypatch, [OSError(28, "No space left on device")])

    with pytest.raises(BusyBoxUnavailableError, match="after 1 attempt"):
        busybox._fetch(release, tmp_path / release.filename)

    assert attempts == [1], f"a local OSError must not be retried, saw {attempts}"
    assert slept == [], "and must not sleep before failing"


def test_the_budget_and_backoff_are_the_makefiles(tmp_path, monkeypatch):
    """Three attempts, 5s then 10s — the same policy as `web-install`, not a second one.

    A discriminator, not a runaway guard: these are a deliberate policy shared
    with the Makefile's npm-ci loop, so drifting them apart is the defect. The
    sleeps are recorded rather than endured; a test that really slept 15s would
    be measuring patience.
    """
    release = BUSYBOX_MATRIX[0]
    slept = _record_sleeps(monkeypatch)
    boom = http.client.IncompleteRead(b"half")
    attempts = _stub_attempts(monkeypatch, [boom, boom, boom])

    with pytest.raises(BusyBoxUnavailableError, match="after 3 attempt"):
        busybox._fetch(release, tmp_path / release.filename)

    assert attempts == [1, 2, 3], f"the budget is three attempts, saw {attempts}"
    assert slept == [5, 10], (
        f"backoff must be 5s then 10s, matching the Makefile's web-install "
        f"retry loop — one retry convention in this repo, not two. Got {slept}"
    )


def test_the_fetch_budget_fits_inside_both_timeouts():
    """A retry that outlives the test timeout destroys the diagnostic it exists to give.

    The retry made the fetch's worst case 3 x `_FETCH_TIMEOUT_S` plus the
    backoff. At the original 60s that was 195s against pyproject's
    `timeout = 180`, so a stalled busybox.net would be SIGALRM'd as a bare
    "Timeout >180.0s" and the caller would never see the
    `after 3 attempt(s) … make busybox-cache` message — the retry silently
    breaking the failure path it was added to protect.

    A runaway bound, not a discriminator: it asserts headroom, not a
    stopwatch, and nothing here measures elapsed time. Both real bounds are
    READ from where they are configured rather than restated, so raising
    either the per-test timeout or the session cap relaxes this guard honestly
    and raising the retry budget without touching them reddens it.
    """
    attempts = len(busybox._RETRY_BACKOFF_S) + 1
    per_artifact = attempts * busybox._FETCH_TIMEOUT_S + sum(busybox._RETRY_BACKOFF_S)

    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    per_test = pyproject["tool"]["pytest"]["ini_options"]["timeout"]
    assert per_artifact * 2 <= per_test, (
        f"one artifact's worst-case fetch is {per_artifact}s against a {per_test}s "
        f"per-test timeout. Leave at least 2x headroom: pytest's SIGALRM would "
        f"otherwise replace the fetch's own actionable error with a bare timeout"
    )

    # `make busybox` wraps the whole session in `timeout <PYTEST_TIMEOUT>`, so
    # the arithmetic has to survive the matrix, not just one entry. Computed
    # for the worst case — every artifact stalling, one after another, in a
    # single worker — because `-n auto` can only improve on that.
    makefile = (_REPO_ROOT / "Makefile").read_text()
    session_cap = int(re.search(r"^PYTEST_TIMEOUT\s*:?=\s*(\d+)s", makefile, re.MULTILINE).group(1))
    worst_session = len(BUSYBOX_MATRIX) * per_artifact
    assert worst_session < session_cap, (
        f"{len(BUSYBOX_MATRIX)} artifacts x {per_artifact}s = {worst_session}s exceeds "
        f"`make busybox`'s {session_cap}s cap, which kills the run outright — no "
        f"per-test error, no JUnit report. Shrink the fetch budget or raise the cap"
    )


def test_the_rootfs_budget_fits_inside_the_per_test_timeout():
    """The rootfs tier's bounds are spent ON TOP of a fetch, in one test body.

    Same failure as the fetch budget above, one layer out. Building a root
    costs a cold-cache fetch plus a userns probe plus the applet install plus
    the scripts a test runs, and the tier's first version spent one 60s
    constant at every site: ~300s worst case against a 180s per-test timeout,
    so a wedged qemu or `unshare` would be SIGALRM'd as a bare
    "Timeout >180.0s" and the caller would never see the named
    RootfsUnavailableError the fixture exists to raise.

    Lives in the unit lane, not the `busybox` tier, on purpose: the tier runs
    only in `make busybox`, and a later task raising one of these constants
    should redden in the ordinary gates rather than in the one job nobody runs
    locally. Every term is READ from where it is configured, so raising the
    per-test timeout relaxes this honestly and raising a bound reddens it.

    EVERY BOUNDED CALL CONTRIBUTES ITS REAP. `_run_host` waits up to
    `_REAP_TIMEOUT_S` for output after SIGKILLing a timed-out group, so a call
    that times out costs its own bound PLUS that wait — and the timeout path is
    precisely the one this arithmetic exists to keep inside the SIGALRM window,
    so omitting it makes the guard wrong exactly when it matters. An earlier
    version of this sum left the reaps out and computed 115s where the module's
    own comment documented 135s; both cleared 180 at the time, which is how a
    20s discrepancy hides until someone raises a bound into the gap.
    """
    fetch = (len(busybox._RETRY_BACKOFF_S) + 1) * busybox._FETCH_TIMEOUT_S + sum(
        busybox._RETRY_BACKOFF_S
    )
    reap = busybox_rootfs._REAP_TIMEOUT_S
    probe = busybox_rootfs._USERNS_PROBE_TIMEOUT_S + reap
    build = busybox_rootfs._BUILD_TIMEOUT_S + reap
    scripts = busybox_rootfs._RUNS_PER_TEST_BUDGETED * (busybox_rootfs._RUN_TIMEOUT_S + reap)
    worst = fetch + probe + build + scripts

    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    per_test = pyproject["tool"]["pytest"]["ini_options"]["timeout"]

    # 1.25x rather than the fetch guard's 2x: this sum already CONTAINS that
    # guard's worst case as a term, so demanding another doubling would force
    # the rootfs bounds below the seconds a loaded emulated runner plausibly
    # needs. Headroom, not a stopwatch — nothing here measures elapsed time.
    assert worst * 1.25 <= per_test, (
        f"one rootfs test's worst case is {worst}s ({fetch}s cold fetch + "
        f"{probe}s userns probe + {build}s applet install + "
        f"{busybox_rootfs._RUNS_PER_TEST_BUDGETED} x "
        f"{busybox_rootfs._RUN_TIMEOUT_S + reap}s script, each non-fetch term "
        f"including the {reap}s post-SIGKILL reap) against a {per_test}s "
        f"per-test timeout. Leave 25% headroom: pytest's SIGALRM would "
        f"otherwise replace the fixture's actionable error with a bare timeout, "
        f"and CI runs this tier on a deliberately cold cache so the fetch term "
        f"is its normal case"
    )


def run_in_rootfs_calls(source: str) -> "dict[str, int]":
    """`run_in_rootfs(...)` call sites per ``test_`` function in *source*.

    AST rather than a regex, for the reason the sibling scanners in
    ``tests/unit/test_declared_harness_bounds.py`` blank comments first: this
    tier's docstrings discuss ``run_in_rootfs`` by name, and a textual count
    reads that prose as call sites. An annotated removal must stay counted-out,
    and a mention must never count in.

    Two limits, stated because a scanner whose coverage cannot be described is
    worse than none. Calls made from a module-level HELPER that a test invokes
    are attributed to the helper, not the test; and a call inside a loop counts
    once, since the budget models call sites and the arithmetic cannot see a
    loop bound either way. Both would under-count, so the honest reading of a
    green result is "no test names it more than N times", not "no test can
    exceed N scripts".
    """
    counts: dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        counts[node.name] = sum(
            1
            for sub in ast.walk(node)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "run_in_rootfs"
        )
    return counts


def test_no_rootfs_test_runs_more_scripts_than_its_budget_assumes():
    """`_RUNS_PER_TEST_BUDGETED` is a term in an arithmetic, so it must be true.

    The budget guard above reads that constant and reds when the CONSTANT
    moves. Nothing made it red when a third `run_in_rootfs` call simply
    appeared in a test — which is the way it will actually be violated, since
    the later tiers add call sites rather than edit bounds. That is an
    assumption wearing a constant's clothing, and the whole point of writing
    the sum down was to stop the timeout budget drifting silently.

    Discovered over the tree, not listed: a new file in the tier inherits this.
    """
    tier = TESTS_ROOT / "busybox"
    assert tier.is_dir(), "tests/busybox vanished — this scanner is reading nothing"

    budget = busybox_rootfs._RUNS_PER_TEST_BUDGETED
    seen = 0
    offenders: list[str] = []
    for module in sorted(tier.rglob("test_*.py")):
        for name, count in run_in_rootfs_calls(module.read_text()).items():
            seen += 1
            if count > budget:
                offenders.append(f"{module.name}::{name} makes {count}")

    assert seen, "no test function found under tests/busybox (scanner misparse?)"
    assert not offenders, (
        f"these tests call run_in_rootfs more than the {budget} times the "
        f"timeout arithmetic budgets for: {offenders}. Each script costs "
        f"_RUN_TIMEOUT_S + _REAP_TIMEOUT_S against a 180s per-test timeout, so "
        f"raise _RUNS_PER_TEST_BUDGETED and let "
        f"test_the_rootfs_budget_fits_inside_the_per_test_timeout re-check the "
        f"sum — do not just add the call"
    )


def test_the_rootfs_script_scanner_observes_red():
    """Positive control: the scanner seen counting, and seen NOT counting prose."""
    two = "def test_one():\n    run_in_rootfs(r, 'a')\n    run_in_rootfs(r, 'b')\n"
    assert run_in_rootfs_calls(two) == {"test_one": 2}
    assert run_in_rootfs_calls(two + "    run_in_rootfs(r, 'c')\n") == {"test_one": 3}

    # A docstring that discusses the helper is not a call site — the failure a
    # regex would have, in a tier whose guards all explain run_in_rootfs.
    prose = (
        'def test_two():\n    """Calls run_in_rootfs(r, x) twice."""\n    run_in_rootfs(r, "a")\n'
    )
    assert run_in_rootfs_calls(prose) == {"test_two": 1}
    assert run_in_rootfs_calls("def test_three():\n    # run_in_rootfs(r, 'x')\n    pass\n") == {
        "test_three": 0
    }

    # Non-test functions are not budgeted, and are the scanner's stated blind
    # spot rather than an oversight.
    assert run_in_rootfs_calls("def helper():\n    run_in_rootfs(r, 'a')\n") == {}


# ─── shipped error text must cite things that exist ────────────────────────


def test_the_recovery_instructions_name_a_real_doc_and_a_real_make_target(tmp_path, monkeypatch):
    """A recovery instruction pointing at nothing costs more than none at all.

    Both citations in this fixture's error text were written before their
    targets existed — `make busybox-cache` in the fetch failure and
    `docs/guide/hosts/busybox.md` in the exec failure. That is a normal way to
    write a plan and a terrible way to leave a tree: the reader arrives at the
    one moment the instruction matters, follows it, and finds nothing there.

    The citations are EXTRACTED from the messages the code really raises, not
    restated here. Restating them would only assert that this test and the
    fixture agree; extracting them means renaming the doc or the target in the
    message, without creating it, reddens this test rather than shipping a
    dangling pointer. Generalises to any future citation of the same shape.
    """
    with pytest.raises(BusyBoxUnavailableError) as exec_failure:
        probe_banner(tmp_path / "busybox-was-never-here")
    cited_docs = re.findall(r"docs/\S+?\.md", str(exec_failure.value))
    assert cited_docs, f"the exec failure must send the reader somewhere: {exec_failure.value}"
    for doc in cited_docs:
        assert (_REPO_ROOT / doc).is_file(), (
            f"{doc} is cited by tests/_fixtures/busybox.py and does not exist"
        )

    # IncompleteRead is transient, so `_fetch` retries: record the backoff
    # rather than living through it, or this guard costs 15 wall-clock seconds
    # to read a string.
    _record_sleeps(monkeypatch)
    _stub_transport(monkeypatch, error=http.client.IncompleteRead(b"half a binary"))
    with pytest.raises(BusyBoxUnavailableError) as fetch_failure:
        busybox._fetch(BUSYBOX_MATRIX[0], tmp_path / "cache" / BUSYBOX_MATRIX[0].filename)
    cited_targets = re.findall(r"`make ([a-z0-9-]+)`", str(fetch_failure.value))
    assert cited_targets, f"the fetch failure must name a way to prime: {fetch_failure.value}"
    # Matched against the set of defined targets rather than with a per-target
    # `re.search`, so a failure reports the missing name instead of pasting the
    # whole Makefile into the traceback as a regex argument.
    makefile = (_REPO_ROOT / "Makefile").read_text()
    defined = set(re.findall(r"^([a-zA-Z0-9_.-]+):", makefile, re.MULTILINE))
    for target in cited_targets:
        assert target in defined, (
            f"`make {target}` is cited by tests/_fixtures/busybox.py but the "
            f"Makefile defines no such target"
        )


def test_the_docs_blast_radius_claim_matches_where_the_bytes_actually_run(tmp_path, monkeypatch):
    """The security section must describe the real execution location, not a nicer one.

    It did not. The page claimed the artifact "is executed only from a pytest
    temporary directory" while `busybox_binary` returns `cache_dir() /
    filename` and `probe_banner` execs it there — a persistent, user-owned
    `~/.cache/otto/busybox`. Wrong anywhere, but this sentence is the blast
    radius in the trust discussion for the one unsigned executable otto runs,
    so it is the sentence whose being wrong costs most.

    The guards added alongside it could not catch this: they check that a
    citation RESOLVES, not that a claim is TRUE. General claim-checking is not
    available, so this pins the specific fact the security argument rests on
    — where the bytes execute — in code, and requires the page to name that
    same location, derived rather than restated. Moving the cache then breaks
    the test rather than silently invalidating the paragraph.
    """
    release = BUSYBOX_MATRIX[0]
    monkeypatch.setenv("OTTO_BUSYBOX_CACHE", str(tmp_path))
    monkeypatch.setattr(busybox, "_PINS", tmp_path / "no-pins.json")  # unpinned: tolerated
    artifact = tmp_path / release.filename
    artifact.write_bytes(b"#!/bin/false\n")

    assert busybox_binary(release).parent == cache_dir(), (
        "the artifact is handed back from the cache directory — nothing stages "
        "it into a scratch dir first, and the docs must not say otherwise"
    )

    argv: "list[list[str]]" = []

    def spy(cmd, **kwargs):
        argv.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="BusyBox v0.0.0 ()\n", stderr="")

    monkeypatch.setattr(busybox.subprocess, "run", spy)
    probe_banner(artifact)
    assert argv == [[str(artifact)]], (
        "the cached path is executed IN PLACE; a copy step would change the "
        "blast radius the docs describe"
    )

    # `~/.cache/otto/busybox` as a reader would write it, derived from the
    # function that decides it rather than typed here — otherwise this asserts
    # only that the test and the docs agree with each other.
    monkeypatch.delenv("OTTO_BUSYBOX_CACHE")
    as_written = "~/" + cache_dir().relative_to(Path.home()).as_posix()
    page = (_REPO_ROOT / "docs/guide/hosts/busybox.md").read_text()
    # `split` on a missing separator returns the whole page, so a renamed
    # heading would silently widen the search to every paragraph — and
    # `~/.cache/otto/busybox` appears twice more on this page, so the widened
    # search PASSES while nothing checks the trust discussion at all. Assert
    # the anchor before scoping to it.
    assert "## Trust:" in page, (
        "docs/guide/hosts/busybox.md has no `## Trust:` heading — this guard "
        "scopes to that section, and without it would assert against the page"
    )
    trust_section = page.split("## Trust:")[-1]
    assert as_written in trust_section, (
        f"the trust section must name {as_written} — the persistent directory "
        f"the artifact really runs from — so its blast-radius claim can be checked"
    )


def test_the_busybox_doc_is_reachable_from_the_hosts_toctree():
    """An unreferenced page is a `-W` build failure, and `make docs` is not the task gate.

    Sphinx runs warnings-as-errors, so a page in the source tree that no
    toctree includes fails the docs build outright. `make docs` is not part of
    the per-task gate, so without this the cost of the omission is a red CI
    job on some later, unrelated commit (issue #178's shape exactly).
    """
    index = (_REPO_ROOT / "docs/guide/hosts/index.md").read_text()
    block = re.search(r"```\{toctree\}(.*?)```", index, re.DOTALL)
    assert block, "docs/guide/hosts/index.md has no toctree at all"
    assert "busybox" in block.group(1).split(), (
        "docs/guide/hosts/busybox.md is not listed in the hosts toctree — "
        "sphinx-build -W fails on a document included in no toctree"
    )
