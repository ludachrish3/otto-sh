"""The hosts a run may target, and ``get_host``'s refusal of the rest (spec 2026-11-24 §3.5, 4.7).

A scoped command resolves only the hosts its active projects target, plus the
built-in runner and the containers on it, which are judged by identity. An
unscoped command resolves any lab host and never reads a scope. A miss is a
``KeyError``: ``HostOutOfScopeError`` for a lab host outside the scope,
naming why and the fix, and the plain unknown-id error, listing the
targetable ids, for an id the lab does not hold.

Real projects (parsed from a real ``settings.toml``) and real hosts (built by
the factory), through :func:`tests._fixtures.fleet.scope_workspace`: ``inscope``
declares ``alpha`` / ``a-dut.*``, ``outscope`` declares ``beta`` / ``b-dut.*``,
and ``a-jump`` / ``b-jump`` are targeted by neither. The names this change adds
are imported inside the tests, so the regression tests here run on the code
before it.
"""

import dataclasses
import logging
import re
from typing import Any, cast

import pytest

from otto.bootstrap import ProjectScopeError
from otto.config.scope import scopes_of
from otto.labs.loader import Lab
from tests._fixtures.fleet import (
    _host,
    _project,
    add_builtin_local,
    install_scoped_context,
    scope_workspace,
)


def _container(parent, service):
    """A docker container on *parent*, stamped with its parent's lab."""
    from otto.host.docker_host import DockerContainerHost

    host = DockerContainerHost(
        parent=parent,
        container_id=f"cid-{service}",
        project="r1",
        service=service,
        compose_project="otto-r1",
    )
    host.source_lab = parent.source_lab
    return host


def _settings(project) -> str:
    """The settings file a refusal names for *project*."""
    return f"{project.root}/.otto/settings.toml"


def _refusal(lookup, host_id: str) -> KeyError:
    """The ``KeyError`` *lookup* raises for *host_id*."""
    with pytest.raises(KeyError) as excinfo:
        lookup(host_id)
    return excinfo.value


# ── which hosts are targetable ─────────────────────────────────────────────────


def test_targetable_holds_runner_and_admissible(tmp_path):
    """Fail-first: the admitted set with the refusal off, the runner side included.

    ``a-jump`` is in the lab and in no declaration, so it is the one host out;
    the runner's container joins with the runner, and ``a-dut``'s with its parent.
    """
    ws = scope_workspace(tmp_path)
    ws.lab.add_host(_container(ws.lab.hosts["local"], "api"))
    ws.lab.add_host(_container(ws.lab.hosts["a-dut"], "db"))

    targetable = ws.ctx.targetable_ids()

    assert set(targetable) == ws.ctx.admissible_ids(require_nonempty=False)
    assert set(targetable) == {"a-dut", "local", "local.r1.api", "a-dut.r1.db"}


def test_targetable_in_lab_order(tmp_path):
    """Fail-first: a list in the lab's order, never sorted and never a set."""
    ws = scope_workspace(tmp_path, labs=("beta", "alpha"))
    both = _project(tmp_path, "both", labs=["alpha", "beta"], hosts=[".-dut"])
    ctx = install_scoped_context(ws.lab, [both])

    assert ctx.targetable_ids() == ["b-dut", "a-dut", "local"]


def test_unscoped_context_targetable_is_whole_lab(tmp_path):
    """Fail-first: an unscoped command may target every lab host, in the lab's order."""
    ws = scope_workspace(tmp_path)

    assert install_scoped_context(
        ws.lab, [ws.inscope, ws.outscope], scoped=False
    ).targetable_ids() == list(ws.lab.hosts)


def test_targetable_no_declaration_is_whole_lab(tmp_path):
    """Regression: with no project declaring a scope, every lab host resolves."""
    ws = scope_workspace(tmp_path)
    ctx = install_scoped_context(ws.lab, [_project(tmp_path, "plain")])

    for host_id, host in ws.lab.hosts.items():
        assert ctx.get_host(host_id) is host


