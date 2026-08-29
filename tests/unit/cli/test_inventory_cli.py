"""``otto inventory`` — read-only helpers over the configured inventory (spec §11).

Every assertion below is anchored on the phrase the guard exists for, never on
a bare word a repr, a locals dump or this module's own name could satisfy: the
CliRunner captures the whole rendered screen, so ``"creds" in output`` would be
satisfied by a column header and ``"inventory" in output`` by the label.

The verbs are driven through the sub-app with typer's ``CliRunner`` (the shape
``tests/unit/cli/test_schema_cli.py`` uses for the other settings-only group),
and the repo they read is a real one on disk: ``OTTO_SUT_DIRS`` points at a
scaffolded SUT repo and ``OTTO_HOME`` at a tmp dir, so ``build_inventory`` runs
its real §8 resolution over real files rather than a patched seam. That is what
makes "no inventory is configured" a reachable state here at all.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from otto import bootstrap
from otto.cli.inventory import inventory_app
from tests._fixtures.sutrepo import make_sut_repo
from tests.unit.inventory.netbox_stub import TOKEN, NetBoxStub, device

runner = CliRunner()

_RECORDS = {
    "_note": "comment space — the stage-1 parser drops it",
    "test1": {
        "ip": "10.0.0.1",
        "site": "lab-a",
        "creds": [{"login": "root", "password": "hunter2"}],
        "extra": {"asset": "A-1"},
    },
    "test2": {"ip": "10.0.0.2"},
}


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    """Render tables and rows at 600 columns.

    Rich folds at the detected width, which is 80 under a CliRunner with no
    tty — long enough to break a tmp path or a `json:<path>` label across two
    lines and defeat every substring assertion here (GH #89's shape).
    """
    monkeypatch.setenv("COLUMNS", "600")


def _scaffold(tmp_path, monkeypatch, *, inventory_toml=""):
    """A real SUT repo at ``tmp_path/repo``, active for this process, with a tmp home."""
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    root = make_sut_repo(tmp_path / "repo", name="inv_repo", extra=inventory_toml)
    monkeypatch.setenv("OTTO_SUT_DIRS", str(root))
    # The root conftest snapshot-restores bootstrap's caches, so invalidating
    # here cannot outlive the test; without it the repo above is invisible.
    bootstrap._reset()
    return root


def _netbox_toml(stub, ttl, *, creds_file=None):
    toml = f'[inventory]\nbackend = "netbox"\nurl = "{stub.base}"\ncache_ttl = "{ttl}"\n'
    if creds_file is not None:
        toml += f'creds_file = "{creds_file}"\n'
    return toml


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo whose ``[inventory]`` names a json file holding ``_RECORDS``."""
    inventory_file = tmp_path / "inventory.json"
    inventory_file.write_text(json.dumps(_RECORDS))
    toml = f'[inventory]\nbackend = "json"\npath = "{inventory_file}"\n'
    return _scaffold(tmp_path, monkeypatch, inventory_toml=toml)


def _run(args):
    return runner.invoke(inventory_app, args)


def _age_the_snapshot(tmp_path, hours):
    """Rewind the snapshot meta's ``fetched_at`` so the next resolution is past the TTL.

    Only ``fetched_at`` moves, so the meta still DESCRIBES the snapshot beside
    it and the cache reads the records back normally — the one thing that
    changes is that they are old.
    """
    cache_dir = tmp_path / "home" / "inventory-cache"
    metas = sorted(cache_dir.glob("*.meta.json"))
    # Loud rather than vacuous: with no meta the aging silently does nothing
    # and every staleness assertion below turns into "the backend answered".
    assert len(metas) == 1, f"expected exactly one snapshot meta under {cache_dir}, got {metas}"
    meta = json.loads(metas[0].read_text())
    aged = datetime.fromisoformat(meta["fetched_at"]) - timedelta(hours=hours)
    meta["fetched_at"] = aged.isoformat()
    metas[0].write_text(json.dumps(meta))
    return aged


# ── lookup ───────────────────────────────────────────────────────────────────


def test_lookup_shows_the_record_and_never_a_password(repo):
    result = _run(["lookup", "test1"])
    assert result.exit_code == 0, result.output
    assert "key:      test1" in result.output
    assert "backend:  json:" in result.output
    assert "10.0.0.1" in result.output
    assert "lab-a" in result.output
    # The login is shown so an operator can see WHICH credential the record
    # carries; the password never leaves the record.
    assert "root" in result.output
    assert "hunter2" not in result.output
    assert "A-1" in result.output  # record.extra, rendered opaquely


def test_lookup_unknown_key_exits_1_with_the_error(repo):
    result = _run(["lookup", "zz"])
    assert result.exit_code == 1
    assert "inventory key 'zz' not found in inventory 'json:" in result.output


def test_lookup_reports_supplies(repo):
    result = _run(["lookup", "test2"])
    assert result.exit_code == 0, result.output
    assert "supplies:" in result.output
    assert "os_name" in result.output


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_shows_keys_count_and_label(repo):
    result = _run(["list"])
    assert result.exit_code == 0, result.output
    assert "test1" in result.output
    assert "test2" in result.output
    assert "10.0.0.1" in result.output
    assert "10.0.0.2" in result.output
    assert "2 record(s) in json:" in result.output


# ── no inventory declared anywhere ───────────────────────────────────────────


def test_no_inventory_configured_exits_1_naming_both_files(tmp_path, monkeypatch):
    _scaffold(tmp_path, monkeypatch)
    result = _run(["list"])
    assert result.exit_code == 1
    # The RESOLVED user file, not the `~/.otto` spelling: OTTO_HOME moves it,
    # and a message naming a file the user does not have is worse than none.
    assert str(tmp_path / "home" / "settings.toml") in result.output
    assert "or in a project's .otto/settings.toml" in result.output


@pytest.mark.parametrize(
    ("verb", "code"),
    # `diff` exits 2, not 1: "I could not compare" is a third outcome there
    # (R28) and must not read to a script as "the two sides differ".
    [("lookup", 1), ("list", 1), ("refresh", 1), ("export", 1), ("diff", 2)],
)
def test_every_verb_names_both_files_when_nothing_is_declared(tmp_path, monkeypatch, verb, code):
    """Not just `list` — a verb that skipped the check would fail some other way."""
    _scaffold(tmp_path, monkeypatch)
    side = tmp_path / "side.json"
    side.write_text(json.dumps({"k": {"ip": "10.0.0.1"}}))
    args = {
        "lookup": ["lookup", "k"],
        "list": ["list"],
        "refresh": ["refresh"],
        "export": ["export", str(tmp_path / "out.json")],
        "diff": ["diff", str(side)],
    }[verb]
    result = _run(args)
    assert result.exit_code == code, result.output
    assert "no inventory is configured" in result.output
    assert str(tmp_path / "home" / "settings.toml") in result.output


def test_a_broken_inventory_declaration_names_the_settings_file(tmp_path, monkeypatch):
    _scaffold(tmp_path, monkeypatch, inventory_toml='[inventory]\nbackend = "json"\n')
    result = _run(["list"])
    assert result.exit_code == 1
    assert "Inventory unavailable:" in result.output
    assert "backend 'json' requires a 'path' string" in result.output


# ── export ───────────────────────────────────────────────────────────────────


def test_export_writes_a_stage_1_document_without_creds(repo, tmp_path):
    out = tmp_path / "export.json"
    result = _run(["export", str(out)])
    assert result.exit_code == 0, result.output
    assert "2 record(s)" in result.output
    assert str(out) in result.output
    doc = json.loads(out.read_text())
    assert list(doc) == ["test1", "test2"]  # sorted, and the `_note` is gone
    assert "creds" not in doc["test1"]
    assert "hunter2" not in out.read_text()


def test_export_refuses_to_overwrite_without_force(repo, tmp_path):
    out = tmp_path / "export.json"
    assert _run(["export", str(out)]).exit_code == 0
    out.write_text('{"sentinel": {"ip": "10.9.9.9"}}')
    again = _run(["export", str(out)])
    assert again.exit_code == 1
    assert f"{out} exists" in again.output
    assert "--force" in again.output
    # Refused means UNTOUCHED, not "refused after writing".
    assert "sentinel" in out.read_text()
    assert _run(["export", str(out), "--force"]).exit_code == 0
    assert "sentinel" not in out.read_text()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root writes into a mode-0500 directory, so the refusal cannot be provoked",
)
def test_export_into_an_unwritable_directory_exits_1_naming_it(repo, tmp_path):
    """A PermissionError out of `mkstemp` is a traceback; the operator is owed a sentence."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    out = locked / "out.json"
    try:
        result = _run(["export", str(out)])
    finally:
        locked.chmod(0o700)  # so tmp_path cleanup can remove it
    assert result.exit_code == 1
    assert f"{out}:" in result.output
    assert "Permission denied" in result.output


def test_export_below_a_regular_file_exits_1_naming_it(repo, tmp_path):
    """`<some file>/out.json` — the parent cannot be created because it is a file."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    out = blocker / "out.json"
    result = _run(["export", str(out)])
    assert result.exit_code == 1
    assert f"{out}:" in result.output
    assert "File exists" in result.output


