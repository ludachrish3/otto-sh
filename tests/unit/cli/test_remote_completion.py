import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import otto
from otto.cli import remote_completion as rc
from otto.cli.remote_completion import (
    SplitPath,
    listing_command,
    parse_listing,
    present,
    split_incomplete,
)
from otto.config.remote_completion_cache import ListingEntry
from otto.result import CommandResult
from otto.utils import Status


@pytest.mark.parametrize(
    ("incomplete", "directory", "prefix"),
    [
        ("", "~", ""),
        ("~", "~", ""),
        ("~/", "~", ""),
        ("~/lo", "~", "lo"),
        ("/var/log/", "/var/log", ""),
        ("/var/log/sys", "/var/log", "sys"),
        ("/", "/", ""),
        ("rel/pa", "rel", "pa"),
        ("justname", "~", "justname"),
    ],
)
def test_split_incomplete(incomplete, directory, prefix):
    assert split_incomplete(incomplete) == SplitPath(directory=directory, prefix=prefix)


def test_listing_command_quotes_plain_paths():
    assert listing_command("/var/my logs") == "LC_ALL=C ls -1ALp -- '/var/my logs'"


def test_listing_command_home():
    assert listing_command("~") == 'LC_ALL=C ls -1ALp -- "$HOME"'


def test_listing_command_home_relative_expands_home_only():
    # Leading ~/ becomes "$HOME"/ so the shell expands it; the rest is quoted.
    assert listing_command("~/my logs") == "LC_ALL=C ls -1ALp -- \"$HOME\"/'my logs'"


def test_listing_command_never_interpolates_metacharacters():
    cmd = listing_command("/tmp/$(reboot)`x`")
    assert "$(reboot)" not in cmd.replace("'/tmp/$(reboot)`x`'", "")


def test_parse_listing_marks_dirs_and_skips_blank_lines():
    out = "logs/\nREADME\n\nkernel.bin\n"
    assert parse_listing(out) == [
        ListingEntry(name="logs", is_dir=True),
        ListingEntry(name="README", is_dir=False),
        ListingEntry(name="kernel.bin", is_dir=False),
    ]


ENTRIES = [
    ListingEntry(name="logs", is_dir=True),
    ListingEntry(name="README", is_dir=False),
    ListingEntry(name=".ssh", is_dir=True),
    ListingEntry(name=".bashrc", is_dir=False),
]


def test_present_joins_directory_and_marks_dirs():
    got = present(ENTRIES, SplitPath(directory="/home/u", prefix=""), kind="any")
    assert got == ["/home/u/README", "/home/u/logs/"]


def test_present_dir_kind_offers_only_directories():
    got = present(ENTRIES, SplitPath(directory="/home/u", prefix=""), kind="dir")
    assert got == ["/home/u/logs/"]


def test_present_prefix_filters():
    got = present(ENTRIES, SplitPath(directory="/home/u", prefix="RE"), kind="any")
    assert got == ["/home/u/README"]


def test_present_dotfiles_only_with_dot_prefix():
    got = present(ENTRIES, SplitPath(directory="/home/u", prefix="."), kind="any")
    assert got == ["/home/u/.bashrc", "/home/u/.ssh/"]


def test_present_home_directory_keeps_tilde():
    got = present(ENTRIES, SplitPath(directory="~", prefix="lo"), kind="any")
    assert got == ["~/logs/"]


####################
#  The completer: context walk, gate, listing
####################


def _ctx(host_id="dut1", labs=("unix",), as_user=None):
    """A mock Click context chain: leaf command -> `otto host` group -> root."""
    root = SimpleNamespace(params={"labs": list(labs), "as_user": as_user}, parent=None)
    group = SimpleNamespace(params={"host_id": host_id, "hop": "", "term": None}, parent=root)
    return SimpleNamespace(params={}, parent=group)


class _FakeHost:
    term = "ssh"
    id = "dut1"


