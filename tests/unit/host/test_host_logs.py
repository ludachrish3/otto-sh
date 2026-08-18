"""Host log retrieval: the destination tree, debug-glob expansion, ordering.

Three contracts meet here:

* ``log_dest`` — ``<base>/logs/<host-id>``, where *base* is an explicit
  ``dest``, else the active command's output directory, else the CWD. The
  subtree below it (``product/``, ``debug/``) is API, not an implementation
  detail: it mirrors the coverage pipeline's per-host-id keying, and consumers
  read it by path.
* ``get_product_logs`` / ``get_debug_logs`` — the two halves, each best-effort,
  each with its own failure rule. Zero logs is SUCCESS; a debug glob on a host
  with no glob support is a LOUD failure rather than a silent skip.
* ``uninstall`` — product logs BEFORE teardown, debug logs AFTER it.

The host under test is the shared ``recording_host`` double (see
``tests/unit/host/conftest.py``): its ``get`` records the transfer and writes
nothing, and its ``glob`` is the production ``PosixFileOps.glob`` driven by a
scripted shell round-trip. ``embedded_recording_host`` is the same double
WITHOUT the posix file-ops family, i.e. with no ``glob`` at all.

Every test that calls a log verb without an explicit ``dest`` installs an
``active_context(output_dir=tmp_path)`` — the fallback is the CWD, and a test
that leaked the tree into the repo would be doing exactly the thing the design
prevents in production.
"""

import os
from pathlib import Path

import pytest

from otto.host.product import Product
from otto.result import Result
from otto.utils import Status, wait_for
from tests.conftest import active_context

# A fixed stamp in the past (2001-09-09), used to age a "left by an earlier
# haul" file. Aging by an explicit utime rather than by waiting is what keeps
# the mtime half of these tests off the wall clock entirely.
_AGED_NS = 1_000_000_000_000_000_000


def _await_a_new_filesystem_timestamp(reference: Path, probe: Path) -> None:
    """Block until the kernel's file-timestamp clock has moved past *reference*'s.

    File stamps come from the kernel's COARSE clock and are one tick granular,
    so two writes in the same tick get byte-identical mtime AND ctime —
    measured on this bed at 1775 collisions in 2000 back-to-back rewrites. A
    test that lays down a stale file and then hauls a fresh one over it cannot
    tell them apart ~90% of the time, so it must wait for the clock to move.

    Waits on the OBSERVABLE condition (a probe file stamped later than
    *reference*) rather than sleeping a guessed interval: one tick is 1-4 ms
    here, but that is a kernel build option, not a contract. Expiry raises
    (``wait_for``'s contract) as a runaway guard — if it ever fires, the
    filesystem is not maintaining ctime and the tests below are meaningless
    rather than merely slow.
    """
    was = reference.stat().st_ctime_ns

    def _clock_moved() -> bool:
        probe.write_text("tick", encoding="utf-8")
        return probe.stat().st_ctime_ns != was

    wait_for(
        _clock_moved,
        timeout=5.0,
        interval=0.001,  # a tick, not a poll interval — this resolves in ones of ms
        on_timeout=lambda: f"filesystem timestamp clock never advanced past {was}",
    )
    probe.unlink()


class _LoggingProduct(Product):
    """Product that records each lifecycle call and can drop a log file on ``get_logs``.

    *writes* is the basename it creates under the destination directory the
    host hands it — the only way a test can tell "the hook ran" apart from
    "the hook ran and its files landed in the documented place".
    """

    def __init__(self, name, events=None, writes=None, owner=None, fail_logs=False):
        self.name = name
        self.owner = owner
        self.events = events if events is not None else []
        self.writes = writes
        self.fail_logs = fail_logs

    def _record(self, phase):
        self.events.append(f"{self.name}:{phase}")
        return Result(Status.Success)

    async def stage(self, host):
        del host
        return self._record("stage")

    async def install(self, host):
        del host
        return self._record("install")

    async def uninstall(self, host):
        del host
        return self._record("uninstall")

    async def is_installed(self, host):
        del host
        return True

    async def get_logs(self, host, dest):
        del host
        self.events.append(f"{self.name}:get_logs")
        if self.writes is not None:
            (dest / self.writes).write_text("log line\n")
        if self.fail_logs:
            return Result(Status.Failed, msg=f"{self.name} logs failed")
        return Result(Status.Success)