def test_export_then_diff_reports_no_differences(repo, tmp_path):
    out = tmp_path / "export.json"
    assert _run(["export", str(out)]).exit_code == 0
    result = _run(["diff", str(out)])
    assert result.exit_code == 0, result.output
    assert "no differences between" in result.output


# ── diff ─────────────────────────────────────────────────────────────────────


def _other_document(tmp_path):
    other = tmp_path / "other.json"
    other.write_text(
        json.dumps(
            {
                "test1": {"ip": "10.0.0.9", "site": "lab-a"},
                "test3": {"ip": "10.0.0.3"},
            }
        )
    )
    return other


def test_diff_reports_changed_fields_and_missing_keys(repo, tmp_path):
    result = _run(["diff", str(_other_document(tmp_path))])
    assert result.exit_code == 1
    assert "test1" in result.output
    assert "ip" in result.output
    assert "10.0.0.1" in result.output
    assert "10.0.0.9" in result.output
    # A key on one side only is a row too, on both sides of the comparison.
    assert "test2" in result.output
    assert "test3" in result.output
    assert "hunter2" not in result.output


def test_diff_distinguishes_an_absent_key_from_an_unstated_field(repo, tmp_path):
    """The two blank cells a bare rendering makes indistinguishable (Task 9 review).

    THE ROWS, not just the legend. The legend is built at import time from the
    two constants, so a change that collapses the CELLS — rendering both empty
    sides the same way — leaves the legend intact and a legend-only assertion
    green while the table has gone back to being unreadable.
    """
    result = _run(["diff", str(_other_document(tmp_path))])
    assert result.exit_code == 1
    # test3 is in the file and not in the inventory: the left cell is ABSENT.
    assert re.search(r"test3.*absent", result.output)
    # test1 is on both sides; the file says nothing about its `extra` table.
    assert re.search(r"test1.*extra.*not stated", result.output)
    # …and the legend that explains the two.
    assert "'absent' = the key is not in that side at all" in result.output
    assert "'not stated' = the record is there but says nothing about that field" in result.output