def test_scope_same_for_component_order(tmp_path):
    """Fail-first: ``-l alpha+beta`` and ``-l beta+alpha`` under ``-E outscope`` agree.

    The runner's ``source_lab`` stamp is the first component, so the two
    orders stamp it, and the container on it, differently. The targetable set,
    the hosts in play and the switched-off refusal of ``b-dut`` must not change.
    """
    from otto.fleet import get_hosts_in_play

    seen = []
    for labs in (("alpha", "beta"), ("beta", "alpha")):
        root = tmp_path / "+".join(labs)
        root.mkdir()
        ws = scope_workspace(root, labs=labs, exclude_projects=("outscope",))
        runner = ws.lab.hosts["local"]
        assert runner.source_lab == labs[0]
        container = _container(runner, "api")
        ws.lab.add_host(container)
        assert ws.ctx.get_host(container.id) is container
        refusal = str(_refusal(ws.ctx.get_host, "b-dut")).replace(str(root), "<root>")
        seen.append((set(ws.ctx.targetable_ids()), get_hosts_in_play(), refusal))

    forward, backward = seen
    assert forward == backward
    assert forward == (
        {"a-dut", "local", "local.r1.api"},
        {"a-dut"},
        (
            "host 'b-dut' is in lab 'beta' but outside this run's scope: only switched-off "
            "projects target it (outscope). Drop -E outscope to reach it."
        ),
    )


# ── get_host's refusals ────────────────────────────────────────────────────────


def test_get_host_out_of_scope_text(tmp_path):
    """Fail-first: a lab host no declaration targets refuses, naming every file to widen.

    ``str()`` is the message itself, not ``KeyError``'s quoted form. ``outscope``
    applies to no loaded lab, yet widening its ``lab_patterns`` is a fix, so its
    file is named too.
    """
    from otto.lab import HostOutOfScopeError

    ws = scope_workspace(tmp_path)

    error = _refusal(ws.ctx.get_host, "a-jump")

    assert isinstance(error, HostOutOfScopeError)
    assert str(error) == (
        "host 'a-jump' is in lab 'alpha' but outside this run's scope: no declared project "
        "targets it. To reach it, add its id to [project] host_patterns (or its lab to "
        f"lab_patterns) in {_settings(ws.inscope)}, {_settings(ws.outscope)}."
    )
    assert error.outside is not None
    assert error.outside.kind == "out_of_scope"


def test_get_host_switched_off_text(tmp_path):
    """Fail-first: a host only a switched-off project targets names the ``-E`` to drop."""
    from otto.lab import HostOutOfScopeError

    ws = scope_workspace(tmp_path, labs=("alpha", "beta"), exclude_projects=("outscope",))

    error = _refusal(ws.ctx.get_host, "b-dut")

    assert isinstance(error, HostOutOfScopeError)
    assert str(error) == (
        "host 'b-dut' is in lab 'beta' but outside this run's scope: only switched-off "
        "projects target it (outscope). Drop -E outscope to reach it."
    )
    assert error.outside is not None
    assert error.outside.switched_off == ("outscope",)


def test_get_host_empty_scope_fleet_text(tmp_path):
    """Fail-first: with no host in play the refusal is the fleet-shaped empty-scope text.

    ``-E inscope`` leaves nothing in play (``outscope``'s lab is not loaded),
    so the refusal says what a fleet walk would, word for word.
    """
    from otto.lab import HostOutOfScopeError

    ws = scope_workspace(tmp_path, exclude_projects=("inscope",))
    with pytest.raises(ProjectScopeError) as walk:
        ws.ctx.admissible_ids()

    error = _refusal(ws.ctx.get_host, "a-dut")

    assert isinstance(error, HostOutOfScopeError)
    assert str(error) == str(walk.value)
    assert "--exclude-projects" in str(error)


def test_get_host_empty_fleet_text_names_the_settings(tmp_path):
    """Fail-first: no declaration admits a host here, so the refusal is the empty-fleet text.

    Nothing was switched off, so the text names the declarations and their
    files on its ``settings:`` line, word for word as a fleet walk would.
    """
    from otto.lab import HostOutOfScopeError

    ws = scope_workspace(tmp_path)
    narrow = _project(tmp_path, "narrow", labs=["alpha"], hosts=["nothing-here"])
    ctx = install_scoped_context(ws.lab, [narrow, ws.outscope])
    with pytest.raises(ProjectScopeError) as walk:
        ctx.admissible_ids()

    error = _refusal(ctx.get_host, "a-dut")

    assert isinstance(error, HostOutOfScopeError)
    assert str(error) == str(walk.value)
    assert f"    settings:     {_settings(narrow)}, {_settings(ws.outscope)}\n" in str(error)


def test_get_host_unknown_lists_targetable_only(tmp_path):
    """Fail-first: an id the lab does not hold is a plain ``KeyError`` listing targetable ids."""
    from otto.lab import HostOutOfScopeError

    ws = scope_workspace(tmp_path)

    error = _refusal(ws.ctx.get_host, "nope")

    assert not isinstance(error, HostOutOfScopeError)
    assert error.args[0] == "No host 'nope' in lab 'alpha'. Available: ['a-dut', 'local']"


