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
from otto.reservations import (
    ReservationGate,
    ResolvedIdentity,
)

# Catch what the production code raises: typer.Exit. Under typer >= 0.26 this is
# typer's own vendored exception (typer._click.exceptions.Exit), which is NOT the
# real click.exceptions.Exit — so catch the typer alias, not click's class.
_Exit = typer.Exit


def _make_ctx(meta: dict) -> typer.Context:
    """Build a typer.Context (backed by click.Context) with the given meta."""
    cmd = click.Command("reservation")
    ctx = click.Context(cmd)
    ctx.meta.update(meta)
    # typer.Context is a subclass of click.Context, so cast is valid here
    return ctx  # type: ignore[return-value]


def _root_options(labs: list[str] | None):
    """A RootOptions with only the fields the reservation commands read set meaningfully."""
    from pathlib import Path

    from otto.cli.invoke import RootOptions

    return RootOptions(
        labs=labs,
        xdir=Path(),
        log_days=30,
        log_level="INFO",
        rich_log_file=False,
        show_time=False,
        dry_run=False,
        as_user=None,
        skip_reservation_check=False,
    )


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
    with pytest.raises(_Exit) as exc:
        whoami(ctx)
    assert exc.value.exit_code == 1


def test_whoami_exits_1_when_no_reservation_key(capsys):
    """Without the top-level callback, ctx.meta has no key — whoami exits 1 via identity=None path."""  # noqa: E501 — descriptive docstring
    ctx = _make_ctx({})
    # res = ctx.meta.get("otto_reservation") returns None → identity is None → Exit(1)
    with pytest.raises(_Exit) as exc:
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

    assert "tech1, overlay" in capsys.readouterr().out


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


def test_check_exits_1_when_not_configured(capsys):
    ctx = _make_ctx(
        {"otto_reservation": ReservationGate(backend=None, identity=None, skip_check=False)}
    )
    with pytest.raises(_Exit) as exc:
        check(ctx)
    assert exc.value.exit_code == 1


def test_check_passes_when_fully_reserved(capsys):
    from unittest.mock import patch

    from otto.config.lab import Lab

    identity = ResolvedIdentity(username="alice", source="$USER")
    backend = _FakeBackend()
    res = ReservationGate(backend=backend, identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab", resources={"r1"})
    with patch("otto.config.get_lab", return_value=lab):
        check(ctx)  # must not raise

    assert "OK" in capsys.readouterr().out


def test_check_exits_1_on_missing_reservation(capsys):
    from unittest.mock import patch

    from otto.config.lab import Lab

    class _EmptyBackend(_FakeBackend):
        def get_reserved_resources(self, username: str) -> set[str]:
            return set()

        def who_reserved(self, resource: str) -> list[str]:
            return []

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationGate(backend=_EmptyBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab", resources={"r1"})
    with (
        patch("otto.config.get_lab", return_value=lab),
        pytest.raises(_Exit) as exc,
    ):
        check(ctx)
    assert exc.value.exit_code == 1


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


def test_check_loads_lab_lazily_when_preamble_skipped(capsys):
    """The lab_free group means check must pull the lab in itself."""
    from unittest.mock import patch

    from otto.config.lab import Lab

    identity = ResolvedIdentity(username="alice", source="$USER")
    state = ReservationGate(backend=_FakeBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({})

    def _fake_ensure(c):
        c.meta["otto_reservation"] = state

    lab = Lab(name="test_lab", resources={"r1"})
    with (
        patch("otto.cli.invoke.ensure_lab_context", side_effect=_fake_ensure) as ensure,
        patch("otto.config.get_lab", return_value=lab),
    ):
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
        pytest.raises(_Exit) as exc,
    ):
        check(ctx)
    assert exc.value.exit_code == 2


def test_check_builds_backend_on_demand(capsys):
    from unittest.mock import patch

    from otto.config.lab import Lab

    identity = ResolvedIdentity(username="alice", source="--as-user")
    res = ReservationGate(
        backend=None,
        identity=identity,
        skip_check=True,
        backend_factory=_FakeBackend,
    )
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab", resources={"r1"})
    with patch("otto.config.get_lab", return_value=lab):
        check(ctx)  # _FakeBackend reserves {"r1"} for everyone → passes

    assert "OK" in capsys.readouterr().out