def test_diff_names_which_side_is_which(repo, tmp_path):
    other = _other_document(tmp_path)
    result = _run(["diff", str(other)])
    assert "left:  json:" in result.output
    assert f"right: {other}" in result.output


def test_diff_of_two_files_never_touches_the_inventory(tmp_path, monkeypatch):
    """``diff A B`` reads two stage-1 files; there is no inventory to require."""
    _scaffold(tmp_path, monkeypatch)  # nothing declares [inventory]
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"k": {"ip": "10.0.0.1"}}))
    b.write_text(json.dumps({"k": {"ip": "10.0.0.2"}}))
    result = _run(["diff", str(a), str(b)])
    assert result.exit_code == 1, result.output
    assert "no inventory is configured" not in result.output
    assert f"left:  {a}" in result.output
    assert f"right: {b}" in result.output
    assert "10.0.0.1" in result.output
    assert "10.0.0.2" in result.output


def test_diff_of_a_missing_file_exits_2_not_1(repo, tmp_path):
    """R28: a typo'd path is "I could not compare", never "the sides differ"."""
    missing = tmp_path / "nope.json"
    result = _run(["diff", str(missing)])
    assert result.exit_code == 2
    assert f"{missing}:" in result.output
    assert "No such file" in result.output


def test_diff_of_a_malformed_document_exits_2_naming_the_key(repo, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"test1": {"ip": "10.0.0.1", "shelf": -3}}))
    result = _run(["diff", str(bad)])
    assert result.exit_code == 2
    assert "key 'test1'" in result.output
    assert "must be >= 0" in result.output


def test_diff_of_a_non_utf8_file_exits_2_naming_it(repo, tmp_path):
    """A binary file decodes before json ever sees it — a ValueError, not an OSError."""
    binary = tmp_path / "binary.json"
    binary.write_bytes(bytes([0xFF, 0xFE, 0x00, 0x01]))
    result = _run(["diff", str(binary)])
    assert result.exit_code == 2
    assert f"{binary}:" in result.output
    assert "codec can't decode byte 0xff" in result.output


