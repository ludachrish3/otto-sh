"""``otto cache info``'s closing block: what completion offers HERE, and what it dropped.

The read side of completion's outlet (``otto.labs.drops``). Driven through
``cache_app`` with ``CliRunner`` like the rest of ``test_cache_cli.py``, but
against a REAL parsed repo under ``OTTO_SUT_DIRS`` — the block runs
discovery, reads the workspace's ``names`` entry and describes the inventory
as completion resolves it, so a stand-in would test nothing.
"""

import json
import re
from types import SimpleNamespace

from typer.testing import CliRunner

import otto.config.completion_cache as cc
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.sutrepo import make_sut_repo

runner = CliRunner()

_CREDS = [{"login": "u", "password": "p"}]

SETTINGS = """\
[[lab.sources]]
name = "local"
backend = "json"
paths = ["lab", "hosts.json"]

[inventory]
backend = "json"
path = "inventory.json"
supplies = ["ip"]
"""


SETTINGS_BROKEN = """\
[[lab.sources]]
name = "local"
backend = "json"
paths = ["lab", "hosts.json"]

[inventory]
backend = "json"
"""


def _workspace(tmp_path, monkeypatch, *, references: list[str], settings: str = SETTINGS):
    """A parsed SUT repo whose ``hosts.json`` supplement references *references*."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(home))
    repo = make_sut_repo(tmp_path / "invsut", name="invsut", extra=settings)
    (repo / "inventory.json").write_text(json.dumps({"dut-1": {"ip": "10.0.0.1"}}))
    write_lab_json(
        repo / "lab" / "lab.json",
        [{"ip": "10.0.0.9", "element": "plain", "creds": _CREDS, "labs": ["site"]}],
        declare_labs=True,
    )
    write_lab_json(
        repo / "hosts.json",
        [
            {"inventory": key, "element": f"dut{i}", "creds": _CREDS, "labs": ["site"]}
            for i, key in enumerate(references, start=1)
        ],
        declare_labs=False,
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    return repo


def _seed_cache() -> None:
    """Write the names entry the way the slow path does, from the real collectors."""
    from otto.bootstrap import discover

    repos = discover().repos
    assert repos, "discovery found no repo under OTTO_SUT_DIRS"
    cc.write_cache(
        repos,
        instructions=[],
        hosts=cc.collect_host_ids(repos),
        host_drops=cc.collect_host_drops(repos),
    )


def test_info_explains_the_workspace_hosts_and_what_was_dropped(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    repo = _workspace(tmp_path, monkeypatch, references=["dut-1", "ghost"])
    _seed_cache()

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "this workspace:" in out
    assert "completion names: fresh" in out
    assert "inventory: json:" in out
    assert "BROKEN" not in out
    assert f"lab files (invsut/local): {repo / 'lab' / 'lab.json'}, {repo / 'hosts.json'}" in out
    assert "hosts offered: 3 — dut1 local plain" in out  # `local` is the builtin host
    assert "dropped: 1 — not offered, and why:" in out
    assert f"[invsut] {repo / 'hosts.json'}: element 'dut2' hosts[0]: " in out
    assert "ghost" in out


def test_info_says_none_dropped_when_every_entry_built(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])
    _seed_cache()

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    assert "hosts offered: 3 — dut1 local plain" in result.output
    assert "dropped: none" in result.output


def test_info_reports_a_missing_entry_without_guessing_hosts(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    assert "no cached workspaces" in result.output
    assert "completion names: missing" in result.output
    assert "hosts offered: unknown — no entry to read" in result.output
    assert "dropped" not in result.output


def test_info_with_a_broken_inventory_promises_no_write(tmp_path, monkeypatch):
    """The standing line must not say a TAB writes an entry when the BROKEN
    inventory line right under it means nothing can (the digest is ephemeral)."""
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"], settings=SETTINGS_BROKEN)

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "inventory: BROKEN" in out
    assert "parse failed: path: Field required" in out
    expected = (
        "completion names: missing — no entry yet; nothing is written while the inventory is broken"
    )
    assert expected in out
    assert "writes one" not in out
    assert "rebuilds it" not in out
    assert "hosts offered: unknown — no entry to read" in out


def test_info_with_an_unfingerprinted_inventory_promises_no_write(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])
    unfingerprinted = SimpleNamespace(label="probe:live", fingerprint=lambda: None)
    monkeypatch.setattr("otto.inventory.config.build_inventory", lambda _repos: unfingerprinted)

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "inventory: probe:live — cannot report freshness, so completion never caches" in out
    assert "nothing is written while the inventory cannot report freshness" in out
    assert "writes one" not in out


def test_info_stays_home_wide_without_a_workspace(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(home))
    monkeypatch.delenv("OTTO_SUT_DIRS", raising=False)

    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    assert "this workspace" not in result.output


def test_info_reports_the_shim_standing(tmp_path, monkeypatch):
    from otto import _shim_complete as sc
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])
    out = runner.invoke(cache_app, ["info"]).output
    assert "  shim: handing over — no cache file" in out  # no entry yet
    # entry() writes the entry on any real invocation; write one the way it does.
    from otto import bootstrap as bs
    from otto.config import completion_cache as cc
    from otto.config.completion_tree import build_shim_payload

    repos = bs.discover().repos
    cc.write_cache(repos, [], [], shim=build_shim_payload(repos))
    out = runner.invoke(cache_app, ["info"]).output
    assert "  shim: served (validated now)" in out
    out = runner.invoke(cache_app, ["info"]).output
    assert re.search(r"  shim: served \(validated \d+s ago\)", out), out
    monkeypatch.setattr(sc, "inspect_shim", lambda path: "handing over — opaque inventory")
    assert "  shim: handing over — opaque inventory" in runner.invoke(cache_app, ["info"]).output


def _tests_workspace(tmp_path, monkeypatch):
    """A repo with one test file, its names/shim entry written as a rebuild writes it."""
    from otto import bootstrap as bs
    from otto.config.completion_tree import build_shim_payload

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(home))
    repo = make_sut_repo(
        tmp_path / "tsut",
        name="tsut",
        tests=["tests"],
        files={"tests/test_a.py": "def test_a():\n    pass\n"},
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo))
    bs.invalidate()
    repos = bs.discover().repos
    cc.write_cache(repos, [], [], shim=build_shim_payload(repos))
    return repo, repos


def _write_table(repo_obj) -> None:
    """The table a whole-tree collection of the repo writes: ``test_a`` in ``tests/test_a.py``."""
    from otto.config import collected_tests as ct

    tests = repo_obj.sut_dir / "tests"
    st = (tests / "test_a.py").stat()
    record = ct.FileRecord(
        stat=[st.st_mtime_ns, st.st_size], tests=[ct.RecordedTest(classes=[], name="test_a")]
    )
    dst = tests.stat()
    table = ct.updated_table(
        repo_obj,
        None,
        ct.classify(repo_obj, None),
        {str(tests / "test_a.py"): record},
        registered_markers=["slow"],
        whole_tree=True,
        dirs={str(tests): [dst.st_mtime_ns, dst.st_size]},
    )
    ct.write_tables([table])


def test_info_reports_what_the_next_test_name_tab_does(tmp_path, monkeypatch):
    """The same checks a test-name TAB runs: seed, or answer and say when the check is due."""
    import os
    import time

    from otto import _shim_complete as sc
    from otto.cli.cache import cache_app

    repo, [repo_obj] = _tests_workspace(tmp_path, monkeypatch)
    out = runner.invoke(cache_app, ["info"]).output
    assert f"  test names: handing over — no test-names cache for {repo_obj.sut_dir}" in out

    _write_table(repo_obj)
    out = runner.invoke(cache_app, ["info"]).output
    assert re.search(
        r"  test names: served \(1 name; checked \d+s ago, the next check is due in \d+s\)", out
    ), out

    marker = cc._cache_path().parent / sc.MARKER_FILENAMES["tests"]
    then = time.time() - sc.CHECK_WINDOW_SECONDS - 100
    os.utime(marker, (then, then))
    (repo / "tests" / "test_a.py").write_text("def test_b():\n    pass\n")
    out = runner.invoke(cache_app, ["info"]).output
    assert re.search(
        r"  test names: served \(1 name; last checked \d+s ago, a check is due\)", out
    ), out

    marker.unlink()
    out = runner.invoke(cache_app, ["info"]).output
    assert "  test names: served (1 name; no check recorded, a check is due)" in out


def test_info_reports_the_collect_child(tmp_path, monkeypatch):
    """Its lock and its cooldown: a detached child's failures are silent by design."""
    import os
    import time

    from otto.cli.cache import cache_app

    _tests_workspace(tmp_path, monkeypatch)
    home = cc._cache_path().parent
    assert "  collect child: idle" in runner.invoke(cache_app, ["info"]).output

    lock = home / cc.COLLECT_LOCK_FILENAME
    lock.write_text("1234")
    out = runner.invoke(cache_app, ["info"]).output
    assert re.search(r"  collect child: running \(it took the lock \d+s ago\)", out), out

    old = time.time() - cc.COLLECT_LOCK_STALE_SECONDS - 60
    os.utime(lock, (old, old))
    out = runner.invoke(cache_app, ["info"]).output
    assert re.search(
        r"  collect child: none running; its lock is \d+m old, left by one that died, "
        r"and the next one takes it over",
        out,
    ), out
    lock.unlink()

    cc.stamp_collect_cooldown("timed out")
    out = runner.invoke(cache_app, ["info"]).output
    assert re.search(
        r"  collect child: cooling down — the last one failed \d+s ago \(timed out\); "
        r"none starts for another \d+s",
        out,
    ), out


