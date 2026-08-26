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

import contextlib
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 only, otto's floor
    import tomli as tomllib

from typing_extensions import Self  # `typing.Self` is 3.11+; otto's floor is 3.10

from tests._ambient_env import AMBIENT_OPT_INS
from tests._fixtures import busybox
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
from tests._fixtures.paths import PROJECT_ROOT

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
                f"qemu-user-static (see docs/architecture/subsystems/busybox-bed.md)"
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
    `tmp.unlink(missing_ok=True)` in `_fetch` was guarded by nothing — deleting
    it left the module green at 26 passed. A left-behind `.part` is not
    cosmetic: `busybox_binary` decides "already cached" by `target.exists()`
    and the debris sits next to it in a user-owned `~/.cache/otto/busybox`
    forever. It used to be one file per artifact, truncated by whatever attempt
    came next; now that the temp name carries a per-call token (see
    `test_two_fetchers_of_one_release_do_not_share_a_temp_file`) nothing
    truncates it, so every failed fetch would orphan a fresh ~1 MB.

    A partial write that then fails is the real shape of this: `_fetch` wraps
    the download AND the write in one `try`, and ENOSPC arrives from
    `write_bytes` after bytes are already on disk. It is the only shape the
    RETRY path can produce — a successful write breaks the loop, and a local
    `OSError` is not transient (`_is_transient`), so a `.part` can exist there
    only on the final, fatal attempt. That is why the unlink is a `finally`
    around the whole body rather than a line in the retry handler: the handler
    cannot see the two remaining exits, a failing chmod (asserted by
    `test_a_failed_chmod_publishes_nothing`) and a Ctrl-C mid-download.
    """
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    _record_sleeps(monkeypatch)
    _stub_transport(monkeypatch, body=b"#!/bin/false\n")

    real_write_bytes = Path.write_bytes
    created: "list[Path]" = []

    def write_then_fail(self, data):
        real_write_bytes(self, data)
        created.append(self)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", write_then_fail)

    with pytest.raises(BusyBoxUnavailableError, match="after 1 attempt"):
        busybox._fetch(release, target)

    # Premise first: without it this is another "did nothing" pass. The path is
    # OBSERVED rather than re-derived, because the per-call token makes it
    # unguessable — a name written out here would be one no fetch ever uses,
    # and the cleanup assertion below would hold against a file that never was.
    assert len(created) == 1, f"one attempt, one write — saw {created}"
    assert created[0].name.startswith(f"{release.filename}."), (
        f"the stub must have created THIS release's .part before failing, or the "
        f"cleanup below is asserted against a file that never existed (saw {created})"
    )
    assert not created[0].exists(), (
        f"the failed attempt left {created[0].name} behind — `_fetch` must unlink "
        f"it, or a user-owned cache accumulates one per failed fetch"
    )
    assert not target.exists(), "and a failed fetch must publish nothing"


def test_the_partial_file_is_named_for_the_whole_artifact(tmp_path, monkeypatch):
    """`with_suffix` would eat the patch digit and the arch, colliding under -n auto.

    `busybox-1.16.1-i686` has no extension — its dots are version separators —
    so `with_suffix('.part')` yields `busybox-1.16.part`, a name shared by
    every arch of every 1.16.x entry. Two xdist workers fetching two arches of
    one version would then write one temp file. Observed by recording what the
    fetch actually writes, rather than by re-deriving the name here.

    A PREFIX now, not an equality, and that change is the half this guard was
    missing rather than a loosening. The whole artifact name is still ONE name,
    so fixing the two-arches collision left the two-workers-one-release
    collision wide open — see
    `test_two_fetchers_of_one_release_do_not_share_a_temp_file`, which is the
    flake this stopped one step short of. The per-call token that closed it
    goes on the END, so the artifact name stays at the front of the name a
    human reads in `ls`, and the file stays beside the target it publishes to,
    which is what keeps `replace` a same-filesystem rename.
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

    assert len(written) == 1, f"one fetch writes one temp file, not {written}"
    name = written[0].name
    assert name.startswith(f"{release.filename}."), (
        f"the temp file must carry the whole artifact name, not {name!r}"
    )
    assert name.endswith(".part"), (
        f"and must keep the .part suffix last, where a `*.part` sweep still sees it — not {name!r}"
    )
    assert written[0].parent == target.parent, (
        f"the temp file must sit beside its target, or `replace` stops being an "
        f"atomic rename — {written[0].parent} vs {target.parent}"
    )


# ─── two fetchers at once: the -n auto collision, forced deterministically ──
#
# `make busybox` is `pytest -m busybox` with no path, so it inherits addopts'
# `-n auto --dist loadgroup` and runs the tier on every core, against a
# deliberately cold cache in CI. Several workers therefore reach
# `busybox_binary` for ONE release at the same moment, all see
# `target.exists()` false, and all enter `_fetch`.
#
# The interleaving is forced by RE-ENTRANCY, not by threads. A thread pair has
# to be steered with events to be deterministic anyway, and adds a deadlock to
# the failure modes of a guard whose entire value is being trustworthy;
# nesting one fetch inside another's write suspends the outer call at exactly
# the instant that matters — its `.part` written, not yet published — with no
# scheduler in the loop. What that produces is a real interleaving: every
# statement of the inner fetch runs between two statements of the outer one.
#
# Being single-process is also what makes these honest about the fix they
# demand. A temp name keyed on `os.getpid()` would still collide here and
# would still red — correctly: two calls in one process are the same hazard as
# two processes, and two machines sharing an `OTTO_BUSYBOX_CACHE` do not have
# unique pids either. The claim is per-CALL uniqueness, so that is what is
# driven.


def _interleave_at_the_unpublished_part(monkeypatch, other_fetcher):
    """Run *other_fetcher* while the first fetch's `.part` is written but unpublished.

    Returns the list of paths `_fetch` writes to, in order, so a test can
    assert the two fetchers did not share one. *other_fetcher* is handed the
    first fetch's in-flight temp path — observed rather than passed in, since
    a per-call token makes it unguessable by design.
    """
    real_write_bytes = Path.write_bytes
    writes: "list[Path]" = []

    def spy(self, data):
        writes.append(self)
        first = len(writes) == 1
        result = real_write_bytes(self, data)
        if first:
            other_fetcher(self)
        return result

    monkeypatch.setattr(Path, "write_bytes", spy)
    return writes