# ── refresh ──────────────────────────────────────────────────────────────────


def test_refresh_of_an_uncached_inventory_exits_1_saying_why(repo):
    result = _run(["refresh"])
    assert result.exit_code == 1
    assert "is not cached — nothing to refresh" in result.output
    assert "the json backend reads its file on every command" in result.output


def test_refresh_of_a_zero_ttl_remote_inventory_exits_1_saying_why(tmp_path, monkeypatch):
    with NetBoxStub([device(1, "nb1")]) as stub:
        monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
        _scaffold(tmp_path, monkeypatch, inventory_toml=_netbox_toml(stub, "0"))
        result = _run(["refresh"])
    assert result.exit_code == 1
    assert "is not cached — nothing to refresh" in result.output
    assert "cache_ttl greater than 0" in result.output


# ── the netbox backend, through the local stub ───────────────────────────────


_NETBOX_DEVICES = [
    device(1, "nb1"),
    device(2, "nb2", ip="10.0.0.2/24"),
    device(3, "nb3", ip="10.0.0.3/24"),
    device(4, "nb-noaddr", ip=None),
    device(5, None),
]


@pytest.fixture
def netbox_repo(tmp_path, monkeypatch):
    with NetBoxStub(_NETBOX_DEVICES, page_size=2) as stub:
        monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
        _scaffold(tmp_path, monkeypatch, inventory_toml=_netbox_toml(stub, "24h"))
        yield stub


def test_list_reports_the_devices_netbox_made_it_skip(netbox_repo):
    result = _run(["list"])
    assert result.exit_code == 0, result.output
    assert "3 record(s) in netbox:" in result.output
    addressless = "skipped 1 device(s) with no address at ip_source 'primary_ip4': nb-noaddr"
    assert addressless in result.output
    assert "skipped 1 unnamed device(s): id 5" in result.output


@pytest.fixture
def far_east_timezone():
    """Run the test at UTC+14, where a local rendering can never equal a UTC one.

    Without this the local-time guard is VACUOUS on any machine whose clock is
    already UTC — CI's, and this VM's — because ``.astimezone()`` is then a
    no-op and dropping it changes nothing. Kiritimati is the largest offset
    there is, and it has no DST, so ``%Z`` renders a stable ``+14``.

    ``TZ`` carries no ``OTTO_`` prefix, so the suite's ambient-env strip leaves
    it alone; ``tzset()`` is what makes ``time``/``datetime`` re-read it, and it
    has to run again on the way out or the new zone leaks into every later test
    in this process.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_refresh_replaces_the_snapshot_and_reports_its_age(netbox_repo, far_east_timezone):
    assert _run(["list"]).exit_code == 0  # one fetch; writes the snapshot
    fetched = len(netbox_repo.queries)
    assert fetched, "the first use must actually fetch"

    result = _run(["refresh"])
    assert result.exit_code == 0, result.output
    assert "3 record(s)" in result.output
    assert "replaced the snapshot fetched" in result.output
    assert "(0m old)" in result.output
    assert len(netbox_repo.queries) > fetched, "refresh must contact the backend"

    # LOCAL time, unlike the load path's UTC-stamped stale warning (R25). At
    # UTC+14 the two renderings differ in every field, so `%Z` alone settles
    # it: `+14` is the local zone, `UTC` is what a missing `.astimezone()`
    # would print.
    now = datetime.now(timezone.utc)
    assert f"{now.astimezone():%Y-%m-%d}" in result.output
    assert "+14" in result.output
    assert f"{now:%Y-%m-%d %H:%M} UTC" not in result.output


def test_a_second_list_is_served_from_the_snapshot(netbox_repo):
    assert _run(["list"]).exit_code == 0
    fetched = len(netbox_repo.queries)
    assert _run(["list"]).exit_code == 0
    assert len(netbox_repo.queries) == fetched, "a fresh snapshot must not refetch"


def test_refresh_on_a_never_fetched_inventory_says_there_was_no_snapshot(netbox_repo):
    result = _run(["refresh"])
    assert result.exit_code == 0, result.output
    assert "no previous snapshot" in result.output


# ── the backend cannot answer ────────────────────────────────────────────────

# A port nothing listens on: the stdlib discard service, so a connect() is
# refused rather than answered.
DEAD = "http://127.0.0.1:9"


def _dead_netbox(tmp_path, monkeypatch):
    monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
    toml = f'[inventory]\nbackend = "netbox"\nurl = "{DEAD}"\ncache_ttl = "24h"\n'
    _scaffold(tmp_path, monkeypatch, inventory_toml=toml)


def test_list_of_an_unreachable_backend_exits_1_naming_it(tmp_path, monkeypatch):
    """No snapshot to fall back on, so the backend's own error is the answer."""
    _dead_netbox(tmp_path, monkeypatch)
    result = _run(["list"])
    assert result.exit_code == 1
    assert f"netbox inventory {DEAD}:" in result.output


