"""Unit tests for ``otto reservation whoami`` and ``otto reservation check``.

Both commands accept a ``typer.Context`` (which is a thin wrapper around a
click.Context) and read ``ctx.meta["otto_reservation"]``. We construct a
click.Context directly so we can populate ``.meta`` without running the
top-level main callback.
"""

import click
import pytest
import typer

from otto.cli.reservation import check, whoami
from otto.config.lab import Lab
from otto.reservations import (
    NullReservationBackend,
    ReservationBackendError,
    ReservationGate,
    ResolvedIdentity,
)
from tests._fixtures.fleet import _repo, install_scoped_context

# The raises-checks below catch what the production code raises: typer.Exit.
# Under typer >= 0.26 that is typer's own vendored exception
# (typer._click.exceptions.Exit), which is NOT the real click.exceptions.Exit —
# spell the typer name, never click's class (and never a local alias: the
# typer-exit-raises-must-assert-code gate can only see the literal spelling).


def _make_ctx(meta: dict) -> typer.Context:
    """Build a typer.Context (backed by click.Context) with the given meta."""
    cmd = click.Command("reservation")
    ctx = click.Context(cmd)
    ctx.meta.update(meta)
    # typer.Context is a subclass of click.Context, so cast is valid here
    return ctx  # type: ignore[return-value]


def _root_options(labs: list[str] | None):
    """A RootOptions with only the fields the reservation commands read set meaningfully."""
    from tests._fixtures.rootoptions import make_root_options

    return make_root_options(labs=labs)


class _FakeBackend:
    def backend_name(self) -> str:
        return "fake"

    def get_reserved_resources(self, username: str) -> set[str]:
        return {"r1"}

    def who_reserved(self, resource: str) -> list[str]:
        return ["alice"]


# ── whoami ─────────────────────────────────────────────────────────────────────


def test_whoami_exits_1_when_no_identity(capsys):
    res = ReservationGate(backend=None, identity=None, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})
    with pytest.raises(typer.Exit) as exc:
        whoami(ctx)
    assert exc.value.exit_code == 1


def test_whoami_exits_1_when_no_reservation_key(capsys):
    """Without the top-level callback, ctx.meta has no key — whoami exits 1 via identity=None path."""  # noqa: E501 — descriptive docstring
    ctx = _make_ctx({})
    # res = ctx.meta.get("otto_reservation") returns None → identity is None → Exit(1)
    with pytest.raises(typer.Exit) as exc:
        whoami(ctx)
    assert exc.value.exit_code == 1


def test_whoami_prints_identity_when_configured(capsys):
    identity = ResolvedIdentity(username="alice", source="$USER")
    backend = _FakeBackend()
    res = ReservationGate(backend=backend, identity=identity, skip_check=False)
    # No lab anywhere: no root options, no loaded lab — whoami must not care.
    ctx = _make_ctx({"otto_reservation": res})

    whoami(ctx)  # must not raise

    captured = capsys.readouterr()
    assert "alice" in captured.out
    assert "fake" in captured.out
    assert "<none>" in captured.out  # the lab line reports no lab was named


def test_whoami_reports_requested_lab_names_without_loading(capsys):
    """whoami echoes the --lab names from root options; it never loads the lab."""
    from otto.cli.invoke import RootOptions

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=_FakeBackend(), identity=identity, skip_check=False)
    opts = _root_options(labs=["tech1", "overlay"])
    ctx = _make_ctx({"otto_reservation": res, "_otto_root_options": opts})
    assert isinstance(opts, RootOptions)

    whoami(ctx)

    assert "tech1+overlay" in capsys.readouterr().out