def test_two_fetchers_of_one_release_do_not_share_a_temp_file(tmp_path, monkeypatch):
    """The CI flake: one fetcher's rename strands the other on its very next line.

    Both write one `.part`; the first to reach `tmp.replace(target)` RENAMES it
    away, and the second's next statement is
    `tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR)`, which raises
    FileNotFoundError on a file it had just finished writing. That is CI run
    31893627225's arm64 leg verbatim — no such file
    `~/.cache/otto/busybox/busybox-1.21.1-i686.part`, from
    `test_applet_enumeration_is_unavailable_on_the_oldest_row[1.21.1]` — and
    against a shared temp name this test reproduces it as that same
    FileNotFoundError rather than as an assertion, because the traceback is
    the claim.

    Publishing TWICE is not a defect and is deliberately not asserted against:
    `replace` is atomic and both fetchers downloaded the same URL, so the
    loser's rename changes nothing anyone can observe, and `_verify` hashes
    what was published afterwards. That is what makes unique names sufficient
    and a lock unnecessary. So the assertion is that neither fetcher can touch
    the other's file, and the completed publish is the proof it mattered.
    """
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    body = b"#!/bin/false\n"
    _stub_transport(monkeypatch, body=body)
    writes = _interleave_at_the_unpublished_part(
        monkeypatch, lambda _in_flight: busybox._fetch(release, target)
    )

    busybox._fetch(release, target)

    assert len(writes) == 2, f"both fetchers must have written a .part — saw {writes}"
    assert writes[0] != writes[1], (
        f"both fetchers wrote {writes[0].name} — one temp file with two owners. "
        f"The first to publish renames it away and the other dies in `tmp.stat()`"
    )
    assert target.read_bytes() == body, "the published artifact must be the fetched bytes"
    assert target.stat().st_mode & stat.S_IXUSR, "and must be runnable"
    leftovers = sorted(p.name for p in tmp_path.glob("*.part"))
    assert not leftovers, f"both fetchers must leave the cache clean, found {leftovers}"