def _patch_happy(monkeypatch, listing):
    monkeypatch.setattr(rc, "_load_host", lambda chain: (_FakeHost(), object()))
    monkeypatch.setattr(rc, "_reservation_allows", lambda chain: True)
    monkeypatch.setattr(rc, "_live_listing", lambda host, directory: listing)
    monkeypatch.setattr(rc, "_cached_listing_for", lambda host_id, directory: None)
    monkeypatch.setattr(rc, "_store_listing_for", lambda host_id, directory, entries: None)
    monkeypatch.setattr(rc, "_release_context", lambda token: None)


def test_chain_walk_takes_each_key_from_the_innermost_context_that_has_it():
    chain = rc._collect_chain_params(_ctx(host_id="dut2", labs=("unix",), as_user="carol"))
    assert chain.host_id == "dut2"
    assert chain.labs == ["unix"]
    assert chain.as_user == "carol"


def test_chain_walk_survives_a_self_referential_mock():
    node = SimpleNamespace(params="not-a-dict")
    node.parent = node
    assert rc._collect_chain_params(node) == rc._ChainParams(
        host_id="", hop="", term=None, labs=[], as_user=None
    )


def test_happy_path_lists_and_filters(monkeypatch):
    _patch_happy(monkeypatch, [ListingEntry("logs", True), ListingEntry("app.bin", False)])
    got = rc.remote_path_completer(_ctx(), "/var/lo", kind="any")
    assert got == ["/var/logs/"]


def test_no_host_id_returns_empty(monkeypatch):
    # A non-empty listing behind the seams, so dropping the guard would show.
    _patch_happy(monkeypatch, [ListingEntry("logs", True)])
    assert rc.remote_path_completer(_ctx(host_id=""), "/var/") == []


def test_no_lab_returns_empty(monkeypatch):
    _patch_happy(monkeypatch, [ListingEntry("logs", True)])
    assert rc.remote_path_completer(_ctx(labs=()), "/var/") == []


def test_non_ssh_host_returns_empty(monkeypatch):
    _patch_happy(monkeypatch, [ListingEntry("logs", True)])

    class _Telnet:
        term = "telnet"
        id = "dut1"

    monkeypatch.setattr(rc, "_load_host", lambda chain: (_Telnet(), object()))
    assert rc.remote_path_completer(_ctx(), "/var/") == []


def test_gate_refusal_blocks_and_never_lists(monkeypatch):
    calls = []
    _patch_happy(monkeypatch, [ListingEntry("logs", True)])
    monkeypatch.setattr(rc, "_reservation_allows", lambda chain: False)
    monkeypatch.setattr(rc, "_live_listing", lambda host, directory: calls.append(directory))
    assert rc.remote_path_completer(_ctx(), "/var/") == []
    assert calls == []


def test_gate_runs_before_host_load(monkeypatch):
    """Refused gate must mean zero lab contact — not even host construction."""
    order = []
    _patch_happy(monkeypatch, [])
    monkeypatch.setattr(rc, "_reservation_allows", lambda chain: (order.append("gate"), False)[1])
    monkeypatch.setattr(
        rc, "_load_host", lambda chain: (order.append("host"), (_FakeHost(), object()))[1]
    )
    assert rc.remote_path_completer(_ctx(), "/var/") == []
    assert order == ["gate"]


def test_listing_cache_hit_skips_live(monkeypatch):
    _patch_happy(monkeypatch, [])
    monkeypatch.setattr(
        rc, "_cached_listing_for", lambda host_id, directory: [ListingEntry("hit", False)]
    )
    monkeypatch.setattr(
        rc, "_live_listing", lambda host, directory: (_ for _ in ()).throw(AssertionError)
    )
    assert rc.remote_path_completer(_ctx(), "/var/h") == ["/var/hit"]