def test_export_of_an_unreachable_backend_exits_1_naming_it(tmp_path, monkeypatch):
    _dead_netbox(tmp_path, monkeypatch)
    result = _run(["export", str(tmp_path / "out.json")])
    assert result.exit_code == 1
    assert f"netbox inventory {DEAD}:" in result.output
    assert not (tmp_path / "out.json").exists()


def test_export_checks_the_destination_before_fetching(tmp_path, monkeypatch):
    """A mistyped path must not cost a NetBox round trip — nor a stale refusal after one."""
    out = tmp_path / "out.json"
    out.write_text('{"sentinel": {"ip": "10.9.9.9"}}')
    _dead_netbox(tmp_path, monkeypatch)
    result = _run(["export", str(out)])
    assert result.exit_code == 1
    assert f"{out} exists" in result.output
    assert DEAD not in result.output  # the fetch never happened


def test_diff_against_an_unreachable_backend_exits_2_not_1(tmp_path, monkeypatch):
    """R28 again, from the other side: a left side that never loaded is not a difference."""
    side = tmp_path / "side.json"
    side.write_text(json.dumps({"k": {"ip": "10.0.0.1"}}))
    _dead_netbox(tmp_path, monkeypatch)
    result = _run(["diff", str(side)])
    assert result.exit_code == 2
    assert f"netbox inventory {DEAD}:" in result.output


def test_refresh_of_an_unreachable_backend_exits_1_naming_it(tmp_path, monkeypatch):
    """A snapshot exists, but the operator asked for a FETCH and did not get one.

    The load path serves the stale snapshot with a warning; ``refresh`` must
    not, or "I refreshed it" would mean "I read the same file again".
    """
    with NetBoxStub(_NETBOX_DEVICES, page_size=2) as stub:
        monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
        _scaffold(tmp_path, monkeypatch, inventory_toml=_netbox_toml(stub, "24h"))
        assert _run(["list"]).exit_code == 0  # writes the snapshot
        base = stub.base
    # The stub is down now; the snapshot is still on disk.
    result = _run(["refresh"])
    assert result.exit_code == 1
    assert f"netbox inventory {base}:" in result.output


# ── a stale snapshot is never served silently ────────────────────────────────


def _stale_netbox(tmp_path, monkeypatch, *, hours=31):
    """Fetch once, take the backend away, then age the snapshot past its TTL."""
    with NetBoxStub(_NETBOX_DEVICES, page_size=2) as stub:
        monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
        _scaffold(tmp_path, monkeypatch, inventory_toml=_netbox_toml(stub, "24h"))
        primed = _run(["list"])
        assert primed.exit_code == 0, primed.output
        assert "unreachable" not in primed.output, "a live fetch must not report staleness"
    _age_the_snapshot(tmp_path, hours=hours)


def test_list_reports_a_snapshot_it_served_because_the_backend_was_down(tmp_path, monkeypatch):
    """The load path LOGS this; a lab_free CLI group has no log handler to log to.

    ``otto/__init__.py`` puts a ``NullHandler`` on the ``otto`` logger, which
    defeats ``logging.lastResort``, and ``command_preamble`` never runs
    ``init_cli_logging`` for a lab-free group — so a warning is the same as
    silence here, and the operator gets a table with no hint that it is a day
    and a half old.
    """
    _stale_netbox(tmp_path, monkeypatch)
    result = _run(["list"])
    assert result.exit_code == 0, result.output
    assert "nb1" in result.output  # it still answers — that is the design
    assert "unreachable" in result.output
    assert "31h old" in result.output
    assert "run `otto inventory refresh`" in result.output