def test_lab_local_host_is_scoped(tmp_path):
    """Fail-first: a lab entry reusing the id ``local`` is an ordinary host, judged by patterns."""
    from otto.lab import HostOutOfScopeError

    lab_local = _host("local", "alpha", 9, subnet="192.0.2")
    ws = scope_workspace(tmp_path, local_host=False, extra_hosts=[lab_local])

    assert isinstance(_refusal(ws.ctx.get_host, "local"), HostOutOfScopeError)
    assert ws.ctx.targetable_ids() == ["a-dut"]


def test_runner_container_follows_runner(tmp_path):
    """Fail-first: a container resolves exactly when its parent does; on the runner, always."""
    from otto.lab import HostOutOfScopeError

    ws = scope_workspace(tmp_path)
    on_runner = _container(ws.lab.hosts["local"], "api")
    on_dut = _container(ws.lab.hosts["a-dut"], "db")
    on_jump = _container(ws.lab.hosts["a-jump"], "cache")
    for container in (on_runner, on_dut, on_jump):
        ws.lab.add_host(container)

    assert ws.ctx.get_host(on_runner.id) is on_runner
    assert ws.ctx.get_host(on_dut.id) is on_dut
    error = _refusal(ws.ctx.get_host, on_jump.id)
    assert isinstance(error, HostOutOfScopeError)
    assert str(error).startswith(f"host {on_jump.id!r} is in lab 'alpha' but outside")


def test_builtin_runner_always_resolves(tmp_path):
    """Regression: the built-in runner resolves whatever the declarations and switches say.

    Under a declaration that misses it, with every declaring project switched
    off (nothing in play), and with no loaded lab any declaration applies to.
    """
    for case, kwargs in {
        "declared": {},
        "switched-off": {"exclude_projects": ("inscope", "outscope")},
        "no-applicable-lab": {"labs": ("beta",), "exclude_projects": ("outscope",)},
    }.items():
        root = tmp_path / case
        root.mkdir()
        ws = scope_workspace(root, **kwargs)
        assert ws.ctx.get_host("local") is ws.lab.hosts["local"], case


@pytest.mark.asyncio
@pytest.mark.parametrize("back_reference", [True, False], ids=["host-lab", "installed"])
async def test_power_controller_resolves_out_of_scope(tmp_path, monkeypatch, back_reference):
    """Regression: a power controller needs only lab membership, like a hop.

    ``a-jump`` is outside the scope, and ``a-dut``'s controller runs on it,
    whether found through the host's own lab or the installed resolver.
    """
    from otto.host.power import CommandPowerConfig, _CommandPowerController
    from otto.result import CommandResult
    from otto.utils import Status

    ws = scope_workspace(tmp_path)
    target = ws.ctx.get_host("a-dut")
    ran: list[tuple[str, str]] = []

    async def _exec(self, command, *_args, **_kwargs):
        ran.append((self.id, command))
        return CommandResult(status=Status.Success, value="", command=command, retcode=0)

    # Host classes are slotted, so the class is patched; only the controller runs.
    monkeypatch.setattr(type(ws.lab.hosts["a-jump"]), "exec", _exec)
    if not back_reference:
        monkeypatch.setattr(target, "_lab", None)
    power = _CommandPowerController(
        CommandPowerConfig(on_cmd="on {id}", off_cmd="off {id}", controller="a-jump")
    )

    result = await power.on(target)

    assert result.status is Status.Success
    assert ran == [("a-jump", "on a-dut")]


# ── one judge, every spelling ──────────────────────────────────────────────────


def test_four_library_spellings_agree(tmp_path):
    """Fail-first: the context, ``otto``, ``otto.lab`` and the project view resolve alike."""
    import otto
    import otto.lab
    from otto.lab import HostOutOfScopeError

    ws = scope_workspace(tmp_path)
    spellings = {
        "ctx": ws.ctx.get_host,
        "otto": otto.get_host,
        "otto.lab": otto.lab.get_host,
        "view": ws.ctx.for_project(name="outscope").get_host,
    }

    for name, lookup in spellings.items():
        assert lookup("a-dut") is ws.lab.hosts["a-dut"], name
    refusals = {name: _refusal(lookup, "a-jump") for name, lookup in spellings.items()}
    assert all(isinstance(error, HostOutOfScopeError) for error in refusals.values())
    assert len({str(error) for error in refusals.values()}) == 1