def test_cached_empty_directory_is_a_hit_not_a_miss(monkeypatch):
    """`cached_listing` returns [] for a cached-empty dir — truthiness would re-list.

    Asserted on the *call*, not the output: an empty cached dir and a
    needlessly re-listed one both complete to ``[]``, so only "did the live
    listing run?" can tell a ``is None`` test from a truthiness test.
    """
    live = []
    _patch_happy(monkeypatch, [ListingEntry("stale", False)])
    monkeypatch.setattr(rc, "_cached_listing_for", lambda host_id, directory: [])
    monkeypatch.setattr(rc, "_live_listing", lambda host, directory: live.append(directory) or [])
    assert rc.remote_path_completer(_ctx(), "/var/") == []
    assert live == []


def test_live_listing_result_is_stored(monkeypatch):
    stored = []
    _patch_happy(monkeypatch, [ListingEntry("logs", True)])
    monkeypatch.setattr(
        rc,
        "_store_listing_for",
        lambda host_id, directory, entries: stored.append((host_id, directory, entries)),
    )
    rc.remote_path_completer(_ctx(), "/var/")
    assert stored == [("dut1", "/var", [ListingEntry("logs", True)])]


def test_context_is_released_even_when_listing_raises(monkeypatch):
    released = []
    _patch_happy(monkeypatch, [])
    monkeypatch.setattr(
        rc, "_live_listing", lambda host, directory: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setattr(rc, "_release_context", released.append)
    assert rc.remote_path_completer(_ctx(), "/var/") == []
    assert len(released) == 1


def test_any_exception_yields_empty(monkeypatch):
    monkeypatch.setattr(
        rc, "_reservation_allows", lambda chain: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert rc.remote_path_completer(_ctx(), "/var/") == []


####################
#  The completion-side reservation gate
####################


def _chain(as_user="carol", labs=("unix",)):
    return rc._ChainParams(host_id="dut1", hop="", term=None, labs=list(labs), as_user=as_user)


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    """Point the sidecar cache at tmp_path and stub the gate's local inputs."""
    main = tmp_path / ".otto" / "completion_cache.json"
    main.parent.mkdir(parents=True)
    monkeypatch.setattr("otto.config.completion_cache._cache_path", lambda: main)
    monkeypatch.setattr(
        "otto.config.get_repos",
        lambda: [SimpleNamespace(reservation_settings={"backend": "json"})],
    )
    monkeypatch.setattr(
        "otto.reservations.identity.resolve_username",
        lambda as_user: SimpleNamespace(username="carol", source="$USER"),
    )
    monkeypatch.setattr(rc, "_required_for", lambda chain: {"r1"})
    return main


def _install_backend(monkeypatch, backend):
    monkeypatch.setattr(
        "otto.reservations.build_reservation_gate",
        lambda *a, **k: SimpleNamespace(backend=backend),
    )


class _WindowsBackend:
    def __init__(self, windows):
        self._windows = windows

    def get_reservation_windows(self, username):
        return self._windows

    def get_reserved_resources(self, username):
        raise AssertionError("windows backend must use the windows path")


class _FlatBackend:
    def __init__(self, held):
        self._held = held

    def get_reserved_resources(self, username):
        return self._held


def test_gate_no_reservation_config_allows(monkeypatch, gate_env):
    monkeypatch.setattr("otto.config.get_repos", lambda: [SimpleNamespace(reservation_settings={})])
    _install_backend(monkeypatch, _FlatBackend(set()))  # would refuse if it were ever consulted
    assert rc._reservation_allows(_chain()) is True


def test_gate_no_required_resources_allows(monkeypatch, gate_env):
    monkeypatch.setattr(rc, "_required_for", lambda chain: set())
    _install_backend(monkeypatch, _FlatBackend(set()))
    assert rc._reservation_allows(_chain()) is True


def test_gate_null_backend_allows(monkeypatch, gate_env):
    from otto.reservations.null_backend import NullReservationBackend

    _install_backend(monkeypatch, NullReservationBackend())
    assert rc._reservation_allows(_chain()) is True


def test_gate_missing_backend_allows(monkeypatch, gate_env):
    _install_backend(monkeypatch, None)
    assert rc._reservation_allows(_chain()) is True


def test_gate_windows_backend_allows_when_window_is_active(monkeypatch, gate_env):
    from otto.reservations import ReservationWindow

    now = datetime.now(tz=timezone.utc)
    _install_backend(
        monkeypatch,
        _WindowsBackend(
            [
                ReservationWindow(
                    resource="r1", start=now - timedelta(hours=1), end=now + timedelta(hours=1)
                )
            ]
        ),
    )
    assert rc._reservation_allows(_chain()) is True


def test_gate_windows_backend_refuses_when_window_has_not_started(monkeypatch, gate_env):
    from otto.reservations import ReservationWindow

    now = datetime.now(tz=timezone.utc)
    _install_backend(
        monkeypatch,
        _WindowsBackend(
            [
                ReservationWindow(
                    resource="r1", start=now + timedelta(hours=1), end=now + timedelta(hours=2)
                )
            ]
        ),
    )
    assert rc._reservation_allows(_chain()) is False


def test_gate_windows_backend_refuses_when_resource_is_not_covered(monkeypatch, gate_env):
    from otto.reservations import ReservationWindow

    now = datetime.now(tz=timezone.utc)
    _install_backend(
        monkeypatch,
        _WindowsBackend(
            [
                ReservationWindow(
                    resource="other", start=now - timedelta(hours=1), end=now + timedelta(hours=1)
                )
            ]
        ),
    )
    assert rc._reservation_allows(_chain()) is False


def test_gate_flat_backend_allows_when_resource_is_held(monkeypatch, gate_env):
    _install_backend(monkeypatch, _FlatBackend({"r1", "r2"}))
    assert rc._reservation_allows(_chain()) is True


def test_gate_flat_backend_refuses_when_resource_is_not_held(monkeypatch, gate_env):
    _install_backend(monkeypatch, _FlatBackend({"r2"}))
    assert rc._reservation_allows(_chain()) is False


def test_gate_backend_error_propagates_to_the_catch_all(monkeypatch, gate_env):
    from otto.reservations.check import ReservationBackendError

    def _boom(*a, **k):
        raise ReservationBackendError("scheduler down")

    monkeypatch.setattr("otto.reservations.build_reservation_gate", _boom)
    with pytest.raises(ReservationBackendError):
        rc._reservation_allows(_chain())


def test_gate_backend_error_stores_no_cache_entry(monkeypatch, gate_env):
    from otto.config.remote_completion_cache import cached_reservation_ok
    from otto.reservations.check import ReservationBackendError

    def _boom(*a, **k):
        raise ReservationBackendError("scheduler down")

    monkeypatch.setattr("otto.reservations.build_reservation_gate", _boom)
    with pytest.raises(ReservationBackendError):
        rc._reservation_allows(_chain())
    assert cached_reservation_ok("carol", {"r1"}, datetime.now(tz=timezone.utc)) is None


def test_gate_windows_query_is_cached(monkeypatch, gate_env):
    from otto.config.remote_completion_cache import cached_reservation_ok
    from otto.reservations import ReservationWindow

    now = datetime.now(tz=timezone.utc)
    _install_backend(
        monkeypatch,
        _WindowsBackend(
            [
                ReservationWindow(
                    resource="r1", start=now - timedelta(hours=1), end=now + timedelta(hours=1)
                )
            ]
        ),
    )
    assert rc._reservation_allows(_chain()) is True
    assert cached_reservation_ok("carol", {"r1"}, now) is True


def test_gate_cache_hit_never_builds_backend(monkeypatch, gate_env):
    from otto.config import remote_completion_cache as rcc
    from otto.reservations import ReservationWindow

    now = datetime.now(tz=timezone.utc)
    rcc.store_reservation_windows(
        "carol",
        [
            ReservationWindow(
                resource="r1", start=now - timedelta(hours=1), end=now + timedelta(hours=1)
            )
        ],
        now,
    )
    monkeypatch.setattr(
        "otto.reservations.build_reservation_gate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("backend built on cache hit")),
    )
    assert rc._reservation_allows(_chain()) is True


def test_gate_cached_refusal_is_honoured(monkeypatch, gate_env):
    from otto.config import remote_completion_cache as rcc

    now = datetime.now(tz=timezone.utc)
    rcc.store_reservation_set("carol", {"r2"}, now)
    monkeypatch.setattr(
        "otto.reservations.build_reservation_gate",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("backend built on cache hit")),
    )
    assert rc._reservation_allows(_chain()) is False


def test_gate_never_skips_under_dash_r(monkeypatch, gate_env):
    """-R must not reach the completion gate: the backend is always constructed."""
    seen = {}

    def _build(repos, *, as_user, skip_reservation_check, cwd_fallback):
        seen["skip"] = skip_reservation_check
        return SimpleNamespace(backend=_FlatBackend({"r1"}))

    monkeypatch.setattr("otto.reservations.build_reservation_gate", _build)
    assert rc._reservation_allows(_chain()) is True
    assert seen["skip"] is False


####################
#  Owner ruling: the reservation cache is completion-only
####################


def test_reservation_cache_is_completion_only():
    """Owner ruling: only the completion path may import the reservation cache."""
    import ast
    import pathlib

    src = pathlib.Path(otto.__file__).parent
    offenders = []
    for py in src.rglob("*.py"):
        rel = py.relative_to(src).as_posix()
        if rel in ("cli/remote_completion.py", "config/remote_completion_cache.py"):
            continue
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.ImportFrom) and "remote_completion_cache" in (
                node.module or ""
            ):
                names = {a.name for a in node.names}
                # main.py may reach the cache for the --clear handler only.
                if rel == "cli/main.py" and names <= {
                    "REMOTE_CACHE_FILENAME",
                    "clear_remote_cache",
                }:
                    continue
                offenders.append(f"{rel}: {sorted(names)}")
            elif isinstance(node, ast.Import) and any(
                "remote_completion_cache" in a.name for a in node.names
            ):
                # `import otto.config.remote_completion_cache`
                offenders.append(rel)
            elif isinstance(node, ast.ImportFrom) and any(
                a.name == "remote_completion_cache" for a in node.names
            ):
                # `from ..config import remote_completion_cache`
                offenders.append(rel)
    assert offenders == []