def test_info_reports_what_the_docker_cache_still_vouches_for(tmp_path, monkeypatch):
    from otto.bootstrap import discover
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])
    _seed_cache()
    repos = discover().repos
    cc.record_docker_images(repos, "test3", refs=["a:1"], ids=["i"])
    cc.record_docker_containers(repos, "alt2", names=["c"], ids=["x"])
    result = runner.invoke(cache_app, ["info"])
    assert result.exit_code == 0, result.output
    assert "  docker observed: images on test3; containers on alt2" in result.output


def test_info_says_nothing_observed_on_a_cold_docker_cache(tmp_path, monkeypatch):
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])
    _seed_cache()
    result = runner.invoke(cache_app, ["info"])
    assert result.exit_code == 0, result.output
    assert "  docker observed: nothing observed" in result.output


def test_info_drops_an_expired_sub_entry_and_lists_hosts_sorted(tmp_path, monkeypatch):
    """Containers last 15 minutes and images a day: sixteen minutes on, only the images stand."""
    from otto.bootstrap import discover
    from otto.cli.cache import cache_app

    _workspace(tmp_path, monkeypatch, references=["dut-1"])
    _seed_cache()
    repos = discover().repos
    cc.record_docker_images(repos, "zulu", refs=["a:1"], ids=["i"])
    cc.record_docker_images(repos, "alpha", refs=["b:1"], ids=["j"])
    cc.record_docker_containers(repos, "zulu", names=["c"], ids=["x"])
    cc.record_docker_containers(repos, "mike", names=["d"], ids=["y"])
    before = runner.invoke(cache_app, ["info"]).output
    assert "  docker observed: images on alpha, zulu; containers on mike, zulu" in before

    later = cc.time.time() + 16 * 60
    monkeypatch.setattr("otto.config.completion_cache.time.time", lambda: later)
    result = runner.invoke(cache_app, ["info"])

    assert result.exit_code == 0, result.output
    assert "  docker observed: images on alpha, zulu\n" in result.output
    assert "mike" not in result.output
