"""Unit tests for ``ReservationGate.evaluate()`` — the typer-free library gate.

Outcome matrix (per user, per backend state):

* ``skip_check=True``            -> skipped=True,  warning contains "SKIPPED"
* ``backend=None`` (no skip)     -> checked=False, skipped=False, warning=None
* backend configured, missing    -> raises MissingReservationError
* backend configured, fully held -> checked=True,  skipped=False, warning=None

Plus an import-hygiene guard: importing ``otto.reservations`` must never pull
in ``typer`` — that is the whole point of extracting the gate out of the CLI.

Every case installs a REAL ``OttoContext``: since spec 2026-08-28
three-level-reservations §5 the gate computes its requirement over
``OttoContext.admissible_ids()``, so a lab handed over by patching
``otto.config.get_lab`` alone would leave the fleet half of the read unwired.
"""

import dataclasses

import pytest

from otto.config.lab import Lab
from otto.reservations import (
    MissingReservationError,
    NullReservationBackend,
    Reservation,
    ReservationBackendBase,
    ReservationGate,
    ReservationGateResult,
    ResolvedIdentity,
)
from tests._fixtures.fleet import _lab as fleet_lab
from tests._fixtures.fleet import _repo, add_builtin_local, install_scoped_context
from tests.conftest import make_host


class _FakeBackend(ReservationBackendBase):
    """Minimal in-memory ReservationBackend for testing the gate."""

    def __init__(
        self,
        owners: dict[str, str],
        *,
        username: "str | None" = None,
        ends: "dict | None" = None,
    ) -> None:
        super().__init__(username=username)
        self.owners = owners  # resource -> username
        self.ends = dict(ends or {})  # resource -> datetime, for the expiry warning

    def fetch_reservations(self, username, start=None, end=None):
        return [
            Reservation(user=username, resource=r, end=self.ends.get(r))
            for r, u in self.owners.items()
            if u == username
        ]

    def holders(self, resource):
        u = self.owners.get(resource)
        return [Reservation(user=u, resource=resource)] if u is not None else []

    def backend_name(self) -> str:
        return "fake"


def _lab_with_resources() -> Lab:
    """Build a lab declaring {rack1} that also holds hosts.

    The hosts are the point: the gate must demand the LAB's declaration and
    nothing else. These hosts contribute nothing because ``make_host`` leaves
    ``resources`` and ``element_resources`` at their empty defaults — NOT
    because a host cannot carry them (since spec 2026-08-28
    three-level-reservations it can). A lab whose hosts declare their own is
    covered in ``test_check.py``; this guards the lab level in isolation.
    """
    return Lab(
        name="test_lab",
        resources={"rack1"},
        hosts={
            "test1": make_host("test1"),
            "test2": make_host("test2"),
        },
    )


def _lab_declaring(*resources: str) -> Lab:
    """Build a host-less lab that declares exactly ``resources``."""
    return Lab(name="test_lab", resources=set(resources))


class TestReservationGateResultMatrix:
    def test_skip_check_returns_skipped_outcome_with_warning(self, caplog, monkeypatch):
        import logging

        lab = _lab_with_resources()
        install_scoped_context(monkeypatch, lab, [])
        backend = _FakeBackend(owners={})  # would fail the check if it ran
        identity = ResolvedIdentity(username="alice", source="$USER")
        gate = ReservationGate(backend=backend, identity=identity, skip_check=True)

        with caplog.at_level(logging.WARNING, logger="otto"):
            outcome = gate.evaluate()

        assert outcome.checked is False
        assert outcome.skipped is True
        assert outcome.warning is not None
        assert "SKIPPED" in outcome.warning
        assert "alice" in outcome.warning
        assert "test_lab" in outcome.warning
        # No rich markup — that is the CLI adapter's job, not the library's.
        assert "[bold red]" not in outcome.warning
        assert any("skipped" in rec.message.lower() for rec in caplog.records)

    def test_skip_check_warns_even_when_backend_none(self, caplog, monkeypatch):
        import logging

        lab = _lab_with_resources()
        install_scoped_context(monkeypatch, lab, [])
        identity = ResolvedIdentity(username="alice", source="$USER")
        # backend=None models the -R break-glass path: construction skipped.
        gate = ReservationGate(backend=None, identity=identity, skip_check=True)

        with caplog.at_level(logging.WARNING, logger="otto"):
            outcome = gate.evaluate()

        assert outcome.skipped is True
        assert outcome.checked is False
        assert outcome.warning is not None
        assert "SKIPPED" in outcome.warning

    def test_no_backend_is_all_false_none(self):
        gate = ReservationGate(backend=None, identity=None, skip_check=False)
        outcome = gate.evaluate()
        assert outcome == ReservationGateResult(checked=False, skipped=False, warning=None)

    def test_backend_missing_resource_raises(self, monkeypatch):
        lab = _lab_with_resources()
        install_scoped_context(monkeypatch, lab, [])
        backend = _FakeBackend(owners={}, username="alice")  # no one has anything
        identity = ResolvedIdentity(username="alice", source="$USER")
        gate = ReservationGate(backend=backend, identity=identity, skip_check=False)

        with pytest.raises(MissingReservationError):
            gate.evaluate()

    def test_backend_fully_held_returns_checked(self, monkeypatch):
        lab = _lab_declaring("rack1", "test1", "test2")
        backend = _FakeBackend(
            owners={
                "rack1": "alice",
                "test1": "alice",
                "test2": "alice",
            },
            username="alice",
        )
        identity = ResolvedIdentity(username="alice", source="$USER")
        install_scoped_context(monkeypatch, lab, [])
        gate = ReservationGate(backend=backend, identity=identity, skip_check=False)

        outcome = gate.evaluate()

        assert outcome == ReservationGateResult(checked=True, skipped=False, warning=None)

    def test_backend_configured_but_identity_none_raises_runtime_error(self, monkeypatch):
        lab = _lab_with_resources()
        install_scoped_context(monkeypatch, lab, [])
        backend = _FakeBackend(owners={})
        gate = ReservationGate(backend=backend, identity=None, skip_check=False)

        with pytest.raises(RuntimeError, match="identity must be resolved"):
            gate.evaluate()