####################
#  The live listing
####################


class _ExecHost:
    """A host whose exec seam is scripted; records the command and the close."""

    term = "ssh"
    id = "dut1"

    def __init__(self, result=None, delay=0.0):
        self._result = result
        self._delay = delay
        self.cmds = []
        self.timeouts = []
        self.closed = 0

    async def exec(self, cmd, timeout=None, log=None):
        self.cmds.append((cmd, log))
        self.timeouts.append(timeout)
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result

    async def close(self):
        self.closed += 1


def test_live_listing_parses_a_successful_exec():
    host = _ExecHost(CommandResult(Status.Success, value="logs/\napp.bin\n", command="ls"))
    assert rc._live_listing(host, "/var") == [
        ListingEntry(name="logs", is_dir=True),
        ListingEntry(name="app.bin", is_dir=False),
    ]
    assert host.cmds[0][0] == listing_command("/var")
    assert host.closed == 1


def test_live_listing_is_redacted_from_every_sink():
    """NEVER, not QUIET: QUIET still writes the command I/O to verbose.log."""
    from otto.logger.mode import LogMode

    host = _ExecHost(CommandResult(Status.Success, value="", command="ls"))
    rc._live_listing(host, "/var")
    assert host.cmds[0][1] is LogMode.NEVER


def test_live_listing_signals_a_failed_exec_as_none():
    """None, not [] — a failure must be distinguishable from an empty directory."""
    host = _ExecHost(CommandResult(Status.Failed, value="junk\n", command="ls", retcode=2))
    assert rc._live_listing(host, "/nope") is None


