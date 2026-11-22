"""Completion never leaves a host out of a lab ``--lab`` loads it in.

Completion may offer more than dispatch loads (a lab it then refuses), never
less. The corpus holds the shapes where the two used to disagree: one source
declaring two machines under one id (each in its own lab), a pattern that
reaches a lab only another project declares, and an id two projects list.
Every lab is then built the way ``otto --lab`` builds it
(:func:`otto.session.build_lab`) and each host it constructs must be in
completion's hosts for that lab, through both by-lab readers.
"""

import json
import os

import pytest

from otto.config import completion_cache as cc
from otto.host.builtin_hosts import builtin_host_ids
from tests._fixtures.labdata import write_lab_json
from tests._fixtures.project_scaffold import make_project

_CREDS = [{"login": "u", "password": "p"}]
_SOURCES = '[[lab.sources]]\nbackend = "json"\npaths = ["lab1", "lab2"]\n'


def _host(ip: str) -> dict:
    return {"ip": ip, "creds": _CREDS}


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    from otto.config.project import discover

    a = make_project(tmp_path / "a", name="proj-a", extra=_SOURCES)
    b = make_project(tmp_path / "b", name="proj-b", extra=_SOURCES)

    # Project a, one source of two files. One id, two machines: ``x-cpu-1`` at
    # .1 in ``bench`` and ``x`` + board ``cpu`` slot 1 at .2 in ``spare``. And
    # a pattern that reaches ``lab-b``, which only project b declares.
    (a / "lab1").mkdir()
    (a / "lab1" / "lab.json").write_text(
        json.dumps(
            {
                "labs": {"bench": {"resources": []}},
                "elements": [
                    {"name": "x-cpu-1", "labs": ["bench"], "hosts": [_host("10.0.0.1")]},
                    {"name": "p", "labs": ["bench", "lab-.*"], "hosts": [_host("10.0.0.5")]},
                ],
                "links": [],
            }
        )
    )
    write_lab_json(
        a / "lab2" / "lab.json",
        [
            {
                "ip": "10.0.0.2",
                "element": "x",
                "board": "cpu",
                "slot": 1,
                "creds": _CREDS,
                "labs": ["spare"],
            }
        ],
    )
    # Project b: its own lab, and an id project a also lists (another machine).
    write_lab_json(
        b / "lab1" / "lab.json",
        [
            {"ip": "10.0.1.1", "element": "q", "creds": _CREDS, "labs": ["lab-b"]},
            {
                "ip": "10.0.1.2",
                "element": "x",
                "board": "cpu",
                "slot": 1,
                "creds": _CREDS,
                "labs": ["lab-b"],
            },
        ],
    )
    monkeypatch.setenv("OTTOPATH", os.pathsep.join([str(a), str(b)]))
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OTTO_LAB", raising=False)
    return discover().projects


def test_every_host_a_lab_loads_is_offered_under_that_lab(two_projects):
    from otto.session import build_lab

    by_lab = cc.collect_host_ids_by_lab(two_projects)
    labs = cc.collect_lab_names(two_projects)
    assert {"bench", "spare", "lab-b"} <= set(labs), "positive control: the corpus declares them"

    builtins = set(builtin_host_ids())
    checked = 0
    for lab in labs:
        loaded = set(build_lab(two_projects, labs=[lab]).hosts) - builtins
        live = set(cc.collect_host_ids(two_projects, lab_names=[lab]))
        assert loaded <= set(by_lab[lab]), f"{loaded - set(by_lab[lab])} missing under {lab!r}"
        assert loaded <= live, f"{loaded - live} missing from the live answer for {lab!r}"
        checked += len(loaded)
    assert checked >= 5, "positive control: hosts were compared"
    assert "x-cpu-1" in by_lab["spare"], "the second machine's lab (one source)"
    assert "p" in by_lab["lab-b"], "the pattern reaches a lab another project declares"
    assert "x-cpu-1" in by_lab["lab-b"], "an id two projects list is in the labs of both"