####################
#  The requirement is computed over the fleet of interest
#  (spec 2026-08-28 three-level-reservations §5)
####################


def _slot_lab():
    """A two-host lab whose hosts each declare a host-level resource."""
    lab = fleet_lab(("slot1", "rig"), ("slot2", "rig"))
    lab.hosts["slot1"].resources = frozenset({"slot-1"})
    lab.hosts["slot2"].resources = frozenset({"slot-2"})
    return lab


def test_gate_requires_only_the_fleet_in_play(tmp_path, monkeypatch):
    """Mutation: make evaluate() pass host_ids=None and this goes red — slot-2 would be demanded."""
    lab = _slot_lab()
    install_scoped_context(monkeypatch, lab, [_repo(tmp_path, "r1", labs=["rig"], hosts=["slot1"])])
    gate = ReservationGate(
        backend=_FakeBackend({"slot-1": "chris"}, username="chris"),
        identity=ResolvedIdentity(username="chris", source="$USER"),
    )

    assert gate.evaluate() == ReservationGateResult(checked=True, skipped=False, warning=None)


def test_gate_demands_every_host_when_no_repo_declares_a_fleet(tmp_path, monkeypatch):
    """The whole-lab fallback is unchanged: no declaration, no narrowing."""
    lab = _slot_lab()
    install_scoped_context(monkeypatch, lab, [_repo(tmp_path, "r1")])
    gate = ReservationGate(
        backend=_FakeBackend({"slot-1": "chris"}, username="chris"),
        identity=ResolvedIdentity(username="chris", source="$USER"),
    )

    with pytest.raises(MissingReservationError, match=r"slot-2\s+host slot2"):
        gate.evaluate()


def test_skip_warning_lists_the_in_play_requirement(tmp_path, monkeypatch):
    """-R must announce the SAME requirement the check would have made, not a wider one."""
    lab = _slot_lab()
    install_scoped_context(monkeypatch, lab, [_repo(tmp_path, "r1", labs=["rig"], hosts=["slot1"])])

    result = ReservationGate(
        skip_check=True, identity=ResolvedIdentity(username="chris", source="$USER")
    ).evaluate()

    assert result.skipped
    assert "['slot-1']" in result.warning
    assert "slot-2" not in result.warning


def _three_level_lab():
    """A lab declaring its own resource over hosts that declare element- and host-level ones."""
    lab = _slot_lab()
    lab.resources = {"rack-1"}
    slot1 = lab.hosts["slot1"]
    slot1.element = dataclasses.replace(slot1.element, resources=frozenset({"chassis-a"}))
    return lab


def _empty_fleet_repo(tmp_path):
    """A repo whose ``[project]`` declaration admits no host in the loaded lab."""
    return _repo(tmp_path, "r1", labs=["rig"], hosts=["nothing-matches"])


