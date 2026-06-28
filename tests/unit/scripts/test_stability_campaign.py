from pathlib import Path

from scripts.stability_campaign import build_tiers, classify_problem, main, summarize_stage


def test_classifies_async_leak_from_text():
    assert (
        classify_problem("tests.x::test_a", "multiple unraisable exception warnings", "") == "leak"
    )
    assert (
        classify_problem(
            "tests.x::test_b",
            "",
            "ResourceWarning: unclosed event loop <_UnixSelectorEventLoop ...>",
        )
        == "leak"
    )


def test_classifies_x86_telnet_wedge():
    assert classify_problem("tests.x::test_c", "console wedged", "") == "wedge"
    assert (
        classify_problem(
            "tests.x::test_d", "ConnectionError: shell never became ready after open", ""
        )
        == "wedge"
    )


def test_classifies_known_inner_pytest_flake():
    assert (
        classify_problem(
            "tests.unit.suite.test_otto_suite.TestOttoTestDir::test_test_dir_created_per_test",
            "AssertionError",
            "",
        )
        == "flake"
    )


def test_anything_else_is_real():
    assert classify_problem("tests.x::test_e", "AssertionError: 1 != 2", "") == "real"


def _write_junit(path: Path, cases: list[tuple[str, str, str]]) -> None:
    """Write a minimal JUnit XML file. cases = [(classname, name, failure_message_or_empty), ...]."""
    body = []
    for classname, name, msg in cases:
        if msg:
            body.append(
                f'<testcase classname="{classname}" name="{name}">'
                f'<failure message="{msg}"></failure></testcase>'
            )
        else:
            body.append(f'<testcase classname="{classname}" name="{name}"/>')
    path.write_text(f'<testsuite tests="{len(cases)}">{"".join(body)}</testsuite>')


def test_summarize_green_when_no_problems(tmp_path):
    p = tmp_path / "clean.xml"
    _write_junit(p, [("tests.x", "test_ok", "")])
    report = summarize_stage([p])
    assert report.total == 0
    assert report.green is True


def test_summarize_buckets_and_not_green(tmp_path):
    p = tmp_path / "dirty.xml"
    _write_junit(
        p,
        [
            ("tests.x", "test_real", "AssertionError: boom"),
            ("tests.y", "test_leak", "multiple unraisable exception warnings"),
            ("tests.z", "test_wedge", "console wedged"),
        ],
    )
    report = summarize_stage([p])
    assert report.counts == {"leak": 1, "wedge": 1, "flake": 0, "real": 1}
    assert report.total == 3
    assert report.green is False


def test_build_tiers_threads_count():
    count = 3
    tiers = build_tiers(count=count, breadth=False)
    names = {t.name for t in tiers}
    assert {
        "unit",
        "full-deep",
        "concurrency",
        "integration-stability",
        "embedded-contract",
    } <= names
    for t in tiers:
        assert (f"--count={count}" in t.argv) or (f"COUNT={count}" in t.argv), t.name


def test_breadth_tier_only_when_requested():
    assert not any(t.name == "full-breadth" for t in build_tiers(count=1, breadth=False))
    assert any(t.name == "full-breadth" for t in build_tiers(count=1, breadth=True))


def test_deep_tier_pins_python_3_10():
    deep = next(t for t in build_tiers(count=10, breadth=False) if t.name == "full-deep")
    assert "tests_all-3.10" in deep.argv


def test_unit_tier_uses_tests_unit_session():
    unit = next(t for t in build_tiers(count=1, breadth=False) if t.name == "unit")
    assert "tests_unit" in unit.argv
    assert unit.junit and all("tests_unit-" in j for j in unit.junit)


def test_dry_run_prints_each_tier_command(capsys):
    rc = main(["run", "--count", "1", "--breadth", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    for tier_name in ("unit", "full-deep", "full-breadth", "embedded-contract"):
        assert tier_name in out
    assert "--count=1" in out


def test_run_exit_code_reflects_green_vs_dirty(tmp_path, monkeypatch):
    import scripts.stability_campaign as sc

    clean = tmp_path / "clean.xml"
    _write_junit(clean, [("t", "ok", "")])
    dirty = tmp_path / "dirty.xml"
    _write_junit(dirty, [("t", "bad", "AssertionError: x")])
    monkeypatch.setattr(sc, "_run_tier", lambda tier: None)

    def make_tiers(target):
        def _b(count, *, breadth):
            return [sc.Tier(name="fake", argv=["true"], junit=[str(target)])]

        return _b

    monkeypatch.setattr(sc, "build_tiers", make_tiers(clean))
    assert sc.main(["run", "--count", "1"]) == 0
    monkeypatch.setattr(sc, "build_tiers", make_tiers(dirty))
    assert sc.main(["run", "--count", "1"]) == 1


def test_run_dirty_when_junit_missing(tmp_path, monkeypatch):
    import scripts.stability_campaign as sc

    missing = tmp_path / "nope.xml"  # never created
    monkeypatch.setattr(sc, "_run_tier", lambda tier: None)
    monkeypatch.setattr(
        sc,
        "build_tiers",
        lambda count, *, breadth: [sc.Tier("fake", ["true"], [str(missing)])],
    )
    assert sc.main(["run", "--count", "1"]) == 1  # missing report => not green


def test_escalate_stops_on_first_dirty(tmp_path, monkeypatch):
    import scripts.stability_campaign as sc

    dirty = tmp_path / "d.xml"
    _write_junit(dirty, [("t", "bad", "AssertionError")])
    ran = []
    monkeypatch.setattr(sc, "_run_tier", lambda tier: ran.append(tier.name))
    monkeypatch.setattr(
        sc,
        "build_tiers",
        lambda count, *, breadth: [sc.Tier("fake", ["true"], [str(dirty)])],
    )
    assert sc.main(["escalate"]) == 1
    assert ran == ["fake"]  # stopped after the first (count=1) stage