def test_live_listing_returns_empty_list_for_a_genuinely_empty_directory():
    host = _ExecHost(CommandResult(Status.Success, value="", command="ls"))
    assert rc._live_listing(host, "/empty") == []


def test_live_listing_passes_the_deadline_to_exec():
    """`wait_for` is the backstop; the host layer gets to unwind on its own terms."""
    host = _ExecHost(CommandResult(Status.Success, value="", command="ls"))
    rc._live_listing(host, "/var")
    assert host.timeouts == [rc.LIST_DEADLINE_SECONDS]


def test_live_listing_closes_the_connection_when_exec_times_out(monkeypatch):
    monkeypatch.setattr(rc, "LIST_DEADLINE_SECONDS", 0.01)
    host = _ExecHost(CommandResult(Status.Success, value="", command="ls"), delay=5.0)
    # asyncio.TimeoutError is only an alias of the builtin from 3.11 on.
    with pytest.raises(asyncio.TimeoutError):
        rc._live_listing(host, "/var")
    assert host.closed == 1


def test_live_listing_deadline_failure_reaches_the_user_as_no_completions(monkeypatch):
    monkeypatch.setattr(rc, "LIST_DEADLINE_SECONDS", 0.01)
    host = _ExecHost(CommandResult(Status.Success, value="", command="ls"), delay=5.0)
    monkeypatch.setattr(rc, "_load_host", lambda chain: (host, object()))
    monkeypatch.setattr(rc, "_reservation_allows", lambda chain: True)
    monkeypatch.setattr(rc, "_cached_listing_for", lambda host_id, directory: None)
    monkeypatch.setattr(rc, "_release_context", lambda token: None)
    assert rc.remote_path_completer(_ctx(), "/var/") == []