def test_whoami_is_lab_free(capsys):
    """With no preamble-populated state, whoami resolves identity + backend from
    repo settings + root options alone — the lab-free path."""
    from unittest.mock import patch

    identity = ResolvedIdentity(username="alice", source="$USER")
    state = ReservationGate(backend=_FakeBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({"_otto_root_options": _root_options(labs=None)})

    with (
        patch("otto.cli.reservation.build_reservation_gate", return_value=state) as build,
        patch("otto.config.get_repos", return_value=[]),
    ):
        whoami(ctx)

    build.assert_called_once()
    out = capsys.readouterr().out
    assert "alice" in out
    assert "<none>" in out
    # The resolved state is memoized for any later subcommand in the invocation.
    assert ctx.meta["otto_reservation"] is state


# ── check ──────────────────────────────────────────────────────────────────────


_CELL = "\N{BOX DRAWINGS LIGHT VERTICAL}"
"""``box.ROUNDED``'s cell border — the one character only a table row carries.

Used as an extra fragment wherever a plain-prose line elsewhere in the output
could satisfy the same anchor (the missing-reservation error names the resource
and its level too), so the assertion pins the TABLE rather than the capture."""


def _table_row(out: str, *fragments: str) -> bool:
    """Report whether ONE rendered line holds every fragment.

    A bare ``"host"``, ``"lab"`` or ``"no"`` turns up all over a Rich table's
    chrome, its title and the surrounding prose; the level and the held flag
    only mean anything ON the row that names the resource. Scanning lines is
    what keeps these assertions pinned to the row rather than to the capture.
    """
    return any(all(fragment in line for fragment in fragments) for line in out.splitlines())


def _flat(out: str) -> str:
    """Collapse every run of whitespace, so a wrapped line still reads as one string.

    Rich word-wraps a table's TITLE at the table's own width — which is set by
    the widest cell, not by ``COLUMNS`` — so a title assertion that spanned a
    fold would fail for a reason that has nothing to do with the requirement.
    """
    return " ".join(out.split())


def _slot_host(fixture_id: str, host_id: str, resource: str):
    """A host carrying resources at BOTH the element and the host level.

    ``make_host`` looks its argument up as a tech1 fixture element name (see
    ``tests/_fixtures/labdata.py:host_data``), so a real fixture id builds a
    valid host and the reservation-relevant fields are overridden afterwards —
    plain dataclass fields, the same shape
    ``tests/unit/reservations/test_check.py:_three_level_lab`` uses. The
    element is left with ``element_id=None`` so the rendered owner is
    ``chassis``, which is the origin the table has to show.
    """
    from tests.conftest import make_host

    host = make_host(fixture_id)
    host.id = host_id
    host.element, host.element_id = "chassis", None
    host.element_resources = frozenset({"chassis-1"})
    host.resources = frozenset({resource})
    return host


def _rig_lab(*hosts) -> Lab:
    """A lab named ``rig`` holding *hosts* — ``add_host`` stamps their ``source_lab``."""
    lab = Lab(name="rig", resources=set())
    for host in hosts:
        lab.add_host(host)
    return lab


class _HoldsSlot1(_FakeBackend):
    """Holds the chassis and ``slot-1`` — deliberately NOT ``slot-2``.

    That gap is what makes the narrowing testable at both call sites: a
    ``check`` that computed either the table or the verdict over the whole lab
    would find ``slot-2`` unheld and exit 1.
    """

    def get_reserved_resources(self, username: str) -> set[str]:
        return {"chassis-1", "slot-1"}


def test_check_exits_1_when_not_configured(capsys):
    ctx = _make_ctx(
        {"otto_reservation": ReservationGate(backend=None, identity=None, skip_check=False)}
    )
    with pytest.raises(typer.Exit) as exc:
        check(ctx)
    assert exc.value.exit_code == 1


def test_check_passes_when_fully_reserved(capsys, monkeypatch):
    identity = ResolvedIdentity(username="alice", source="$USER")
    backend = _FakeBackend()
    res = ReservationGate(backend=backend, identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab", resources={"r1"})
    install_scoped_context(monkeypatch, lab, [])
    check(ctx)  # must not raise

    assert "OK" in capsys.readouterr().out


def test_check_exits_1_on_missing_reservation(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")

    class _EmptyBackend(_FakeBackend):
        def get_reserved_resources(self, username: str) -> set[str]:
            return set()

        def who_reserved(self, resource: str) -> list[str]:
            return []

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=_EmptyBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab", resources={"r1"})
    install_scoped_context(monkeypatch, lab, [])
    with pytest.raises(typer.Exit) as exc:
        check(ctx)
    assert exc.value.exit_code == 1
    # The table still renders, and the unheld row is flagged — the requirement
    # is shown BEFORE the verdict, so a failing check explains itself. The cell
    # border is part of the anchor on purpose: MissingReservationError's own
    # "(held by: nobody)" line names the same resource, the same level and
    # contains "no", so a borderless anchor would be satisfied by the error.
    assert _table_row(capsys.readouterr().out, _CELL, "r1", "lab", "no")


def test_check_prints_the_requirement_table_before_the_verdict(capsys, monkeypatch):
    """Every requirement is shown with its ORIGIN — the slot, not just the string."""
    monkeypatch.setenv("COLUMNS", "300")

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=_HoldsSlot1(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = _rig_lab(_slot_host("test1", "chassis1", "slot-1"))
    install_scoped_context(monkeypatch, lab, [])

    check(ctx)

    out = capsys.readouterr().out
    assert "resource" in out
    assert "level" in out
    assert "owner" in out
    assert "held" in out
    assert _table_row(out, "chassis-1", "element", "│ chassis  │", "yes")
    assert _table_row(out, "slot-1", "host", "chassis1", "yes")
    # The title's IDENTITY half, not just its count: a table that named the
    # wrong lab or the wrong user would still be a correct-looking table.
    assert "reservations required by lab rig for alice" in _flat(out)
    assert out.index("chassis-1") < out.index("OK — all required resources are reserved")


def test_check_table_covers_only_the_hosts_in_play(capsys, monkeypatch, tmp_path):
    """A declaring repo narrows the fleet, and the requirement narrows with it."""
    monkeypatch.setenv("COLUMNS", "300")

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=_HoldsSlot1(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = _rig_lab(
        _slot_host("test1", "chassis1", "slot-1"),
        _slot_host("test2", "chassis2", "slot-2"),
    )
    install_scoped_context(
        monkeypatch, lab, [_repo(tmp_path, "r1", labs=["rig"], hosts=["chassis1"])]
    )

    check(ctx)  # must not raise: slot-2 is unheld, but chassis2 is out of play

    out = capsys.readouterr().out
    assert _table_row(out, "slot-1", "host", "chassis1")
    # chassis2 is outside this run's fleet of interest, so its host-level
    # resource is neither a row in the table nor a requirement of the verdict.
    assert "slot-2" not in out
    assert "(1 host(s) in play)" in _flat(out)
    assert "OK — all required resources are reserved" in out


def test_check_under_an_empty_declared_fleet_reports_zero_hosts_in_play(
    capsys, monkeypatch, tmp_path
):
    """A declaration that admits nothing is 0 hosts in play, not a refusal from this command.

    ``check`` reports; it walks nothing, so the fleet-shaped abort belongs to
    the walk a run would do next and not here. With no host selected the
    requirement is the lab's own set — the element and host rows have no host
    to come from — and the title says so.

    Mutation: pass ``require_nonempty=True`` at ``reservation.py``'s
    ``admissible_ids`` call and this goes red with ``ProjectScopeError``.
    """
    monkeypatch.setenv("COLUMNS", "300")

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=_FakeBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = _rig_lab(_slot_host("test1", "chassis1", "slot-1"))
    lab.resources = {"r1"}
    install_scoped_context(
        monkeypatch, lab, [_repo(tmp_path, "r1", labs=["rig"], hosts=["nothing-matches"])]
    )

    check(ctx)

    out = capsys.readouterr().out
    assert _table_row(out, _CELL, "r1", "lab", "rig", "yes")
    assert "(0 host(s) in play)" in _flat(out)
    # No host is in play, so neither the element's set nor the host's is required.
    assert "chassis-1" not in out
    assert "slot-1" not in out
    assert "OK — all required resources are reserved" in out


def test_check_says_so_when_nothing_is_required(capsys, monkeypatch):
    """An empty requirement gets a sentence, and NO table at all.

    An empty bordered header box above the sentence is chrome that says
    nothing — the sentence is the whole message.
    """
    monkeypatch.setenv("COLUMNS", "300")

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=_FakeBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    install_scoped_context(monkeypatch, _rig_lab(), [])

    check(ctx)

    out = capsys.readouterr().out
    assert "(this lab requires no reservation for the hosts in play)" in _flat(out)
    assert "OK — all required resources are reserved" in out
    assert _CELL not in out  # no table was drawn


def test_check_does_not_query_the_backend_when_nothing_is_required(capsys, monkeypatch):
    """A backend outage must not fail a run that needs no reservation.

    ``check_reservations`` computes the requirement and returns on an empty one
    BEFORE it ever queries; the table has to make the same call, or the command
    starts failing where it used to succeed.
    """
    monkeypatch.setenv("COLUMNS", "300")

    class _UnreachableBackend(_FakeBackend):
        def get_reserved_resources(self, username: str) -> set[str]:
            raise ReservationBackendError("unreachable")

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=_UnreachableBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    install_scoped_context(monkeypatch, _rig_lab(), [])

    check(ctx)  # must not raise: nothing is required, so nothing is asked

    out = capsys.readouterr().out
    assert "(this lab requires no reservation for the hosts in play)" in _flat(out)
    assert "OK — all required resources are reserved" in out


def test_check_table_renders_n_a_under_the_null_backend(capsys, monkeypatch):
    """The ``"none"`` backend reserves nothing, so ``held`` has no answer to give.

    ``check_reservations`` short-circuits on it and prints OK. A table that
    queried it anyway would get ``set()`` back and render every requirement as
    unheld directly above that OK line — the display contradicting the verdict.
    """
    monkeypatch.setenv("COLUMNS", "300")

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=NullReservationBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = _rig_lab(_slot_host("test1", "chassis1", "slot-1"))
    install_scoped_context(monkeypatch, lab, [])

    check(ctx)  # must not raise

    out = capsys.readouterr().out
    assert _table_row(out, _CELL, "slot-1", "host", "n/a")
    assert _table_row(out, _CELL, "chassis-1", "element", "n/a")
    # Not one cell claims the requirement is unheld. Anchored on the rendered
    # CELL — rich pads a cell to its column width, so an unheld row carries
    # " no " with the padding — never on a bare "no": that is two letters
    # inside any identifier or owner a real lab might carry (`slot-north`),
    # and this assertion would then be red for a reason it is not about.
    assert not _table_row(out, _CELL, " no ")
    assert "OK — all required resources are reserved" in out


def test_check_renders_a_resource_that_looks_like_markup_verbatim(capsys, monkeypatch):
    """Resource identifiers are OPAQUE — rich must not read one as a style tag.

    ``rack[a]`` is a legal identifier and rich's markup parser accepts any tag
    opening with a letter, so an unescaped cell renders as ``rack`` — the table
    would name a resource that is not the one being checked, which is the
    silent-wrong-answer this table exists to prevent.
    """
    monkeypatch.setenv("COLUMNS", "300")

    class _HoldsBracketed(_FakeBackend):
        def get_reserved_resources(self, username: str) -> set[str]:
            return {"rack[a]"}

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=_HoldsBracketed(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    install_scoped_context(monkeypatch, Lab(name="rig", resources={"rack[a]"}), [])

    check(ctx)

    out = capsys.readouterr().out
    assert _table_row(out, _CELL, "rack[a]", "lab", "rig", "yes")


def test_whoami_builds_backend_on_demand(capsys):
    identity = ResolvedIdentity(username="alice", source="--as-user")
    # -R shape: backend not built, but a factory is available.
    res = ReservationGate(
        backend=None,
        identity=identity,
        skip_check=True,
        backend_factory=_FakeBackend,
    )
    ctx = _make_ctx({"otto_reservation": res})

    whoami(ctx)

    out = capsys.readouterr().out
    assert "alice" in out
    assert "fake" in out  # backend_name() from the factory-built backend


def test_check_loads_lab_lazily_when_preamble_skipped(capsys, monkeypatch):
    """The lab_free group means check must pull the lab in itself."""
    from unittest.mock import patch

    identity = ResolvedIdentity(username="alice", source="$USER")
    state = ReservationGate(backend=_FakeBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({})

    def _fake_ensure(c):
        c.meta["otto_reservation"] = state

    lab = Lab(name="test_lab", resources={"r1"})
    install_scoped_context(monkeypatch, lab, [])
    with patch("otto.cli.invoke.ensure_lab_context", side_effect=_fake_ensure) as ensure:
        check(ctx)

    ensure.assert_called_once()
    assert "OK" in capsys.readouterr().out


def test_check_without_lab_exits_with_usage_error(capsys):
    """No --lab → check reports the missing option through the shared loud path."""
    from unittest.mock import patch

    from otto.cli.invoke import LabContextError

    ctx = _make_ctx({})
    err = LabContextError("Error: Missing option '--lab'.", exit_code=2, rich=False)

    with (
        patch("otto.cli.invoke.ensure_lab_context", side_effect=err),
        pytest.raises(typer.Exit) as exc,
    ):
        check(ctx)
    assert exc.value.exit_code == 2


def test_check_builds_backend_on_demand(capsys, monkeypatch):
    identity = ResolvedIdentity(username="alice", source="--as-user")
    res = ReservationGate(
        backend=None,
        identity=identity,
        skip_check=True,
        backend_factory=_FakeBackend,
    )
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab", resources={"r1"})
    install_scoped_context(monkeypatch, lab, [])
    check(ctx)  # _FakeBackend reserves {"r1"} for everyone → passes

    assert "OK" in capsys.readouterr().out