class _MtimePreservingProduct(_LoggingProduct):
    """A product whose fetch restores the source's mtime — ``scp`` with ``preserve``.

    The local file is written for real (so its ctime advances, as any write
    does) and then stamped back to *mtime_ns*, which is exactly what a
    preserving transfer leaves behind.
    """

    def __init__(self, name, writes, mtime_ns):
        super().__init__(name, writes=writes)
        self.mtime_ns = mtime_ns

    async def get_logs(self, host, dest):
        result = await super().get_logs(host, dest)
        os.utime(dest / self.writes, ns=(self.mtime_ns, self.mtime_ns))
        return result


# =========================================================================== #
# log_dest — the destination tree
# =========================================================================== #


def test_log_dest_is_logs_slash_host_id_under_an_explicit_dest(recording_host, tmp_path):
    # Contract test: the directory shape is API (it mirrors coverage's
    # per-host-id keying). Kills: any drive-by reorganization of the tree.
    assert recording_host.log_dest(tmp_path) == tmp_path / "logs" / "h1"


def test_log_dest_defaults_to_the_active_commands_output_dir(recording_host, tmp_path):
    # Kills: reading Path.cwd() unconditionally — every otto run would scatter
    # log trees across whatever directory the user happened to be in.
    with active_context(output_dir=tmp_path / "run-1"):
        assert recording_host.log_dest() == tmp_path / "run-1" / "logs" / "h1"