def test_live_listing_runs_under_the_lifecycle_not_a_bare_asyncio_run(monkeypatch):
    """The house rule (tests/unit/test_no_bare_asyncio_run.py) asserted behaviourally.

    Also pins the teardown deadline: a completion process must not hold the
    shell for otto's 10s command default while a connection refuses to die.
    """
    from otto import lifecycle

    seen = {}
    real = lifecycle.run_command

    def _spy(coro, **kwargs):
        seen.update(kwargs)
        return real(coro, **kwargs)

    monkeypatch.setattr(lifecycle, "run_command", _spy)
    host = _ExecHost(CommandResult(Status.Success, value="logs/\n", command="ls"))
    assert rc._live_listing(host, "/var") == [ListingEntry(name="logs", is_dir=True)]
    assert seen == {"teardown_deadline": rc.LIST_DEADLINE_SECONDS}


def test_live_listing_lets_the_host_scope_sweep_the_connection():
    """``run_command`` enters the active context's host scope, so a host it
    registered is swept at loop exit as well as closed explicitly (belt and
    suspenders). A bare ``asyncio.run`` would close it once, never sweeping."""
    from otto.context import OttoContext, reset_context, set_context

    host = _ExecHost(CommandResult(Status.Success, value="", command="ls"))
    ctx = OttoContext(lab=SimpleNamespace(name="t", hosts={}))  # type: ignore[arg-type]
    ctx.scope.register(host)  # type: ignore[arg-type]
    token = set_context(ctx)
    try:
        assert rc._live_listing(host, "/var") == []
    finally:
        reset_context(token)
    assert host.closed == 2, "expected the explicit close AND the scope sweep"


def _interrupt_run_command(monkeypatch):
    """Make ``run_command`` answer with ``SystemExit(130)``, as a ^C does.

    Patched at the lifecycle seam rather than raised from inside the coroutine:
    a ``SystemExit`` raised *in a Task* is hard-aborted by asyncio itself (the
    loop unwinds mid-run, leaving tasks destroyed) — that is not the path a
    real interrupt takes. ``run_command`` catches ``_InterruptedCommand`` after
    its sweep and raises ``SystemExit(128 + signum)`` from the outside, which
    is exactly what this stub reproduces.
    """
    from otto import lifecycle

    def _interrupted(coro, **kwargs):
        coro.close()  # the real path never leaves the coroutine un-awaited either
        raise SystemExit(130)

    monkeypatch.setattr(lifecycle, "run_command", _interrupted)


