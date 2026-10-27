"""The duplicate-code ratchet reads pylint's report and holds the baseline contract."""

from pathlib import Path

from scripts import check_duplicate_code as gate

REPORT = """\
************* Module otto.reservations.identity
R0801: Similar lines in 2 files
==otto.host.option_specs:[29:43]
==otto.host.options:[106:157]
    def thing(self) -> None:
        return self.other
R0801: Similar lines in 3 files
==otto.c:[1:9]
==otto.a:[5:13]
==otto.b:[2:10]
    x = 1
"""


def test_parse_report_keys_each_clone_on_its_sorted_modules():
    clones = gate.parse_report(REPORT)
    assert [c.key for c in clones] == [
        "otto.host.option_specs otto.host.options",
        "otto.a otto.b otto.c",
    ]
    assert clones[1].spans == ("otto.a:[5:13]", "otto.b:[2:10]", "otto.c:[1:9]")


def test_parse_report_takes_only_the_announced_members():
    # The quoted clone body can hold anything; only the N lines after the
    # header are members.
    report = "R0801: Similar lines in 1 files\n==otto.x:[1:2]\n==otto.y:[3:4]\n"
    (clone,) = gate.parse_report(report)
    assert clone.modules == ("otto.x",)


def test_compare_names_new_pairs_and_pairs_that_went():
    clones = gate.parse_report(REPORT)
    verdict = gate.compare(clones, {"otto.a otto.b otto.c", "otto.old otto.older"})
    assert [c.key for c in verdict.new] == ["otto.host.option_specs otto.host.options"]
    assert verdict.gone == ["otto.old otto.older"]
    assert not verdict.holds


def test_compare_holds_on_an_exact_match():
    clones = gate.parse_report(REPORT)
    assert gate.compare(clones, {c.key for c in clones}).holds


def test_render_then_read_round_trips_and_sorts(tmp_path: Path):
    clones = gate.parse_report(REPORT)
    path = tmp_path / "baseline.txt"
    path.write_text(gate.render_baseline(clones))
    assert gate.read_baseline(path) == {c.key for c in clones}
    body = [line for line in path.read_text().splitlines() if not line.startswith("#")]
    assert body == sorted(body)


def test_pylint_runs_one_process_with_the_similarities_checker_alone():
    argv = gate.pylint_argv(["src/otto"])
    assert "--jobs=1" in argv
    assert "--enable=duplicate-code" in argv
    assert "--disable=all" in argv
    assert argv[-1] == "src/otto"
