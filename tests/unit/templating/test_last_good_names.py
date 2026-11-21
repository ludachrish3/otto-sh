"""A failed ``jsonnet`` source's last known lab names, shown as unavailable (lab templating §6.8).

A complete, problem-free status read of a scoped source records the names it
declared (:mod:`otto.labs.last_good`); when the source later fails, the
composite lists those names, each unavailable with the source's reason. The
names are diagnostics only: they never authorize a load, and never prove a
lab is outside the failed source's reach. Every test drives a real project
through one operation (``_lab_project``) under a private ``OTTO_HOME``.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

import otto.labs.jsonnet_source as jsonnet_module
from otto.config.home import lab_names_cache_dir
from otto.config.project import Project
from otto.labs import LabRepositoryError, LabUnavailableError
from otto.labs.jsonnet_source import _JsonnetLabRepository
from otto.templating.errors import RenderCancelled
from otto.templating.memo import cancellation
from tests.e2e._otto_subprocess import run_otto

from ._lab_project import jsonnet_source, operation, project, write

ROUTER_SCOPE = {"prefixes": ["router-lab."]}
"""Every router lab, by prefix."""

BROKEN = "function(otto) error 'netbox down'"
"""An entry whose render fails: the source is down."""

WAIT = 30.0
"""How long a test waits for a worker thread; never reached when the code is right."""


def _declaring(*labs: str) -> str:
    """An entry declaring *labs*, each with no element."""
    entries = ", ".join(f"'{lab}': {{}}" for lab in labs)
    return f"function(otto) {{ labs: {{ {entries} }} }}"


def _layouts(root: Path) -> Path:
    return root / "lab_data" / "layouts"


def _project(root: Path, *, entries: "dict[str, str]", **source: Any) -> Project:
    """A project whose one jsonnet source renders *entries* (file name to text).

    The source is scoped to ``ROUTER_SCOPE`` unless *source* gives a scope;
    ``scope=None`` leaves it unscoped.
    """
    for file, text in entries.items():
        write(_layouts(root) / file, text)
    source.setdefault("scope", ROUTER_SCOPE)
    if source["scope"] is None:
        del source["scope"]
    return project(root, jsonnet_source(paths=[str(_layouts(root) / "*.jsonnet")], **source))


def _repository(op: Any) -> _JsonnetLabRepository:
    """The one jsonnet source of *op*'s host source."""
    [found] = [s.repository for s in op.repository.sources if "/jsonnet#" in s.label]
    assert isinstance(found, _JsonnetLabRepository)
    return found


def _records() -> "list[Path]":
    """Every last-good record under the home, sorted."""
    directory = lab_names_cache_dir()
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def _healthy_then_broken(root: Path, *, labs: "tuple[str, ...]") -> Any:
    """Record *labs* from a healthy read, break the entry, and return the next operation."""
    sut = _project(root, entries={"lab.jsonnet": _declaring(*labs)})
    healthy = operation([sut])
    assert [s.name for s in healthy.repository.lab_status_report(view=healthy.view).labs] == sorted(
        labs
    )
    write(_layouts(root) / "lab.jsonnet", BROKEN)
    return operation([sut])


# --------------------------------------------------------------------------- #
# Names shown, never trusted.
# --------------------------------------------------------------------------- #


def test_first_run_with_no_cache(tmp_path, otto_home):
    root = tmp_path / "sut"
    op = operation([_project(root, entries={"lab.jsonnet": BROKEN})])
    report = op.repository.lab_status_report(view=op.view)
    assert report.labs == []
    [outage] = report.unavailable_sources
    assert "netbox down" in outage.diagnostic.reason
    with pytest.raises(LabUnavailableError, match="netbox down") as caught:
        op.repository.load_lab_in_view("router-lab.east", view=op.view)
    assert caught.value.declared is False
    assert _records() == []