def test_live_listing_degrades_an_interrupt_to_no_listing(monkeypatch):
    """``run_command`` answers a signal with ``SystemExit`` — a BaseException the
    completer's catch-all does not catch. The narrow guard turns it into
    "no listing", i.e. no completions, never a traceback mid-TAB."""
    _interrupt_run_command(monkeypatch)
    assert rc._live_listing(_ExecHost(None), "/var") is None


def test_interrupt_during_completion_reaches_the_user_as_no_completions(monkeypatch):
    """The same, end to end at the completer level: SystemExit degrades to []."""
    _interrupt_run_command(monkeypatch)
    monkeypatch.setattr(rc, "_load_host", lambda chain: (_ExecHost(None), object()))
    monkeypatch.setattr(rc, "_reservation_allows", lambda chain: True)
    monkeypatch.setattr(rc, "_cached_listing_for", lambda host_id, directory: None)
    monkeypatch.setattr(rc, "_release_context", lambda token: None)
    stored = []
    monkeypatch.setattr(
        rc, "_store_listing_for", lambda host_id, directory, entries: stored.append(directory)
    )
    assert rc.remote_path_completer(_ctx(), "/var/") == []
    assert stored == [], "an interrupted listing must not be cached"


def test_live_listing_swallows_a_failing_close():
    class _BadClose(_ExecHost):
        async def close(self):
            raise RuntimeError("connection already gone")

    host = _BadClose(CommandResult(Status.Success, value="logs/\n", command="ls"))
    assert rc._live_listing(host, "/var") == [ListingEntry(name="logs", is_dir=True)]


####################
#  What reaches the cache, and what reaches the terminal
####################


@pytest.fixture
def real_listing_cache(monkeypatch, tmp_path):
    """Wire the sidecar cache to tmp_path and leave the store/read seams REAL.

    The completer tests above stub `_store_listing_for`; these ones must not,
    because the whole question is what ends up in the cache file.
    """
    main = tmp_path / ".otto" / "completion_cache.json"
    main.parent.mkdir(parents=True)
    monkeypatch.setattr("otto.config.completion_cache._cache_path", lambda: main)
    monkeypatch.setattr(rc, "_reservation_allows", lambda chain: True)
    monkeypatch.setattr(rc, "_release_context", lambda token: None)
    return main


def _cached(directory):
    from otto.config.remote_completion_cache import cached_listing

    return cached_listing("dut1", directory, datetime.now(tz=timezone.utc))


def test_failed_listing_is_not_cached(monkeypatch, real_listing_cache):
    """A transient `ls` failure must not poison the directory for the whole TTL."""
    host = _ExecHost(CommandResult(Status.Failed, value="", command="ls", retcode=2))
    monkeypatch.setattr(rc, "_load_host", lambda chain: (host, object()))
    assert rc.remote_path_completer(_ctx(), "/var/") == []
    assert _cached("/var") is None


def test_genuinely_empty_directory_is_cached_as_empty(monkeypatch, real_listing_cache):
    """The counterpart: a successful empty listing IS cached, as []."""
    host = _ExecHost(CommandResult(Status.Success, value="", command="ls"))
    monkeypatch.setattr(rc, "_load_host", lambda chain: (host, object()))
    assert rc.remote_path_completer(_ctx(), "/var/") == []
    assert _cached("/var") == []


def test_successful_listing_is_cached(monkeypatch, real_listing_cache):
    host = _ExecHost(CommandResult(Status.Success, value="logs/\n", command="ls"))
    monkeypatch.setattr(rc, "_load_host", lambda chain: (host, object()))
    assert rc.remote_path_completer(_ctx(), "/var/") == ["/var/logs/"]
    assert _cached("/var") == [ListingEntry(name="logs", is_dir=True)]