def test_export_says_the_artefact_it_wrote_came_from_a_stale_snapshot(tmp_path, monkeypatch):
    """Two resolutions in ONE process: the log line is deduped, the notice must not be.

    ``_warned_snapshots`` is process-global so an operator sees "your NetBox is
    down" once per command however often the inventory is resolved. If the
    notice were deduped with it, the SECOND verb in a process would write a
    stale export in silence — and the second is the one holding the records.
    """
    _stale_netbox(tmp_path, monkeypatch)
    assert _run(["list"]).exit_code == 0  # the first resolution takes the dedup slot

    out = tmp_path / "stale-export.json"
    result = _run(["export", str(out)])
    assert result.exit_code == 0, result.output
    assert "unreachable" in result.output
    assert "31h old" in result.output
    assert len(json.loads(out.read_text())) == 3  # the artefact is still written


def test_diff_says_its_left_side_came_from_a_stale_snapshot(tmp_path, monkeypatch):
    """The §19.2 transition gate: "no differences" against a stale left side is a lie.

    The comparison still runs and still exits 0 — the file and the snapshot
    really do agree — but the reader has to be told which of the two sides was
    a day-old copy, or the gate certifies a migration against yesterday.
    """
    out = tmp_path / "export.json"
    with NetBoxStub(_NETBOX_DEVICES, page_size=2) as stub:
        monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
        _scaffold(tmp_path, monkeypatch, inventory_toml=_netbox_toml(stub, "24h"))
        assert _run(["export", str(out)]).exit_code == 0
    _age_the_snapshot(tmp_path, hours=31)

    result = _run(["diff", str(out)])
    assert result.exit_code == 0, result.output
    assert "no differences between" in result.output
    assert "unreachable" in result.output
    assert "31h old" in result.output


def test_lookup_says_the_record_came_from_a_stale_snapshot(tmp_path, monkeypatch):
    _stale_netbox(tmp_path, monkeypatch)
    result = _run(["lookup", "nb1"])
    assert result.exit_code == 0, result.output
    assert "key:      nb1" in result.output
    assert "unreachable" in result.output
    assert "31h old" in result.output


def test_a_key_missing_from_a_stale_snapshot_says_the_snapshot_is_stale(tmp_path, monkeypatch):
    """A missing key and an old snapshot are one answer, not two."""
    _stale_netbox(tmp_path, monkeypatch)
    result = _run(["lookup", "added-yesterday"])
    assert result.exit_code == 1
    assert "inventory key 'added-yesterday' not found" in result.output
    assert "unreachable" in result.output
    assert "31h old" in result.output


def test_refresh_clears_the_stale_notice(tmp_path, monkeypatch):
    """A successful fetch must not leave the outage it just cleared standing."""
    with NetBoxStub(_NETBOX_DEVICES, page_size=2) as stub:
        monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
        _scaffold(tmp_path, monkeypatch, inventory_toml=_netbox_toml(stub, "24h"))
        assert _run(["list"]).exit_code == 0
        _age_the_snapshot(tmp_path, hours=31)
        refreshed = _run(["refresh"])
        assert refreshed.exit_code == 0, refreshed.output
        assert "unreachable" not in refreshed.output
        # And the next read is clean too — the snapshot on disk is new.
        listed = _run(["list"])
    assert listed.exit_code == 0, listed.output
    assert "unreachable" not in listed.output


# ── the creds overlay sits OUTSIDE the cache ─────────────────────────────────


def test_refresh_reaches_the_cache_through_the_creds_overlay(tmp_path, monkeypatch):
    """``creds_file`` wraps the cache in a CredsOverlay; refresh must see past it.

    Without the unwrap the outermost object is not a SnapshotCache and the
    verb would report a cached NetBox inventory as "not cached".
    """
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"nb1": [{"login": "operator", "password": "s3cret"}]}))
    with NetBoxStub(_NETBOX_DEVICES, page_size=2) as stub:
        monkeypatch.setenv("NETBOX_TOKEN", TOKEN)
        toml = _netbox_toml(stub, "24h", creds_file=creds)
        _scaffold(tmp_path, monkeypatch, inventory_toml=toml)
        result = _run(["refresh"])
        assert result.exit_code == 0, result.output
        assert "3 record(s)" in result.output
        assert "not cached" not in result.output

        # And the overlay is live rather than bypassed: the login comes from
        # the creds file, and its password still never prints.
        looked_up = _run(["lookup", "nb1"])
    assert looked_up.exit_code == 0, looked_up.output
    assert "operator" in looked_up.output
    assert "s3cret" not in looked_up.output