def test_log_dest_falls_back_to_cwd_when_the_context_has_no_output_dir(
    recording_host, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    with active_context(output_dir=None):
        assert recording_host.log_dest() == Path.cwd() / "logs" / "h1"


def test_explicit_dest_beats_the_context(recording_host, tmp_path):
    # Kills: preferring the context when a caller named a destination.
    with active_context(output_dir=tmp_path / "ctx"):
        assert recording_host.log_dest(tmp_path / "given") == tmp_path / "given" / "logs" / "h1"


# =========================================================================== #
# get_product_logs
# =========================================================================== #


@pytest.mark.asyncio
async def test_product_logs_land_under_logs_hostid_product(recording_host, tmp_path):
    recording_host.products = [_LoggingProduct("app", writes="app.log")]
    result = await recording_host.get_product_logs(dest=tmp_path)
    assert result.is_ok
    assert (tmp_path / "logs" / recording_host.id / "product" / "app.log").exists()


@pytest.mark.asyncio
async def test_product_log_failure_does_not_stop_the_remaining_products(recording_host, tmp_path):
    """Best-effort: every product is asked, the FIRST failure is what returns.

    Kills: short-circuiting on the first non-ok result, which would lose the
    logs of every product declared after a noisy one.
    """
    events = []
    recording_host.products = [
        _LoggingProduct("a", events, fail_logs=True),
        _LoggingProduct("b", events),
    ]
    result = await recording_host.get_product_logs(dest=tmp_path)
    assert not result.is_ok
    assert result.msg == "a logs failed"
    assert events == ["a:get_logs", "b:get_logs"]


@pytest.mark.asyncio
async def test_get_product_logs_owner_filter_skips_another_repos_products(recording_host, tmp_path):
    # Kills: an owner argument that is accepted and ignored — repo A's
    # `get-logs` would haul repo B's product logs into its own tree.
    events = []
    recording_host.products = [
        _LoggingProduct("a", events, owner="acme"),
        _LoggingProduct("b", events, owner="other"),
    ]
    assert (await recording_host.get_product_logs(dest=tmp_path, owner="acme")).is_ok
    assert events == ["a:get_logs"]


@pytest.mark.asyncio
async def test_no_products_retrieves_nothing_successfully(recording_host, tmp_path):
    assert (await recording_host.get_product_logs(dest=tmp_path)).is_ok


# =========================================================================== #
# get_debug_logs
# =========================================================================== #


@pytest.mark.asyncio
async def test_debug_globs_expand_and_fetch_into_debug_dir(recording_host, tmp_path):
    recording_host.debug_log_globs = ["/var/log/messages*"]
    recording_host.script_glob(["/var/log/messages", "/var/log/messages.1"])
    result = await recording_host.get_debug_logs(dest=tmp_path)
    assert result.is_ok
    dest = tmp_path / "logs" / recording_host.id / "debug"
    assert recording_host.get_calls == [
        ([Path("/var/log/messages"), Path("/var/log/messages.1")], dest)
    ]


@pytest.mark.asyncio
async def test_concrete_debug_paths_need_no_glob_round_trip(embedded_recording_host, tmp_path):
    """A pattern-free entry is fetched as-is, on a host with no glob at all.

    Kills: expanding every entry, which would refuse (or cost a shell round
    trip for) the concrete paths that are the embedded family's whole answer.
    """
    embedded_recording_host.debug_log_globs = ["/logs/app.log"]
    result = await embedded_recording_host.get_debug_logs(dest=tmp_path)
    assert result.is_ok
    assert embedded_recording_host.exec_calls == []
    dest = tmp_path / "logs" / embedded_recording_host.id / "debug"
    assert embedded_recording_host.get_calls == [([Path("/logs/app.log")], dest)]


@pytest.mark.asyncio
async def test_debug_glob_pattern_without_glob_support_fails_loud(
    embedded_recording_host, tmp_path
):
    # Kills: silently skipping the pattern — the spec defers embedded globbing
    # but demands the gap be NAMED, not swallowed.
    embedded_recording_host.debug_log_globs = ["/logs/*.txt"]
    result = await embedded_recording_host.get_debug_logs(dest=tmp_path)
    assert not result.is_ok
    assert "glob" in result.msg
    assert "override" in result.msg
    assert embedded_recording_host.get_calls == []


@pytest.mark.asyncio
async def test_no_debug_globs_fetches_nothing_successfully(recording_host, tmp_path):
    # Kills: an unconditional get() with an empty file list, which several
    # transfer backends report as a failure.
    result = await recording_host.get_debug_logs(dest=tmp_path)
    assert result.is_ok
    assert recording_host.get_calls == []


# =========================================================================== #
# get_logs — the dispatcher
# =========================================================================== #


@pytest.mark.asyncio
async def test_zero_logs_is_success_but_require_product_logs_fails_empty(recording_host, tmp_path):
    assert (await recording_host.get_logs(dest=tmp_path)).is_ok
    result = await recording_host.get_logs(require_product_logs=True, dest=tmp_path)
    # Kills: a require flag that is parsed but never enforced.
    assert not result.is_ok
    assert recording_host.id in result.msg


@pytest.mark.asyncio
async def test_require_product_logs_is_satisfied_by_a_retrieved_file(recording_host, tmp_path):
    recording_host.products = [_LoggingProduct("app", writes="app.log")]
    assert (await recording_host.get_logs(require_product_logs=True, dest=tmp_path)).is_ok


@pytest.mark.asyncio
async def test_require_product_logs_is_not_satisfied_by_an_earlier_hauls_files(
    recording_host, tmp_path
):
    """A REUSED ``dest`` is the common case, and its leftovers prove nothing.

    ``--dest`` (or an output dir) pointed at last run's tree already contains
    ``logs/<id>/product/…``. Kills: asking whether the directory is non-empty,
    which reports "logs retrieved" for a haul that retrieved NOTHING and hands
    the caller yesterday's logs as today's evidence.
    """
    stale = tmp_path / "logs" / recording_host.id / "product"
    stale.mkdir(parents=True)
    (stale / "app.log").write_text("from an earlier haul\n", encoding="utf-8")

    recording_host.products = [_LoggingProduct("app")]  # writes nothing this time
    result = await recording_host.get_logs(require_product_logs=True, dest=tmp_path)
    assert not result.is_ok
    assert recording_host.id in result.msg
    assert (stale / "app.log").exists()  # …and the requirement check destroys nothing


@pytest.mark.asyncio
async def test_require_product_logs_counts_an_overwritten_file_as_retrieved(
    recording_host, tmp_path
):
    """Re-hauling a log under the name it already had IS a retrieval.

    THE STALE FILE HOLDS THE SAME BYTES the product is about to write, so the
    path and the size are identical and only a STAMP can decide this. Kills a
    check that looks for new filenames, and kills a ``(path, size)``
    fingerprint — each of which fails the ordinary second run, where every host
    writes ``app.log`` again into a dest that already has one.
    """
    product_dir = tmp_path / "logs" / recording_host.id / "product"
    product_dir.mkdir(parents=True)
    stale = product_dir / "app.log"
    stale.write_text("log line\n", encoding="utf-8")  # byte-for-byte what the product writes
    os.utime(stale, ns=(_AGED_NS, _AGED_NS))  # an earlier haul's file, aged deterministically

    recording_host.products = [_LoggingProduct("app", writes="app.log")]
    assert (await recording_host.get_logs(require_product_logs=True, dest=tmp_path)).is_ok


@pytest.mark.asyncio
async def test_require_product_logs_sees_a_refetch_that_preserved_size_and_mtime(
    recording_host, tmp_path
):
    """``scp`` with ``preserve`` reproduces the source mtime, so mtime cannot decide.

    ``ScpOptions.preserve`` forwards to ``asyncssh.scp``'s ``preserve``, so on
    a lab that sets it, re-fetching an unchanged log lands byte-identical size
    AND mtime into a reused dest — indistinguishable from "nothing arrived".
    Kills a ``(path, size, mtime)`` fingerprint, which would fail every
    ordinary run on such a lab for retrieving exactly what it was asked for.
    ctime is what closes it: no userspace API can set it, so the re-write
    advances it.
    """
    product_dir = tmp_path / "logs" / recording_host.id / "product"
    product_dir.mkdir(parents=True)
    stale = product_dir / "app.log"
    stale.write_text("log line\n", encoding="utf-8")
    os.utime(stale, ns=(_AGED_NS, _AGED_NS))
    # The stale file's ctime is NOW (utime just changed its metadata); the
    # fetch below must land in a later tick or nothing could tell them apart.
    _await_a_new_filesystem_timestamp(stale, tmp_path / ".tick-probe")

    recording_host.products = [_MtimePreservingProduct("app", "app.log", _AGED_NS)]
    assert (await recording_host.get_logs(require_product_logs=True, dest=tmp_path)).is_ok


@pytest.mark.asyncio
async def test_requiring_product_logs_while_skipping_them_is_an_error(recording_host, tmp_path):
    """The contradiction fails LOUD, before anything is gathered.

    ``otto host <id> get-logs --no-product --require-product-logs`` is
    expressible today. Kills: enforcing the requirement only inside the
    ``product`` branch, where the flag is parsed, silently unenforced, and the
    command exits 0 having promised logs it never looked for.
    """
    recording_host.debug_log_globs = ["/logs/app.log"]
    result = await recording_host.get_logs(product=False, require_product_logs=True, dest=tmp_path)
    assert not result.is_ok
    assert "require_product_logs" in result.msg
    assert "product" in result.msg
    # And it refused BEFORE the debug half ran, rather than reporting an error
    # after doing half the work.
    assert recording_host.get_calls == []


@pytest.mark.asyncio
async def test_the_contradiction_refusal_matches_the_project_layers_wording(
    recording_host, tmp_path
):
    """Three verbs refuse this flag pair; one wording, or the CLI teaches three.

    ``otto host <id> get-logs``, ``otto run get-logs`` and the lab-level verb
    all express the same contradiction, and the project layer holds its two in
    one constant. It cannot hold this one -- the host layer sits below it and
    may not import it -- so the guard against that copy drifting is here.
    """
    from otto.project.actions import _REQUIRE_PRODUCT_LOGS_CONTRADICTION

    result = await recording_host.get_logs(product=False, require_product_logs=True, dest=tmp_path)
    assert result.msg == _REQUIRE_PRODUCT_LOGS_CONTRADICTION


@pytest.mark.asyncio
async def test_get_logs_halves_are_selectable(recording_host, tmp_path):
    """``product``/``debug`` each gate their own half. Kills: either flag ignored."""
    events = []
    recording_host.debug_log_globs = ["/logs/app.log"]

    recording_host.products = [_LoggingProduct("app", events)]
    assert (await recording_host.get_logs(debug=False, dest=tmp_path)).is_ok
    assert events == ["app:get_logs"]
    assert recording_host.get_calls == []

    events.clear()
    assert (await recording_host.get_logs(product=False, dest=tmp_path)).is_ok
    assert events == []
    assert len(recording_host.get_calls) == 1


@pytest.mark.asyncio
async def test_get_logs_reports_a_product_failure_without_fetching_debug_logs(
    recording_host, tmp_path
):
    recording_host.debug_log_globs = ["/logs/app.log"]
    recording_host.products = [_LoggingProduct("app", fail_logs=True)]
    result = await recording_host.get_logs(dest=tmp_path)
    assert not result.is_ok
    assert result.msg == "app logs failed"


# =========================================================================== #
# uninstall — THE ordering decision
# =========================================================================== #


@pytest.mark.asyncio
async def test_uninstall_orders_product_logs_uninstall_debug_logs(recording_host, tmp_path):
    """Product logs BEFORE teardown, debug logs AFTER it.

    Chris's decision, and the reason the two halves are separate verbs at all:
    a lost product-log set is the frustration this design exists to prevent, and
    teardown activity is exactly what debug logs are there to capture. Kills:
    gathering both up front, and gathering both at the end.
    """
    events = recording_host.event_log
    recording_host.products = [_LoggingProduct("app", events)]
    recording_host.debug_log_globs = ["/var/log/m*"]
    recording_host.script_glob(["/var/log/messages"])
    with active_context(output_dir=tmp_path):
        assert (await recording_host.uninstall()).is_ok
    fetched = next(i for i, e in enumerate(events) if e.startswith("get:"))
    assert events.index("app:get_logs") < events.index("app:uninstall") < fetched


@pytest.mark.asyncio
async def test_uninstall_log_flags_off_gather_nothing(recording_host, tmp_path):
    events = recording_host.event_log
    recording_host.products = [_LoggingProduct("app", events)]
    recording_host.debug_log_globs = ["/logs/app.log"]
    with active_context(output_dir=tmp_path):
        assert (await recording_host.uninstall(get_product_logs=False, get_debug_logs=False)).is_ok
    assert events == ["app:uninstall"]


@pytest.mark.asyncio
async def test_uninstall_proceeds_and_reports_when_log_gathering_fails(recording_host, tmp_path):
    """A lost log haul is reported, never a reason to leave the host installed.

    Kills: returning on the log step, which would strand every product on the
    host because one product's log retrieval failed.
    """
    events = recording_host.event_log
    recording_host.products = [_LoggingProduct("app", events, fail_logs=True)]
    with active_context(output_dir=tmp_path):
        result = await recording_host.uninstall()
    assert not result.is_ok
    assert result.msg == "app logs failed"
    assert "app:uninstall" in events


@pytest.mark.asyncio
async def test_uninstall_owner_filter_scopes_the_log_haul_too(recording_host, tmp_path):
    events = recording_host.event_log
    recording_host.products = [
        _LoggingProduct("a", events, owner="acme"),
        _LoggingProduct("b", events, owner="other"),
    ]
    with active_context(output_dir=tmp_path):
        assert (await recording_host.uninstall(owner="acme")).is_ok
    assert events == ["a:get_logs", "a:uninstall"]


@pytest.mark.asyncio
async def test_uninstall_debug_logs_stay_unscoped_when_an_owner_is_given(recording_host, tmp_path):
    """``owner`` narrows the product halves and NOTHING ELSE.

    Debug logs are host-level: the globs belong to the host, no repo stamps
    them, and there is nothing on them an owner could filter. The per-repo
    layer therefore turns them OFF explicitly (``get_debug_logs=False``) rather
    than relying on the scope to do it — which is only a real decision if this
    verb would in fact sweep them when scoped.

    Kills ``if get_debug_logs and owner is None:`` — the tempting shortcut,
    since every owner-scoped caller in the tree passes ``get_debug_logs=False``
    today, so it costs nothing in-tree and silently ignores the flag the moment
    a caller (an override, ``otto host <id> uninstall --owner``) asks for both.
    The two owner tests either side of this one cannot see it: neither sets a
    debug glob, so the debug half is a no-op whether it runs or not.
    """
    events = recording_host.event_log
    recording_host.products = [
        _LoggingProduct("a", events, owner="acme"),
        _LoggingProduct("b", events, owner="other"),
    ]
    recording_host.debug_log_globs = ["/var/log/messages"]

    with active_context(output_dir=tmp_path):
        assert (await recording_host.uninstall(owner="acme")).is_ok

    debug_dest = tmp_path / "logs" / recording_host.id / "debug"
    assert recording_host.get_calls == [([Path("/var/log/messages")], debug_dest)]
    # …and the sweep still lands AFTER the scoped teardown, where uninstall's
    # ordering contract puts it — the scope must not reorder the halves either.
    fetched = next(i for i, e in enumerate(events) if e.startswith("get:"))
    assert events.index("a:uninstall") < fetched
    assert "b:uninstall" not in events  # the PRODUCT half is still scoped