def test_get_host_success_judges_one_id(tmp_path, monkeypatch):
    """Fail-first: a successful lookup judges the named id once and never builds the fleet."""
    from otto.context import OttoContext
    from otto.host.targeting import HostScope

    ws = scope_workspace(tmp_path)
    scopes_of(ws.ctx)  # resolved ahead, so only the lookup is measured
    judged: list[str] = []
    admits = HostScope.admits

    def _counted(self, host_id):
        judged.append(host_id)
        return admits(self, host_id)

    def _refused(*_args, **_kwargs):
        raise AssertionError("a successful lookup read the whole fleet")

    monkeypatch.setattr(HostScope, "admits", _counted)
    monkeypatch.setattr(HostScope, "targetable_ids", _refused)
    monkeypatch.setattr(HostScope, "refusal", _refused)
    monkeypatch.setattr(OttoContext, "_hosts_in_play", _refused)

    assert ws.ctx.get_host("a-dut") is ws.lab.hosts["a-dut"]
    assert judged == ["a-dut"]


# ── an unscoped command never reads a scope ───────────────────────────────────


def test_unscoped_command_get_host_resolves_out_of_scope_host(tmp_path):
    """Regression: an unscoped command resolves a host no declaration targets."""
    import otto

    ws = scope_workspace(tmp_path)
    ctx = install_scoped_context(ws.lab, [ws.inscope, ws.outscope], scoped=False)

    assert ctx.get_host("a-jump") is ws.lab.hosts["a-jump"]
    assert otto.get_host("a-jump") is ws.lab.hosts["a-jump"]


def test_unscoped_lookup_resolves_no_scope(tmp_path, monkeypatch):
    """Regression: an unscoped lookup resolves no scope and discovers no project.

    Scope resolution and project discovery raise here, so a lookup that read
    either would fail; an unknown id still refuses with the plain miss.
    """
    from otto.context import OttoContext

    ws = scope_workspace(tmp_path)
    ctx = install_scoped_context(ws.lab, [ws.inscope, ws.outscope], scoped=False)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("an unscoped lookup read the scope")

    monkeypatch.setattr(OttoContext, "_resolve_scopes", _forbidden)
    monkeypatch.setattr(OttoContext, "_bootstrap_snapshot", _forbidden)
    monkeypatch.setattr("otto.declared.compose", _forbidden)

    for host_id, host in ws.lab.hosts.items():
        assert ctx.get_host(host_id) is host
        assert ctx.lab.hosts[host_id] is host
    assert "No host 'nope'" in _refusal(ctx.get_host, "nope").args[0]


# ── the refusal record and its renderers ──────────────────────────────────────


def test_host_or_value_error_both_branches():
    """Fail-first: an out-of-scope miss keeps its text, any other miss takes the caller's."""
    from types import SimpleNamespace

    from otto.config.scope import HostOutOfScopeError
    from otto.host.targeting import host_or_value_error

    found = object()

    class _Hosts:
        def __getitem__(self, host_id):
            if host_id == "ok":
                return found
            if host_id == "outside":
                raise HostOutOfScopeError("host 'outside' is outside this run's scope")
            raise KeyError(host_id)

    lab = cast("Any", SimpleNamespace(hosts=_Hosts()))

    assert host_or_value_error(lab, "ok", unknown_message="unused") is found
    with pytest.raises(ValueError, match=r"^host 'outside' is outside this run's scope$") as out:
        host_or_value_error(lab, "outside", unknown_message="unused")
    assert isinstance(out.value.__cause__, HostOutOfScopeError)
    with pytest.raises(ValueError, match=r"^link references host 'nope'$"):
        host_or_value_error(lab, "nope", unknown_message="link references host 'nope'")


_ONE_SOURCE_CASES = {
    "none-off": (),
    "outscope-off": ("outscope",),
    "all-off": ("narrow", "outscope"),
    "undeclared-owner": ("outscope",),
}
"""Each case's ``-E`` list; ``undeclared-owner`` also walks as the undeclared ``plain``."""