def test_a_failing_fetcher_does_not_delete_another_ones_download(tmp_path, monkeypatch):
    """The same collision with the sign reversed: cleanup as cross-worker deletion.

    A shared temp name turns `_fetch`'s own hygiene into a weapon. One worker's
    fetch fails — a blip from busybox.net, a 404 on a new matrix entry — and
    the unlink that exists to keep ITS debris out of the cache removes a
    download another worker still has in flight, converting one worker's
    transient error into another's FileNotFoundError. The failing fetcher here
    never writes a byte, which is what makes the deletion unambiguous: the only
    `.part` in the cache is the other fetcher's.

    Asserted INSIDE the interleaved callback, at the instant the cleanup has
    run, rather than after the outer fetch returns. The stranded fetch dies in
    `tmp.stat()` on its way out, so an assertion placed after it would never be
    reached — it would report the symptom the sibling test above already owns
    instead of this cause.
    """
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    body = b"#!/bin/false\n"
    _record_sleeps(monkeypatch)
    attempts = _stub_attempts(monkeypatch, [body, OSError(28, "No space left on device")])
    survived: "list[bool]" = []

    def failing_fetcher(in_flight):
        with pytest.raises(BusyBoxUnavailableError, match="after 1 attempt"):
            busybox._fetch(release, target)
        assert in_flight.exists(), (
            f"the failed fetch deleted {in_flight.name}, a file it never wrote — "
            f"that .part belongs to a fetch still in flight, and removing it makes "
            f"one worker's transient error crash a different worker"
        )
        survived.append(True)

    writes = _interleave_at_the_unpublished_part(monkeypatch, failing_fetcher)

    busybox._fetch(release, target)

    assert survived == [True], "the interleaved failing fetch never ran — nothing was tested"
    assert attempts == [1, 2], f"one attempt each, the second fatal — saw {attempts}"
    assert len(writes) == 1, f"only the surviving fetcher writes bytes here — saw {writes}"
    assert target.read_bytes() == body, "the surviving fetcher must still publish"


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
    leftovers = sorted(p.name for p in tmp_path.glob("*.part"))
    assert not leftovers, (
        f"and the failed publish must not leave {leftovers} behind. This exit is "
        f"outside the retry handler, so only `_fetch`'s `finally` covers it — and "
        f"a per-call temp name has nothing to truncate it on the next attempt"
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
    monkeypatch.setenv("OTTO_BUSYBOX_SOURCE", "upstream")
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


# ─── two sources, one pin: the release mirror first, busybox.net behind it ──
#
# Issue #261's remaining half. The cache keeps a WARM lane off the network, but
# a cold cache — a new runner image, a pin change, a fresh clone — still went
# to busybox.net alone, and busybox.net alone is what was down. The five
# artifacts are re-published as assets of the GitHub release
# `ci-assets-busybox-1` (bytes fetched from upstream and verified against the
# same pins), so a consuming lane has a source on the same host as the runner.
# `_verify` is unchanged and runs on every fetch whatever the source, so the
# mirror can serve a different byte and it is refused exactly as upstream's
# would be — the pin, not the host, is what is trusted.
#
# Drift detection is the exception, by policy: reading upstream's own bytes is
# the only way to notice upstream rebuilding an artifact in place, because a
# mirror in front of it would report "still matches" about bytes upstream no
# longer serves. It forces `OTTO_BUSYBOX_SOURCE=upstream`.
#
# It runs NIGHTLY, not on push, and that placement is the point. Upstream drift
# cannot affect a single byte this repo tests or ships: every consumer fetches
# mirror-first and `_verify`s against `busybox_pins.json`, so the pins — not the
# host — decide what runs. Detection is therefore a MONITORING signal, and a
# monitoring signal enforced as a merge gate fails when a third party is down
# rather than when the change is bad. CI's `busybox` job carried that pin until
# 2026-08-26, when a busybox.net outage reddened main for a push that had
# nothing to do with it. Nightly still notices drift and opens an issue; it just
# cannot block anyone. Pinned below in three directions: not on the push gate,
# present in nightly, and never on a consuming lane.

_SOURCE_ENV = "OTTO_BUSYBOX_SOURCE"


def test_the_mirror_is_the_release_asset_for_the_cached_filename():
    """The asset name IS the cache filename, so one string names the byte in both places."""
    release = BUSYBOX_MATRIX[0]
    assert busybox._MIRROR_BASE == (
        "https://github.com/ludachrish3/otto-sh/releases/download/ci-assets-busybox-1"
    ), "the mirror base names the release the assets were uploaded to"
    assert release.mirror_url == f"{busybox._MIRROR_BASE}/{release.filename}"
    assert release.url.startswith("https://busybox.net/"), "upstream is still upstream"
    assert release.url_for("mirror") == release.mirror_url
    assert release.url_for("upstream") == release.url


def test_every_source_order_spends_exactly_the_retry_budget():
    """One host per attempt: the fallback rides the existing budget, it does not add one.

    `test_the_fetch_budget_fits_inside_both_timeouts` reasons from
    `len(_RETRY_BACKOFF_S) + 1` attempts; an order longer than that would
    fetch past the arithmetic, and a shorter one would leave a retry unspent.
    """
    attempts = len(busybox._RETRY_BACKOFF_S) + 1
    for policy, order in busybox._SOURCE_ORDERS.items():
        assert len(order) == attempts, (
            f"{policy!r} names {len(order)} hosts for {attempts} attempts"
        )
        assert set(order) <= {"mirror", "upstream"}, order


def test_the_default_order_is_mirror_then_upstream_then_mirror(tmp_path, monkeypatch):
    """Three attempts, three sources: the mirror twice, upstream between them.

    One retry budget (`test_the_fetch_budget_fits_inside_both_timeouts`), so
    the fallback rides the attempts that already exist rather than adding a
    second budget on top. A transient failure sleeps and moves to the next
    source; the URL sequence is the claim, not the count.
    """
    monkeypatch.delenv(_SOURCE_ENV, raising=False)
    release = BUSYBOX_MATRIX[0]
    _record_sleeps(monkeypatch)
    asked: "list[str]" = []

    def urlopen(url, *_a, **_kw):
        asked.append(url)
        raise http.client.IncompleteRead(b"half")

    monkeypatch.setattr(busybox.urllib.request, "urlopen", urlopen)

    with pytest.raises(BusyBoxUnavailableError, match="after 3 attempt") as excinfo:
        busybox._fetch(release, tmp_path / release.filename)

    assert asked == [release.mirror_url, release.url, release.mirror_url], asked
    assert release.mirror_url in str(excinfo.value)
    assert release.url in str(excinfo.value), "every source tried is named in the error"


def test_a_404_from_one_source_moves_to_the_next_without_sleeping(tmp_path, monkeypatch):
    """A missing asset is that SOURCE saying no, not the network — fall through at once.

    The old rule "a 404 is a typo, fail fast" still holds per source: it is
    not retried against the same host, and it costs no backoff. What it no
    longer does is end the fetch while another source remains untried.
    """
    monkeypatch.delenv(_SOURCE_ENV, raising=False)
    release = BUSYBOX_MATRIX[0]
    target = tmp_path / release.filename
    slept = _record_sleeps(monkeypatch)
    asked: "list[str]" = []

    with _closing_http_errors(404) as errors:

        def urlopen(url, *_a, **_kw):
            asked.append(url)
            if url == release.mirror_url:
                raise errors[0]
            return _FakeResponse(b"#!/bin/false\n")

        monkeypatch.setattr(busybox.urllib.request, "urlopen", urlopen)
        busybox._fetch(release, target)

    assert asked == [release.mirror_url, release.url], asked
    assert slept == [], "a deterministic refusal by one source must not sleep"
    assert target.exists(), "upstream's answer is published"


def test_a_source_that_said_no_is_not_asked_again(tmp_path, monkeypatch):
    """404 from the mirror, then 404 from upstream: the third slot is the mirror again — skip it.

    Two sources have both answered deterministically; a third attempt at
    either is the "three attempts at a typo" the fail-fast rule exists to
    refuse. The error reports both answers and stops at two.
    """
    monkeypatch.delenv(_SOURCE_ENV, raising=False)
    release = BUSYBOX_MATRIX[0]
    slept = _record_sleeps(monkeypatch)

    with _closing_http_errors(404, 404) as errors:
        attempts = _stub_attempts(monkeypatch, errors)
        with pytest.raises(BusyBoxUnavailableError, match="after 2 attempt") as excinfo:
            busybox._fetch(release, tmp_path / release.filename)

    assert attempts == [1, 2], attempts
    assert slept == []
    assert release.mirror_url in str(excinfo.value)
    assert release.url in str(excinfo.value)


def test_a_local_failure_does_not_move_to_another_source(tmp_path, monkeypatch):
    """ENOSPC is not a source's answer; asking a second host cannot free the disk."""
    monkeypatch.delenv(_SOURCE_ENV, raising=False)
    release = BUSYBOX_MATRIX[0]
    slept = _record_sleeps(monkeypatch)
    asked: "list[str]" = []

    def urlopen(url, *_a, **_kw):
        asked.append(url)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(busybox.urllib.request, "urlopen", urlopen)

    with pytest.raises(BusyBoxUnavailableError, match="after 1 attempt"):
        busybox._fetch(release, tmp_path / release.filename)

    assert asked == [release.mirror_url], "the mirror once, upstream never"
    assert slept == []


def test_a_transient_on_the_other_host_never_returns_to_a_host_that_refused(tmp_path, monkeypatch):
    """Mirror 404, upstream truncated: the third slot is the refused mirror — stop at two.

    Found in review: filtering only the deterministic path let a transient on
    upstream send attempt three back to the mirror that had already said no,
    costing a 10 s sleep and a request, and reporting the mirror's 404 as the
    cause of a fetch that upstream's flake had actually ended.
    """
    monkeypatch.delenv(_SOURCE_ENV, raising=False)
    release = BUSYBOX_MATRIX[0]
    slept = _record_sleeps(monkeypatch)
    asked: "list[str]" = []

    with _closing_http_errors(404) as errors:

        def urlopen(url, *_a, **_kw):
            asked.append(url)
            if url == release.mirror_url:
                raise errors[0]
            raise http.client.IncompleteRead(b"half")

        monkeypatch.setattr(busybox.urllib.request, "urlopen", urlopen)
        with pytest.raises(BusyBoxUnavailableError, match="after 2 attempt") as excinfo:
            busybox._fetch(release, tmp_path / release.filename)

    assert asked == [release.mirror_url, release.url], asked
    assert slept == [], "no host worth waiting for remained"
    assert "IncompleteRead" in str(excinfo.value), (
        "the cause named must be the last host asked (upstream's flake), not the mirror's 404"
    )


def test_upstream_only_never_asks_the_mirror(tmp_path, monkeypatch):
    """`OTTO_BUSYBOX_SOURCE=upstream` is the `busybox` job's policy.

    The detector must read upstream's bytes: a mirror in front of it would
    report "still matches" about a build upstream has since replaced.
    """
    monkeypatch.setenv(_SOURCE_ENV, "upstream")
    release = BUSYBOX_MATRIX[0]
    _record_sleeps(monkeypatch)
    asked: "list[str]" = []

    def urlopen(url, *_a, **_kw):
        asked.append(url)
        raise http.client.IncompleteRead(b"half")

    monkeypatch.setattr(busybox.urllib.request, "urlopen", urlopen)

    with pytest.raises(BusyBoxUnavailableError, match="after 3 attempt"):
        busybox._fetch(release, tmp_path / release.filename)

    assert asked == [release.url] * 3, asked


def test_an_unknown_source_policy_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv(_SOURCE_ENV, "fastest")
    release = BUSYBOX_MATRIX[0]
    _stub_transport(monkeypatch, body=b"#!/bin/false\n")

    with pytest.raises(BusyBoxUnavailableError, match="mirror-first") as excinfo:
        busybox._fetch(release, tmp_path / release.filename)
    assert "fastest" in str(excinfo.value)
    assert "upstream" in str(excinfo.value)


def test_the_source_policy_is_a_declared_harness_opt_in():
    """Undeclared, the root conftest strips it: the `busybox` job silently fetches mirror-first."""
    assert _SOURCE_ENV in AMBIENT_OPT_INS


def test_upstream_drift_detection_is_a_nightly_job_and_never_a_push_gate():
    """The detector reads upstream, nightly. No push gate may. Pinned three ways.

    The three assertions are one rule seen from three sides: reading upstream's
    bytes is the only way to see a rebuild-in-place, and it must never be able
    to fail a push, because the pins — not the host — decide what this repo
    runs. A busybox.net outage reddened main on 2026-08-26 for a push it had
    nothing to do with; that is the regression this pins shut.

    Asserted against `ci.yml` by NAME for the push half and by SEARCH for the
    nightly half, deliberately. The push half names the one job that carried
    the pin, so restoring it there reddens here. The nightly half asks only
    that SOME nightly job forces upstream and reports its failure, so renaming
    or restructuring that job moves the guard instead of breaking it.
    """
    ci = yaml.safe_load((_WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    for name, job in ci["jobs"].items():
        job_env = job.get("env", {}) or {}
        assert _SOURCE_ENV not in job_env, (
            f"ci.yml `{name}` sets {_SOURCE_ENV}={job_env[_SOURCE_ENV]!r}. NO job on the push "
            f"gate may pin the source: upstream drift cannot affect a byte this repo tests or "
            f"ships (every consumer verifies against busybox_pins.json), so pinning upstream "
            f"here only lets a third-party outage fail a push. Drift detection belongs in "
            f"nightly.yml, where it opens an issue instead of blocking main."
        )

    nightly = yaml.safe_load((_WORKFLOWS / "nightly.yml").read_text(encoding="utf-8"))
    detectors = [
        name
        for name, job in nightly["jobs"].items()
        if (job.get("env", {}) or {}).get(_SOURCE_ENV) == "upstream"
    ]
    assert detectors, (
        f"no nightly.yml job sets {_SOURCE_ENV}=upstream, so nothing in this repo ever reads "
        f"upstream's own bytes again — a rebuild-in-place at busybox.net would go unnoticed "
        f"forever, since every other lane fetches the mirror and reports 'still matches'"
    )
    reported = set(nightly["jobs"]["report-failure"]["needs"])
    missing = [name for name in detectors if name not in reported]
    assert not missing, (
        f"{missing} detect upstream drift but are absent from nightly's report-failure "
        f"`needs`, so the drift they find opens no issue and waits for someone to watch "
        f"the Actions tab — which is the whole reason it was safe to move off the gate"
    )

    for key in _unit_tree_jobs():
        workflow = yaml.safe_load((_WORKFLOWS / key[0]).read_text(encoding="utf-8"))
        job_env = workflow["jobs"][key[1]].get("env", {}) or {}
        assert _SOURCE_ENV not in job_env, (
            f"{key[0]} `{key[1]}` only consumes the artifacts; forcing {_SOURCE_ENV} there "
            f"puts busybox.net back on a default lane's critical path"
        )
    assert _unit_tree_jobs(), "premise: the allowlist no longer names a live consuming lane"


# ─── shipped error text must cite things that exist ────────────────────────


def test_the_recovery_instructions_name_a_real_doc_and_a_real_make_target(tmp_path, monkeypatch):
    """A recovery instruction pointing at nothing costs more than none at all.

    Both citations in this fixture's error text were written before their
    targets existed — `make busybox-cache` in the fetch failure and
    `docs/architecture/subsystems/busybox-bed.md` in the exec failure. That is a normal way to
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
    page = (_REPO_ROOT / "docs/architecture/subsystems/busybox-bed.md").read_text()
    # `split` on a missing separator returns the whole page, so a renamed
    # heading would silently widen the search to every paragraph — and
    # `~/.cache/otto/busybox` appears twice more on this page, so the widened
    # search PASSES while nothing checks the trust discussion at all. Assert
    # the anchor before scoping to it.
    assert "## Trust:" in page, (
        "docs/architecture/subsystems/busybox-bed.md has no `## Trust:` heading — this guard "
        "scopes to that section, and without it would assert against the page"
    )
    trust_section = page.split("## Trust:")[-1]
    assert as_written in trust_section, (
        f"the trust section must name {as_written} — the persistent directory "
        f"the artifact really runs from — so its blast-radius claim can be checked"
    )


def test_the_busybox_doc_is_reachable_from_the_architecture_toctree():
    """An unreferenced page is a `-W` build failure, and `make docs` is not the task gate.

    Sphinx runs warnings-as-errors, so a page in the source tree that no
    toctree includes fails the docs build outright. `make docs` is not part of
    the per-task gate, so without this the cost of the omission is a red CI
    job on some later, unrelated commit (issue #178's shape exactly).

    The page moved from `docs/guide/hosts/` to `docs/architecture/subsystems/`
    when the user guide's CLI section was restructured to mirror the command
    tree; the bed matrix is architecture, not CLI usage. The architecture index
    is reStructuredText, so this reads an RST directive rather than a MyST
    fenced block.
    """
    index = (_REPO_ROOT / "docs/architecture/index.rst").read_text()
    blocks = re.findall(r"\.\. toctree::(.*?)(?=\n\.\. |\Z)", index, re.DOTALL)
    assert blocks, "docs/architecture/index.rst has no toctree at all"
    assert any("subsystems/busybox-bed" in b.split() for b in blocks), (
        "docs/architecture/subsystems/busybox-bed.md is not listed in the "
        "architecture toctree — sphinx-build -W fails on a document included "
        "in no toctree"
    )


# ---------------------------------------------------------------------------
# The supply-chain split: which CI job may cache these artifacts, and which
# must not. Both halves are pinned, because the safety of the caching half
# rests entirely on the other half still going to the network.
# ---------------------------------------------------------------------------

_WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
_ARTIFACT_CACHE_PATH = "~/.cache/otto/busybox"
# The invocations that select `tests/unit/test_support_matrix.py`, whose
# `_run_conformance` spawns a real hermetic conformance run with BusyBox cells
# built from the artifacts. A job whose `run:` text carries one of these
# CONSUMES the artifacts and must cache them. This is an ALLOWLIST of literal
# spellings, not a model of what each Makefile target or nox session selects:
# a job that reaches the tree by some other spelling (`make coverage-hostless`,
# a bare `pytest tests/unit`) is NOT seen here and must be added as a
# deliberate line. `make stability-unit` is deliberately absent — it is
# `pytest -m concurrency`, four no-VM files that never touch the artifacts.
_UNIT_TREE_ENTRYPOINTS = (
    "nox -s tests_hostless-",
    "nox -s tests_unit_repeat",
)


def _workflow_jobs() -> "dict[tuple[str, str], list[dict]]":
    """Every job in every workflow file -> its steps, keyed by (workflow, job)."""
    jobs: "dict[tuple[str, str], list[dict]]" = {}
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job, spec in workflow["jobs"].items():
            jobs[(path.name, job)] = spec.get("steps", [])
    return jobs


def _runs(steps: "list[dict]") -> str:
    return "\n".join(str(s.get("run", "")) for s in steps)


def _cache_steps(steps: "list[dict]") -> "list[dict]":
    # Any cache action, not just `actions/cache`: the negative guard below has
    # to catch a `buildjet/cache` or similar landing in `busybox` as well.
    return [s for s in steps if "cache" in str(s.get("uses", ""))]


def _unit_tree_jobs() -> "list[tuple[str, str]]":
    return sorted(
        key
        for key, steps in _workflow_jobs().items()
        if any(entry in _runs(steps) for entry in _UNIT_TREE_ENTRYPOINTS)
    )


def test_every_entrypoint_spelling_still_names_a_live_job():
    """Premise for the rule below, per entry: a spelling that matches no job
    makes the rule quiet on the consumer it used to cover. Renaming
    `tests_unit_repeat` would drop `unit-repeat` from the parametrization and
    leave every remaining row green — the exact silent loss this file exists
    to refuse — so each spelling must find at least one job, and the two
    known consumers must be among them."""
    jobs = _workflow_jobs()
    for entry in _UNIT_TREE_ENTRYPOINTS:
        matched = sorted(key for key, steps in jobs.items() if entry in _runs(steps))
        assert matched, (
            f"entrypoint {entry!r} matches no job in any workflow — the lane it covered "
            f"was renamed or removed; update _UNIT_TREE_ENTRYPOINTS in the same diff"
        )
    consumers = _unit_tree_jobs()
    assert ("ci.yml", "tests") in consumers, consumers
    assert ("ci.yml", "unit-repeat") in consumers, consumers
    assert ("nightly.yml", "unit-matrix") in consumers, consumers


@pytest.mark.parametrize("workflow_job", _unit_tree_jobs(), ids="/".join)
def test_every_lane_that_runs_the_unit_tree_caches_the_artifacts_it_only_consumes(workflow_job):
    """A lane that only CONSUMES the artifacts must not depend on their source being up.

    Ten tests in `tests/unit/test_support_matrix.py` drive a real hermetic
    conformance run whose cells are built from these artifacts. Issue #261 is
    what it costs when the fetch fails: an SSL handshake timeout reddened a
    release-bump push that touched only the version. The first fix cached
    the `tests` job alone; `unit-repeat` runs the same tree and failed for
    the same reason on the next push (run 32897991866), and nightly's
    `unit-matrix` repeats it `nox_count` times per Python. The lane that
    VERIFIES the bytes (`busybox`) is the deliberate exception, guarded
    below; nightly's `conformance-hermetic` primes cold as a named step by
    choice and runs none of these spellings.

    Consumers are derived from each job's `run:` text against
    `_UNIT_TREE_ENTRYPOINTS` — so a job that spells one of those entrypoints
    is reported here by name, and a job that reaches the tree by a spelling
    the tuple does not carry is not (the tuple's comment says so).
    """
    workflow, job = workflow_job
    steps = _workflow_jobs()[workflow_job]
    caches = _cache_steps(steps)
    assert len(caches) == 1, (
        f"{workflow} `{job}` runs the unit tree and must cache the BusyBox artifacts it "
        f"consumes — found {len(caches)} cache step(s). A cold cache puts the artifact "
        f"source on this lane's critical path (issue #261)"
    )
    first_run = next(
        i
        for i, s in enumerate(steps)
        if any(e in str(s.get("run", "")) for e in _UNIT_TREE_ENTRYPOINTS)
    )
    assert steps.index(caches[0]) < first_run, (
        f"{workflow} `{job}`: the cache step sits AFTER the step that runs the tree, so "
        f"it restores nothing in time"
    )
    with_ = caches[0].get("with") or {}
    assert with_.get("path") == _ARTIFACT_CACHE_PATH, (
        f"{workflow} `{job}`: the cache must cover the artifact dir `cache_dir()` resolves "
        f"to; got {with_.get('path')!r}"
    )
    assert "busybox_pins.json" in str(with_.get("key", "")), (
        f"{workflow} `{job}`: the key must follow the pins, so a pin change misses; "
        f"got {with_.get('key')!r}"
    )
    assert "restore-keys" not in with_, (
        f"{workflow} `{job}`: a prefix restore would bring back the OLD bytes under the "
        f"SAME filename when a pin changes because upstream rebuilt in place -- and "
        f"busybox_binary only fetches when the target is ABSENT, so the stale file would "
        f"be kept and fail _verify until the cache expired"
    )


def test_the_busybox_lane_never_caches_the_artifacts_it_verifies():
    """The load-bearing half. Caching HERE would delete the event the pin exists to catch.

    That job re-fetches cold so an upstream in-place rebuild reddens on the
    push. A cache keyed on the pins would only ever miss when the pins change,
    i.e. it would skip the fetch in exactly the runs that could detect the
    rebuild -- keeping the pin file and deleting its purpose. The consuming
    lanes may cache precisely because this one does not, so this assertion is
    what makes those safe.
    """
    assert _cache_steps(_workflow_jobs()[("ci.yml", "busybox")]) == [], (
        "the busybox job caches its artifacts; it must re-fetch cold every run, because "
        "detection of an upstream in-place rebuild lives there and nowhere else"
    )


@pytest.fixture
def _fresh_preflight(monkeypatch):
    """`preflight` memoizes in MODULE state, so every guard here needs a clean slate.

    `monkeypatch.setattr` rather than mutating the live verdict: it records
    whatever the running session already had and restores it at teardown, so
    these guards cannot leave a poisoned verdict — or a spuriously satisfied
    one — behind for the rest of the process.
    """
    monkeypatch.setattr(busybox, "_PREFLIGHT", busybox._PreflightVerdict())


@pytest.mark.usefixtures("_fresh_preflight")
def test_preflight_on_a_warm_cache_opens_no_socket(tmp_path, monkeypatch):
    """The precondition must be free when there is nothing to fetch.

    This is what makes it safe to run from `busybox_binary` on every call and
    from `make busybox-preflight` before every tier run. The
    transport is replaced with something that RAISES rather than merely
    counted: a probe that slipped through would fail here by name instead of
    quietly succeeding against a stub. Read together with the cold-cache guard
    below, which proves the same call does reach the network when it should —
    neither claim is worth much alone, since a `preflight` that did nothing at
    all would satisfy this one.
    """
    monkeypatch.setenv("OTTO_BUSYBOX_CACHE", str(tmp_path))
    for release in BUSYBOX_MATRIX:
        (tmp_path / release.filename).write_bytes(b"already here")

    def explode(*_a, **_kw):
        raise AssertionError("a warm cache must not reach busybox.net")

    monkeypatch.setattr(busybox.urllib.request, "urlopen", explode)

    busybox.preflight()


@pytest.mark.usefixtures("_fresh_preflight")
def test_preflight_probes_exactly_one_artifact_not_the_whole_matrix(tmp_path, monkeypatch):
    """ONE probe is the entire point: five would reproduce the bug this closes.

    A dead source costs the full per-artifact retry budget EACH TIME it is
    asked, so a precondition that walked the matrix would spend
    `len(BUSYBOX_MATRIX) x 60 = 300s` learning something one 60s answer
    settles — and 300s is past the 180s per-test SIGALRM that turned CI run
    32888520702's real error into a bare timeout.

    The assertion is on the URL LIST, not on a count, so a probe that asked for
    the wrong entry is reported as the wrong entry rather than as a passing 1.
    """
    monkeypatch.setenv("OTTO_BUSYBOX_CACHE", str(tmp_path))
    asked: "list[str]" = []

    def urlopen(url, *_a, **_kw):
        asked.append(url)
        return _FakeResponse(b"#!/bin/false\n")

    monkeypatch.setattr(busybox.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(busybox, "_verify", lambda *_a, **_kw: None)

    busybox.preflight()

    assert asked == [BUSYBOX_MATRIX[0].mirror_url], (
        f"preflight must prove the SOURCE answers with a single probe; it asked for "
        f"{len(asked)} artifact(s): {asked}"
    )


@pytest.mark.usefixtures("_fresh_preflight")
def test_preflight_reraises_its_verdict_without_a_second_probe(tmp_path, monkeypatch):
    """A whole-repo `pytest` collects BOTH lanes, so both hooks call this.

    Without the memo that is two independent 60s stalls against a dead mirror
    in one process, and the second one proves nothing the first did not. The
    attempt script is asserted before AND after the second call, which is what
    makes "remembered" distinguishable from "measured again and agreed".
    """
    monkeypatch.setenv("OTTO_BUSYBOX_CACHE", str(tmp_path))
    _record_sleeps(monkeypatch)

    with _closing_http_errors(503, 503, 503) as errors:
        attempts = _stub_attempts(monkeypatch, errors)

        with pytest.raises(BusyBoxUnavailableError, match="make busybox-cache") as first:
            busybox.preflight()
        assert attempts == [1, 2, 3], f"the first probe spends the retry budget, saw {attempts}"

        with pytest.raises(BusyBoxUnavailableError) as second:
            busybox.preflight()

        assert attempts == [1, 2, 3], (
            f"the verdict must be REMEMBERED, not re-measured; the second call spent "
            f"another budget and the attempt log grew to {attempts}"
        )
        assert second.value is first.value, "and it must re-raise the verdict it recorded"


@pytest.mark.usefixtures("_fresh_preflight")
def test_a_hash_mismatch_does_not_poison_the_preflight_verdict(tmp_path, monkeypatch):
    """A stale pin is not an unreachable source, and must not be reported as one.

    `_verify` sits OUTSIDE the memo deliberately. If a mismatch were recorded,
    every later caller in the process would be told artifact 0's pin is stale
    no matter which artifact it asked about — a wrong diagnosis for a
    security-relevant check. The proof is that the SECOND call probes a
    DIFFERENT url: the memo did not fire, and the first artifact is now cached
    so `missing` has advanced.
    """
    monkeypatch.setenv("OTTO_BUSYBOX_CACHE", str(tmp_path))
    asked: "list[str]" = []

    def urlopen(url, *_a, **_kw):
        asked.append(url)
        return _FakeResponse(b"not the pinned bytes\n")

    monkeypatch.setattr(busybox.urllib.request, "urlopen", urlopen)

    with pytest.raises(BusyBoxUnavailableError, match="hash mismatch"):
        busybox.preflight()
    with pytest.raises(BusyBoxUnavailableError, match="hash mismatch"):
        busybox.preflight()

    assert asked == [BUSYBOX_MATRIX[0].mirror_url, BUSYBOX_MATRIX[1].mirror_url], (
        f"a mismatch must leave the verdict unrecorded, so the next call advances to "
        f"the next missing artifact; saw {asked}"
    )


# ─── the precondition lives at the consumer, so nothing fires when nothing runs ─
#
# Issue #264. `preflight()` used to be hung off `pytest_collection_finish` in
# both artifact trees' conftests, keyed on items surviving collection. That
# keying reasoned correctly about DESELECTION and not at all about
# `--collect-only`, which collects items, executes none of them, and fires the
# hook regardless — so every child pytest that shells out to enumerate the
# suite inherited a busybox.net dependency on a cold cache, and a raise from a
# collection hook is INTERNALERROR (exit 3), burying the one message the probe
# exists to deliver. `session.items` being non-empty proves items were
# COLLECTED, never that they will EXECUTE (issue #196's shape).
#
# The precondition now lives in `busybox_binary`, the consumer. It fires at the
# moment an artifact is about to be fetched — inside a running test — so a dead
# source is a test failure carrying the priming instructions, and a run that
# fetches nothing reaches nothing, by construction rather than by guard.

_COLLECT_ONLY_ARGS = ("--collect-only", "-q", "--no-cov", "-p", "no:cacheprovider", "-n0")
# Runaway guard, not a measurement: collecting the two trees is ~1s. Kept
# UNDER pyproject's 180s per-test SIGALRM on purpose — a subprocess timeout
# above that ceiling can never fire, and a wedged child would then surface as
# a bare `Timeout >180.0s` instead of this call's own diagnosis.
_COLLECT_ONLY_TIMEOUT_S = 120


def test_a_collect_only_run_of_the_artifact_trees_fetches_no_artifact(tmp_path):
    """Issue #264's reproducer as a guard: collect both trees, execute nothing, fetch nothing.

    A REAL child pytest rather than a hook called with a fake session. The
    defect was in what pytest does with a hook, and an in-process call could
    only ever assert what the hook did with the session it was handed — which
    is the reasoning that was already right. The cache is empty so any fetch
    that lands is visible, and every proxy variable points at a closed local
    port so a fetch that is attempted is refused in milliseconds instead of
    reaching a public mirror (`_is_transient` still retries a refusal, so a
    regression costs the 15s backoff, never a hang).

    Be exact about which assertion carries the claim. Under a refused proxy no
    bytes can arrive, and `_fetch` unlinks its `.part` in a `finally`, so a
    restored collection hook leaves the cache EMPTY and dies as INTERNALERROR:
    the exit code is the trace that moves (verified by restoring the hook —
    exit 3, cache empty). The cache assertion is a backstop for a future fetch
    path that reaches somewhere the proxy does not block, not a second
    independent witness.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    # PYTEST_* dropped so the child is not steered by the parent's own run,
    # the same recipe as `tests/unit/test_lane_invariants.py`'s collect children.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["OTTO_BUSYBOX_CACHE"] = str(cache)
    # Declared opt-ins survive the ambient strip and would reach the child:
    # a hand-exported OTTO_CONFORMANCE_BED would make it resolve the 49-cell
    # bed space (lab-config dependent) instead of the hermetic one.
    for name in ("OTTO_CONFORMANCE_BED", "OTTO_CONFORMANCE_CELLS", "no_proxy", "NO_PROXY"):
        env.pop(name, None)
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env[name] = "http://127.0.0.1:9"

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *_COLLECT_ONLY_ARGS, "tests/busybox", "tests/conformance"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_COLLECT_ONLY_TIMEOUT_S,
        check=False,
    )

    assert completed.returncode == 0, (
        f"a collect-only run over the artifact trees exited {completed.returncode}. A "
        f"collection hook that reaches the network dies here as INTERNALERROR.\n"
        f"stdout tail:\n{completed.stdout[-1500:]}\nstderr tail:\n{completed.stderr[-1500:]}"
    )
    # Premise before claim: an empty cache after a run that collected nothing
    # would satisfy the assertion below without testing anything.
    assert re.search(r"\b[1-9]\d* tests? collected\b", completed.stdout), (
        f"the child collected nothing, so the empty cache proves nothing:\n"
        f"{completed.stdout[-800:]}"
    )
    leftovers = sorted(p.name for p in cache.iterdir())
    assert leftovers == [], (
        f"a run that executed no test fetched {leftovers} — the artifact source is "
        f"back on the critical path of every collect-only child (issue #264)"
    )


@pytest.mark.usefixtures("_fresh_preflight")
def test_the_first_consumer_proves_the_source_and_later_ones_remember(tmp_path, monkeypatch):
    """`busybox_binary` is where the one-probe bound lives now, and it must be ONE.

    Five artifacts, each paying a full retry budget against a dead source, is
    the arithmetic `preflight` exists to collapse (300s inside one test's 180s
    SIGALRM — `_run_conformance` builds the whole hermetic space in one
    subprocess). With the collection hooks gone, the consumer is the only
    place left to pay it, so the claim is asserted there: the first consumer
    spends the budget and raises the priming instructions; the second, asking
    for a DIFFERENT artifact, re-raises that same verdict without a single
    further attempt. The attempt log is checked before and after, which is
    what tells "remembered" apart from "measured again and agreed".
    """
    monkeypatch.setenv("OTTO_BUSYBOX_CACHE", str(tmp_path))
    _record_sleeps(monkeypatch)

    with _closing_http_errors(503, 503, 503) as errors:
        attempts = _stub_attempts(monkeypatch, errors)

        with pytest.raises(BusyBoxUnavailableError, match="make busybox-cache") as first:
            busybox_binary(BUSYBOX_MATRIX[0])
        assert attempts == [1, 2, 3], f"the first consumer spends the retry budget, saw {attempts}"

        with pytest.raises(BusyBoxUnavailableError) as second:
            busybox_binary(BUSYBOX_MATRIX[1])

        assert attempts == [1, 2, 3], (
            f"the verdict must be REMEMBERED by the next consumer, not re-measured; the "
            f"attempt log grew to {attempts}"
        )
        assert second.value is first.value, "and it must re-raise the verdict it recorded"


@pytest.mark.usefixtures("_fresh_preflight")
def test_the_consumer_bound_holds_where_the_artifact_cannot_run(tmp_path, monkeypatch):
    """A host that cannot EXECUTE an artifact can still FETCH it, and must still be bounded.

    `preflight()`'s matrix form filters on `can_run`, because the tier only
    fetches what it will run. The consumer form must not: `make busybox-cache`
    ("no interpreter needed, so it works on any arch") and
    `scripts/build_busybox_guest_images.py` fetch every entry on a machine
    that may run none of them. With the filter applied there, the probe list
    is empty, the memo is marked done by a NO-OP, and every consumer in the
    process — including ones asking for an arch the host can run — pays its
    own full retry budget against a dead source: five budgets where one was
    promised. Measured before the fix as 15 attempts across the matrix on an
    aarch64 host with no i686 handler.

    Two rows: the Makefile form asked first, which must neither probe nor
    record anything it did not prove; then two consumers, of which only the
    first may spend a budget.
    """
    monkeypatch.setenv("OTTO_BUSYBOX_CACHE", str(tmp_path))
    monkeypatch.setattr(busybox, "can_run", lambda *_a, **_kw: False)
    _record_sleeps(monkeypatch)

    with _closing_http_errors(503, 503, 503) as errors:
        attempts = _stub_attempts(monkeypatch, errors)

        busybox.preflight()  # nothing runnable is missing: no probe, no verdict
        assert attempts == [], f"the tier's form must not fetch what it cannot run, saw {attempts}"

        with pytest.raises(BusyBoxUnavailableError) as first:
            busybox_binary(BUSYBOX_MATRIX[0])
        assert attempts == [1, 2, 3], f"the first consumer spends one budget, saw {attempts}"

        with pytest.raises(BusyBoxUnavailableError) as second:
            busybox_binary(BUSYBOX_MATRIX[1])
        assert attempts == [1, 2, 3], (
            f"a vacuous `done` let the second consumer re-measure: attempts {attempts}"
        )
        assert second.value is first.value


def test_the_whole_matrix_in_one_test_is_why_the_precondition_exists():
    """The arithmetic that `preflight` exists to fix, asserted as arithmetic.

    Two claims, and the FIRST is the one that makes this guard mean anything:
    the unprotected whole-matrix bound genuinely EXCEEDS the per-test timeout,
    so the gap being closed is real rather than defensive. `_run_conformance`
    in tests/unit/test_support_matrix.py builds the entire hermetic space
    inside one subprocess, which is one test's budget, not a lane's.

    The second says the precondition collapses that to a single artifact's
    bound with the same 2x headroom the per-artifact guard above demands.
    Every number is READ from where it is configured, so raising the retry
    budget without revisiting this reddens it.
    """
    attempts = len(busybox._RETRY_BACKOFF_S) + 1
    per_artifact = attempts * busybox._FETCH_TIMEOUT_S + sum(busybox._RETRY_BACKOFF_S)
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    per_test = pyproject["tool"]["pytest"]["ini_options"]["timeout"]

    unprotected = per_artifact * len(BUSYBOX_MATRIX)
    assert unprotected > per_test, (
        f"this guard is vacuous unless the gap is real: {len(BUSYBOX_MATRIX)} artifacts at "
        f"{per_artifact}s is {unprotected}s against a {per_test}s per-test timeout. If that "
        f"stopped being true, `preflight`'s justification changed and its docstring is stale"
    )
    assert per_artifact * 2 <= per_test, (
        f"preflight bounds a dead source at ONE artifact ({per_artifact}s), which must keep "
        f"the same 2x headroom under the {per_test}s per-test timeout that a single fetch has"
    )


def _makefile_prerequisites(makefile: str) -> "dict[str, list[str]]":
    """Every explicit rule's target -> its prerequisite list.

    `(?!=)` after the colon is what keeps `MATRIX_BASELINE := ...` and friends
    out: a recursive-assignment line is not a rule, and counting one as a
    target with prerequisites would make the guard below answer about a
    variable's value.
    """
    rules: "dict[str, list[str]]" = {}
    for line in makefile.splitlines():
        match = re.match(r"^(?P<name>[A-Za-z0-9_.-]+)\s*:(?!=)\s*(?P<prereqs>[^#]*)", line)
        if match is None or match["name"] == ".PHONY":
            continue
        rules.setdefault(match["name"], []).extend(match["prereqs"].split())
    return rules


def test_only_the_busybox_tier_depends_on_the_preflight_target():
    """The whole #261 rule in one assertion, stated in both directions.

    `busybox-preflight` reaches a public mirror on a cold cache. That is
    correct for the tier that exists to verify those artifacts and WRONG for
    every default lane: `make coverage` collecting `tests/conformance/` and
    then deselecting all of it must not be able to fail because busybox.net is
    having a bad afternoon. Equality against the full dependent list rather
    than two membership checks, so a lane that acquires this prerequisite
    later is reported by name instead of slipping past an `in`.

    The conformance tree's own need is met from INSIDE the run, by
    `busybox_binary` itself, which probes at the first fetch a test actually
    makes — so a run that fetches nothing reaches nothing (issue #264).
    """
    makefile = (_REPO_ROOT / "Makefile").read_text()
    dependents = sorted(
        target
        for target, prereqs in _makefile_prerequisites(makefile).items()
        if "busybox-preflight" in prereqs
    )
    assert dependents == ["busybox"], (
        f"exactly one target may require the preflight, and it is the BusyBox tier; "
        f"found {dependents}. A default lane depending on it puts a public mirror on "
        f"the gate's critical path, which is issue #261"
    )