NEVER_PRINTS_PROGRAM = """
import logging, sys
from types import SimpleNamespace

from otto.cli import remote_completion as rc
from otto.config.remote_completion_cache import ListingEntry

# The premise of the whole test: a real completion process has no logging
# configured, so the root logger is bare and logging.lastResort is live. If a
# future import chain starts installing a root handler this assert fails
# loudly rather than letting the test go quietly vacuous.
assert logging.getLogger().handlers == [], logging.getLogger().handlers

# Deliberately a THIRD-PARTY logger. `otto` carries a library-citizen
# NullHandler from import (src/otto/__init__.py), so its own records never
# reach lastResort; asyncssh -- which is what actually runs underneath the
# completer's exec -- has no handler and propagates straight to the bare root.
assert logging.getLogger("asyncssh").handlers == []


def _warn_then_list(host, directory):
    logging.getLogger("asyncssh").warning("connection reset by peer")
    logging.getLogger("otto.host").warning("connection retried")
    return [ListingEntry("logs", True)]


rc._load_host = lambda chain: (SimpleNamespace(term="ssh", id="dut1"), object())
rc._reservation_allows = lambda chain: True
rc._cached_listing_for = lambda host_id, directory: None
rc._store_listing_for = lambda host_id, directory, entries: None
rc._release_context = lambda token: None
rc._live_listing = _warn_then_list

root = SimpleNamespace(params={"labs": ["unix"], "as_user": None}, parent=None)
group = SimpleNamespace(params={"host_id": "dut1"}, parent=root)
got = rc.remote_path_completer(SimpleNamespace(params={}, parent=group), "/var/")
assert got == ["/var/logs/"], got
"""


def test_a_warning_logged_during_completion_never_reaches_the_terminal(tmp_path):
    """The reachable print: unconfigured logging + a WARNING = lastResort -> stderr.

    Must run in a subprocess. Under pytest the logging plugin keeps handlers on
    both the root and the ``otto`` logger for the whole test call, so
    ``lastResort`` can never fire and an in-process capsys assertion of this
    would be vacuous no matter how the fixture is written. Only a genuinely
    unconfigured interpreter reproduces the TAB-time conditions.
    """
    proc = subprocess.run(
        [sys.executable, "-c", NEVER_PRINTS_PROGRAM],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
        check=False,  # the assertion below reports proc.stderr, which `check` would hide
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert proc.stdout == ""


def test_silenced_installs_a_root_handler_and_removes_it_again():
    """The mechanism behind the subprocess test, asserted directly."""
    import logging

    root = logging.getLogger()
    before = root.handlers[:]
    with rc._silenced():
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        assert isinstance(added[0], logging.NullHandler)
    assert root.handlers == before


def test_silenced_restores_the_handler_list_when_the_body_raises():
    import logging

    root = logging.getLogger()
    before = root.handlers[:]
    with pytest.raises(RuntimeError), rc._silenced():
        raise RuntimeError("boom")
    assert root.handlers == before


def test_completer_prints_nothing_on_the_happy_path(monkeypatch, capsys):
    """Catches a stray print/rich write; the lastResort case needs the subprocess test."""
    _patch_happy(monkeypatch, [ListingEntry("logs", True)])
    assert rc.remote_path_completer(_ctx(), "/var/") == ["/var/logs/"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_completer_prints_nothing_when_the_listing_fails(monkeypatch, capsys):
    _patch_happy(monkeypatch, [])
    monkeypatch.setattr(
        rc, "_live_listing", lambda host, directory: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert rc.remote_path_completer(_ctx(), "/var/") == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_failed_listing_is_never_handed_to_the_cache(monkeypatch, real_listing_cache):
    """Asserted on the call: `store_listing(None)` would also raise its way to no entry."""
    stored = []
    host = _ExecHost(CommandResult(Status.Failed, value="", command="ls", retcode=2))
    monkeypatch.setattr(rc, "_load_host", lambda chain: (host, object()))
    monkeypatch.setattr(
        rc,
        "_store_listing_for",
        lambda host_id, directory, entries: stored.append((host_id, directory, entries)),
    )
    assert rc.remote_path_completer(_ctx(), "/var/") == []
    assert stored == []