@pytest.mark.parametrize("case", list(_ONE_SOURCE_CASES))
def test_settings_files_one_source(tmp_path, case):
    """Fail-first: the out-of-scope record, the empty-scope text and the judge name the same files.

    ``narrow`` targets nothing in ``alpha`` and ``outscope`` applies to no
    loaded lab, so nothing is in play: the fleet walk refuses with the
    empty-scope text, whose ``settings:`` line is compared with the rest. The
    files are the declaring projects' less the switched-off ones in every
    case: ``all-off`` is the fallback that names every declaration on its
    ``declared by:`` line, and ``undeclared-owner`` is a walk bound to a
    project that declares nothing (its lab holds only the runner, so its walk
    is empty too). Neither may offer a switched-off project's file, and with
    no file left to name the ``settings:`` line is left out.
    """
    from otto.config.scope import host_outside

    exclude = _ONE_SOURCE_CASES[case]
    ws = scope_workspace(tmp_path)
    narrow = _project(tmp_path, "narrow", labs=["alpha"], hosts=["nothing-here"])
    lab, projects, owner = ws.lab, [narrow, ws.outscope], None
    if case == "undeclared-owner":
        lab = add_builtin_local(Lab(name="alpha"))
        projects.append(_project(tmp_path, "plain"))
        owner = "plain"
    ctx = install_scoped_context(lab, projects, exclude_projects=exclude)
    expected = [_settings(p) for p in (narrow, ws.outscope) if p.name not in exclude]

    with pytest.raises(ProjectScopeError) as walk:
        ctx.admissible_ids(owner=owner)
    lines = re.findall(r"^    settings:     (.*)$", str(walk.value), flags=re.MULTILINE)

    assert lines == ([", ".join(expected)] if expected else [])
    assert ctx._host_scope().settings_files() == expected
    if "a-dut" in lab.hosts:
        outside = host_outside(
            lab.hosts["a-dut"], scopes_of(ctx), lab_name=lab.name, exclude_projects=exclude
        )
        assert outside is not None
        assert list(outside.settings_files) == expected


def test_fleet_line_reasons_from_host_outside(tmp_path, monkeypatch, caplog):
    """Fail-first: the fleet-of-interest line names each outside host's reason from its record.

    The stand-in record says ``switched_off`` for a host no project targets,
    so a line that judged the host itself would still say "no declared
    project targets it".
    """
    from otto.config import scope

    real = scope.host_outside

    def _stand_in(host, scopes, **kwargs):
        record = real(host, scopes, **kwargs)
        if record is None:
            return None
        return dataclasses.replace(record, kind="switched_off", switched_off=("stand-in",))

    monkeypatch.setattr(scope, "host_outside", _stand_in)
    ws = scope_workspace(tmp_path)

    with caplog.at_level(logging.INFO, logger="otto.context"):
        scopes_of(ws.ctx)
    (line,) = [r.message for r in caplog.records if "fleet of interest" in r.message]

    assert line.endswith("outside it: a-jump (only switched-off projects target it)")


def test_unscoped_judge_names_no_settings_files(tmp_path):
    """An unscoped judge has no scope to widen: no files, and no verdicts resolved."""
    from otto.host.targeting import HostScope

    ws = scope_workspace(tmp_path)

    def _never():
        raise AssertionError("an unscoped judge resolved the verdicts")

    assert HostScope(lab=ws.lab, scoped=False, verdicts=_never).settings_files() == []
    unscoped = install_scoped_context(ws.lab, [ws.inscope, ws.outscope], scoped=False)
    assert unscoped._host_scope().settings_files() == []
    assert ws.ctx._host_scope().settings_files() == [
        str(ws.inscope.root / ".otto" / "settings.toml"),
        str(ws.outscope.root / ".otto" / "settings.toml"),
    ]


def test_a_refusal_with_no_file_to_widen_omits_the_files():
    """Fail-first: with no settings file to name, the refusals leave the line out, never ``(none)``.

    Near-unreachable (a refusal names the files of every declaring,
    not-switched-off project), but an empty field reads as a fault, so the
    host refusal and the docker refusal both end before it.
    """
    import types
    from unittest.mock import patch

    from otto.config.scope import HostOutside, outside_message
    from otto.docker.observe import DockerVerbError, default_docker_parent
    from tests._fixtures.fleet import _lab

    outside = HostOutside(
        host_id="test3", lab="bench", kind="out_of_scope", switched_off=(), settings_files=()
    )
    assert outside_message(outside) == (
        "host 'test3' is in lab 'bench' but outside this run's scope: no declared project "
        "targets it. To reach it, add its id to [project] host_patterns (or its lab to "
        "lab_patterns)."
    )

    whole = _lab(("test3", "bench"))
    whole.hosts["test3"].docker_capable = True
    view = types.SimpleNamespace(name="bench", hosts={})
    with (
        patch("otto.lab_view.whole_lab", return_value=whole),
        patch("otto.lab_view.settings_files_of", return_value=[]),
        pytest.raises(DockerVerbError) as refused,
    ):
        default_docker_parent(lab=view)
    assert str(refused.value) == (
        "no docker-capable host in lab 'bench' is in scope; capable but outside: test3"
    )