def test_empty_declared_fleet_requires_the_lab_level_only_under_skip(tmp_path, monkeypatch):
    """Zero hosts in play is a requirement of the LAB's own set — never an abort here.

    An empty declared fleet means nothing is in play, so the requirement is the
    lab-level rows and only those: no host contributes its element's set or its
    own when no host is selected. The refusal still belongs to the walk that
    follows, which is where it always was.

    Mutation: pass ``require_nonempty=True`` in ``otto.config.fleet.get_hosts_in_play``
    and this goes red with ``ProjectScopeError``.
    """
    lab = _three_level_lab()
    install_scoped_context(monkeypatch, lab, [_empty_fleet_repo(tmp_path)])

    result = ReservationGate(
        skip_check=True, identity=ResolvedIdentity(username="chris", source="$USER")
    ).evaluate()

    assert result.skipped
    assert "Required resources: ['rack-1']" in result.warning
    assert "chassis-a" not in result.warning
    assert "slot-1" not in result.warning
    assert "slot-2" not in result.warning


def test_empty_declared_fleet_checks_the_lab_level_only(tmp_path, monkeypatch):
    """The checked path reaches a verdict too: hold the lab's set and the gate passes."""
    lab = _three_level_lab()
    install_scoped_context(monkeypatch, lab, [_empty_fleet_repo(tmp_path)])
    gate = ReservationGate(
        backend=_FakeBackend({"rack-1": "chris"}, username="chris"),
        identity=ResolvedIdentity(username="chris", source="$USER"),
    )

    assert gate.evaluate() == ReservationGateResult(checked=True, skipped=False, warning=None)


def test_the_gate_ignores_resources_declared_on_the_builtin_local_host(monkeypatch):
    """Reaching the runner never needs a slot (spec 2026-08-28 §5).

    The built-in host is handed a resource it would never carry in production:
    against a gate that still counted it, ``alice`` holds nothing and this
    raises. The lab itself declares none, so the requirement is empty and the
    backend is never asked — which is the whole point of the exemption.
    """
    lab = add_builtin_local(fleet_lab(("h1", "a")), resources={"runner-slot"})
    install_scoped_context(monkeypatch, lab, [])
    gate = ReservationGate(
        backend=_FakeBackend(owners={}, username="alice"),
        identity=ResolvedIdentity(username="alice", source="$USER"),
    )

    assert gate.evaluate() == ReservationGateResult(checked=True, skipped=False, warning=None)


def test_the_gate_still_enforces_a_lab_declared_local_host(monkeypatch):
    """The other direction, so the exemption cannot be read as "ignore the id `local`".

    A lab may define its own ``local`` entry, and ``load_lab`` then injects no
    built-in host at all. That entry is an ordinary host the user wrote down,
    and its slot is enforced.
    """
    lab = fleet_lab(("local", "a"), ("h2", "a"))
    lab.hosts["local"].resources = frozenset({"runner-slot"})
    install_scoped_context(monkeypatch, lab, [])
    gate = ReservationGate(
        backend=_FakeBackend(owners={"runner-slot": "dana"}, username="alice"),
        identity=ResolvedIdentity(username="alice", source="$USER"),
    )

    with pytest.raises(MissingReservationError, match="runner-slot"):
        gate.evaluate()


def test_reservations_import_is_typer_free():
    import subprocess
    import sys

    code = "import sys, otto.reservations; sys.exit(1 if 'typer' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


####################
#  The expiry warning
#  The gate warns about a booking it required and found held but which is
#  about to lapse. It is not the gate's job to refuse — the reservation IS
#  held right now — only to say so before a long run walks into the gap.
####################


def _soon(minutes):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_gate_warns_when_a_required_reservation_is_expiring(caplog, monkeypatch):
    """Held, so no refusal — but ending inside the window, so a warning."""
    import logging

    lab = _lab_declaring("rack1")
    install_scoped_context(monkeypatch, lab, [])
    gate = ReservationGate(
        backend=_FakeBackend(owners={"rack1": "alice"}, username="alice", ends={"rack1": _soon(1)}),
        identity=ResolvedIdentity(username="alice", source="$USER"),
    )

    with caplog.at_level(logging.WARNING, logger="otto"):
        outcome = gate.evaluate()

    assert outcome == ReservationGateResult(checked=True, skipped=False, warning=None)
    assert "rack1" in caplog.text
    assert "expires" in caplog.text.lower()


def test_gate_is_silent_when_the_reservation_runs_well_past_the_window(caplog, monkeypatch):
    """The negative half of the pair: a healthy booking says nothing at all."""
    import logging

    lab = _lab_declaring("rack1")
    install_scoped_context(monkeypatch, lab, [])
    gate = ReservationGate(
        backend=_FakeBackend(
            owners={"rack1": "alice"}, username="alice", ends={"rack1": _soon(90)}
        ),
        identity=ResolvedIdentity(username="alice", source="$USER"),
    )

    with caplog.at_level(logging.WARNING, logger="otto"):
        gate.evaluate()

    assert "expires" not in caplog.text.lower()