def test_names_from_an_earlier_process_show_as_unavailable(tmp_path, otto_home):
    root = tmp_path / "sut"
    sut = _project(root, entries={"lab.jsonnet": _declaring("router-lab.east", "router-lab.west")})
    healthy = operation([sut])
    healthy.repository.lab_status_report(view=healthy.view)
    [record] = _records()
    assert set(json.loads(record.read_text())) == {"names", "recorded_at"}
    write(_layouts(root) / "lab.jsonnet", BROKEN)

    result = run_otto(
        ["--list-labs"],
        xdir=tmp_path / "xdir",
        roots=root,
        # The home this process recorded into, and a user home of the test's own,
        # so the child reads no real settings and writes nothing outside tmp_path.
        extra_env={"OTTO_HOME": str(otto_home), "HOME": str(tmp_path / "user"), "COLUMNS": "400"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    rows = [line.strip(" │") for line in result.stdout.splitlines() if "router-lab." in line]
    assert len(rows) == 2, result.stdout
    for row, lab in zip(rows, ["router-lab.east", "router-lab.west"], strict=True):
        assert row.startswith(f"• {lab} — unavailable: "), result.stdout
        assert "netbox down" in row, result.stdout
    assert "netbox down" in result.stdout.split("router-lab.west")[-1]  # the source's own row


def test_cached_names_never_authorize_a_load(tmp_path, otto_home):
    down = _healthy_then_broken(tmp_path / "sut", labs=("router-lab.east",))
    report = down.repository.lab_status_report(view=down.view)
    [status] = report.labs
    assert status.name == "router-lab.east"
    assert status.unavailable is not None
    assert "netbox down" in status.unavailable.reason
    with pytest.raises(LabUnavailableError, match="netbox down") as caught:
        down.repository.load_lab_in_view("router-lab.east", view=down.view)
    assert caught.value.declared is False


def test_cached_names_never_prove_non_contribution(tmp_path, otto_home):
    down = _healthy_then_broken(tmp_path / "sut", labs=("router-lab.east",))
    assert [s.name for s in down.repository.lab_status_report(view=down.view).labs] == [
        "router-lab.east"
    ]
    with pytest.raises(LabUnavailableError, match="netbox down") as caught:
        down.repository.load_lab_in_view("router-lab.north", view=down.view)
    assert caught.value.declared is False


def test_changed_declaration_ignores_old_names(tmp_path, otto_home):
    root = tmp_path / "sut"
    down = _healthy_then_broken(root, labs=("router-lab.east",))
    assert _repository(down).list_labs() == ["router-lab.east"]
    settings = root / ".otto" / "settings.toml"
    changed = jsonnet_source(
        paths=[str(_layouts(root) / "*.jsonnet")],
        scope=ROUTER_SCOPE,
        import_paths=["lab_data/lib", "lab_data/more"],
    )
    settings.write_text(  # scaffold-exempt: the changed declaration is the subject
        settings.read_text().split("[[lab.sources]]")[0] + changed
    )
    redeclared = operation([Project(root=root)])
    assert _repository(redeclared).list_labs() == []
    report = redeclared.repository.lab_status_report(view=redeclared.view)
    assert report.labs == []
    assert len(report.unavailable_sources) == 1


# --------------------------------------------------------------------------- #
# Only a complete, accepted read records; nothing else overwrites.
# --------------------------------------------------------------------------- #


def test_unwritable_home_never_fails_a_render(tmp_path, otto_home, caplog):
    lab_names_cache_dir().parent.mkdir(parents=True, exist_ok=True)
    lab_names_cache_dir().write_text("a file where the directory belongs")
    op = operation(
        [_project(tmp_path / "sut", entries={"lab.jsonnet": _declaring("router-lab.east")})]
    )
    with caplog.at_level(logging.DEBUG, logger="otto.labs.last_good"):
        report = op.repository.lab_status_report(view=op.view)
    [status] = report.labs
    assert status.name == "router-lab.east"
    assert status.available
    assert report.unavailable_sources == []
    assert _repository(op).list_labs() == []
    [record] = [r for r in caplog.records if r.name == "otto.labs.last_good"]
    assert record.levelno == logging.DEBUG
    assert "could not record" in record.getMessage()


def test_partial_read_never_overwrites_last_good_names(tmp_path, otto_home):
    root = tmp_path / "sut"
    sut = _project(
        root,
        entries={
            "east.jsonnet": _declaring("router-lab.east"),
            "west.jsonnet": _declaring("router-lab.west"),
        },
    )
    healthy = operation([sut])
    healthy.repository.lab_status_report(view=healthy.view)
    [record] = _records()
    before = record.read_bytes()
    write(_layouts(root) / "west.jsonnet", BROKEN)
    down = operation([sut])
    report = down.repository.lab_status_report(view=down.view)
    assert [s.name for s in report.labs] == ["router-lab.east", "router-lab.west"]
    assert all(not s.available for s in report.labs)
    assert _records() == [record]
    assert record.read_bytes() == before


def test_an_unscoped_source_records_nothing(tmp_path, otto_home):
    root = tmp_path / "sut"
    op = operation(
        [_project(root, entries={"lab.jsonnet": _declaring("router-lab.east")}, scope=None)]
    )
    [status] = op.repository.lab_status_report(view=op.view).labs
    assert status.name == "router-lab.east"
    assert _records() == []
    assert _repository(op).list_labs() == []


def test_an_unchanged_read_never_rewrites_the_record(monkeypatch, tmp_path, otto_home):
    import otto.utils

    real = otto.utils.atomic_write_bytes
    writes: list[Path] = []

    def counting(path: Path, payload: bytes, **kwargs: Any) -> None:
        if path.parent == lab_names_cache_dir():
            writes.append(path)
        real(path, payload, **kwargs)

    monkeypatch.setattr(otto.utils, "atomic_write_bytes", counting)
    root = tmp_path / "sut"
    sut = _project(root, entries={"lab.jsonnet": _declaring("router-lab.east")})
    first = operation([sut])
    first.repository.lab_status_report(view=first.view)
    [record] = _records()
    before = record.read_bytes()
    assert len(writes) == 1
    again = operation([sut])
    again.repository.lab_status_report(view=again.view)
    assert len(writes) == 1  # the same names: one read, no write
    assert record.read_bytes() == before  # recorded_at is when these names were first recorded
    write(_layouts(root) / "lab.jsonnet", _declaring("router-lab.east", "router-lab.west"))
    changed = operation([sut])
    changed.repository.lab_status_report(view=changed.view)
    assert writes == [record, record]
    assert _repository(changed).list_labs() == ["router-lab.east", "router-lab.west"]


def test_late_abandoned_worker_publishes_no_names(monkeypatch, tmp_path, otto_home):
    from otto.templating import render

    root = tmp_path / "sut"
    op = operation([_project(root, entries={"lab.jsonnet": _declaring("router-lab.east")})])
    repository = _repository(op)
    view = op.view.for_project(root=root)
    scope = repository.inputs.env.project_root
    started, release = threading.Event(), threading.Event()
    real = render._evaluate

    def blocking(*args: Any) -> str:
        """Evaluate once the test has given up on the operation."""
        started.set()
        assert release.wait(WAIT)
        return real(*args)

    monkeypatch.setattr(render, "_evaluate", blocking)
    failures: list[BaseException] = []

    def worker() -> None:
        with cancellation(view.render_memo.cancelled(scope)):
            try:
                repository.list_lab_status(view=view)
            except LabRepositoryError as e:
                failures.append(e)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert started.wait(WAIT)
    view.render_memo.cancel(scope=scope, reason="the completion deadline passed")
    release.set()
    thread.join(WAIT)
    assert not thread.is_alive()
    [failure] = failures
    assert isinstance(failure.__cause__, RenderCancelled)
    assert _records() == []
    assert repository.list_labs() == []


def test_cancel_just_before_publication_publishes_nothing(monkeypatch, tmp_path, otto_home):
    root = tmp_path / "sut"
    op = operation([_project(root, entries={"lab.jsonnet": _declaring("router-lab.east")})])
    repository = _repository(op)
    view = op.view.for_project(root=root)
    real = jsonnet_module.lab_statuses_of

    def statuses_then_cancel(documents: Any) -> Any:
        """Read the statuses, then give up on the operation before they are published."""
        statuses = real(documents)
        view.render_memo.cancel(
            scope=repository.inputs.env.project_root, reason="the completion deadline passed"
        )
        return statuses

    monkeypatch.setattr(jsonnet_module, "lab_statuses_of", statuses_then_cancel)
    with pytest.raises(LabRepositoryError, match="cancelled before its labs were listed") as e:
        repository.list_lab_status(view=view)
    assert isinstance(e.value.__cause__, RenderCancelled)
    assert _records() == []
    assert repository.list_labs() == []