def test_skip_check_suppresses_the_warning(caplog, monkeypatch):
    """``-R`` short-circuits evaluate() above the helper.

    The skip path returns before the check, so the expiry line is never
    reached. The resource name still appears — the skip warning lists the
    requirement — so the assertion is on the expiry wording, not the id.
    Mutation: move the helper above the ``skip_check`` branch and this goes
    red.
    """
    import logging

    lab = _lab_declaring("rack1")
    install_scoped_context(monkeypatch, lab, [])
    gate = ReservationGate(
        backend=_FakeBackend(owners={"rack1": "alice"}, username="alice", ends={"rack1": _soon(1)}),
        identity=ResolvedIdentity(username="alice", source="$USER"),
        skip_check=True,
    )

    with caplog.at_level(logging.WARNING, logger="otto"):
        outcome = gate.evaluate()

    assert outcome.skipped is True
    assert "expires" not in caplog.text.lower()


def test_gate_does_not_warn_about_a_resource_outside_the_fleet_requirement(
    caplog, monkeypatch, tmp_path
):
    """Scope is what the gate demanded, not everything the user holds.

    ``slot2`` is outside the declared fleet, so ``slot-2`` was never required
    here — warning about it would be noise on a run that never touches it,
    and it is ``otto.cli.host``'s job to warn if the user names that host.
    """
    import logging

    lab = _slot_lab()
    install_scoped_context(monkeypatch, lab, [_repo(tmp_path, "r1", labs=["rig"], hosts=["slot1"])])
    gate = ReservationGate(
        backend=_FakeBackend(
            {"slot-1": "chris", "slot-2": "chris"},
            username="chris",
            ends={"slot-1": _soon(120), "slot-2": _soon(1)},
        ),
        identity=ResolvedIdentity(username="chris", source="$USER"),
    )

    with caplog.at_level(logging.WARNING, logger="otto"):
        gate.evaluate()

    assert "slot-2" not in caplog.text


class _ExplodingNullBackend(NullReservationBackend):
    """Null-SHAPED, but fatal to query.

    A plain ``NullReservationBackend`` cannot prove the guard: its
    ``fetch_reservations`` returns ``[]``, so deleting ``is_null_backend``
    from a call site leaves every assertion green — the helper is handed an
    empty list and says nothing, exactly as it would have anyway. Subclassing
    keeps ``is_null_backend`` (an ``isinstance`` check) answering True, so the
    guard still suppresses this backend, while removing the guard turns the
    now-reachable fetch into a loud failure instead of a quiet no-op.
    """

    def fetch_reservations(self, username, start=None, end=None):
        raise AssertionError("the null backend was queried; the is_null_backend guard is missing")


def test_the_null_backend_gate_never_reaches_the_expiry_helper(caplog, monkeypatch):
    """A lab with a scheduler configured to ``none`` warns about nothing.

    The guard matters because the helper's argument is a live fetch: reaching
    it past the null short-circuit would query a backend the whole point of
    which is that there is nothing to query.

    Mutation: drop ``not is_null_backend(self.backend)`` from ``evaluate``
    and this raises out of the fetch.
    """
    import logging

    lab = _lab_declaring("rack1")
    install_scoped_context(monkeypatch, lab, [])
    gate = ReservationGate(
        # WITH the username the check is for: without it the fetch this test
        # is trying to prove unreachable would fail on the base class's "no
        # username was resolved" error instead of the AssertionError the
        # exploding double exists to raise, and the mutation would go red for
        # the wrong reason. `is_null_backend` short-circuits before the
        # username is ever read, so the guard stays green either way.
        backend=_ExplodingNullBackend(username="alice"),
        identity=ResolvedIdentity(username="alice", source="$USER"),
    )

    with caplog.at_level(logging.WARNING, logger="otto"):
        assert gate.evaluate().checked is True

    assert "expires" not in caplog.text.lower()


def test_an_empty_requirement_never_reaches_the_expiry_helper(caplog, monkeypatch):
    """Same guard from the other side: a lab needing nothing asks nothing.

    The backend here raises on any fetch, which is the assertion: a run that
    needs no reservation must survive a scheduler outage.
    """
    import logging

    from otto.reservations import ReservationBackendError

    class _ExplodingBackend(ReservationBackendBase):
        def fetch_reservations(self, username, start=None, end=None):
            raise ReservationBackendError("scheduler is down")

        def backend_name(self):
            return "boom"

    lab = Lab(name="test_lab")
    assert not lab.resources  # the premise, stated
    install_scoped_context(monkeypatch, lab, [])
    gate = ReservationGate(
        backend=_ExplodingBackend(username="alice"),
        identity=ResolvedIdentity(username="alice", source="$USER"),
    )

    with caplog.at_level(logging.WARNING, logger="otto"):
        assert gate.evaluate().checked is True

    assert "expires" not in caplog.text.lower()
