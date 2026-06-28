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
    ReservationState,
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


class _FakeBackend:
    def backend_name(self) -> str:
        return "fake"

    def get_reserved_resources(self, username: str) -> set[str]:
        return {"r1"}

    def who_reserved(self, resource: str) -> list[str]:
        return ["alice"]


# ── whoami ─────────────────────────────────────────────────────────────────────


def test_whoami_exits_1_when_no_identity(capsys):
    res = ReservationState(backend=None, identity=None, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})
    with pytest.raises(_Exit) as exc:
        whoami(ctx)
    assert exc.value.exit_code == 1


def test_whoami_exits_1_when_no_reservation_key(capsys):
    """Without the top-level callback, ctx.meta has no key — whoami exits 1 via identity=None path."""
    ctx = _make_ctx({})
    # res = ctx.meta.get("otto_reservation") returns None → identity is None → Exit(1)
    with pytest.raises(_Exit) as exc:
        whoami(ctx)
    assert exc.value.exit_code == 1


def test_whoami_prints_identity_when_configured(capsys):
    from unittest.mock import patch

    from otto.configmodule.lab import Lab

    identity = ResolvedIdentity(username="alice", source="$USER")
    backend = _FakeBackend()
    res = ReservationState(backend=backend, identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab")
    with patch("otto.configmodule.get_lab", return_value=lab):
        whoami(ctx)  # must not raise

    captured = capsys.readouterr()
    assert "alice" in captured.out
    assert "fake" in captured.out
    assert "test_lab" in captured.out


# ── check ──────────────────────────────────────────────────────────────────────


def test_check_exits_1_when_not_configured(capsys):
    ctx = _make_ctx(
        {"otto_reservation": ReservationState(backend=None, identity=None, skip_check=False)}
    )
    with pytest.raises(_Exit) as exc:
        check(ctx)
    assert exc.value.exit_code == 1


def test_check_passes_when_fully_reserved(capsys):
    from unittest.mock import patch

    from otto.configmodule.lab import Lab

    identity = ResolvedIdentity(username="alice", source="$USER")
    backend = _FakeBackend()
    res = ReservationState(backend=backend, identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab", resources={"r1"})
    with patch("otto.configmodule.get_lab", return_value=lab):
        check(ctx)  # must not raise

    assert "OK" in capsys.readouterr().out


def test_check_exits_1_on_missing_reservation(capsys):
    from unittest.mock import patch

    from otto.configmodule.lab import Lab

    class _EmptyBackend(_FakeBackend):
        def get_reserved_resources(self, username: str) -> set[str]:
            return set()

        def who_reserved(self, resource: str) -> list[str]:
            return []

    identity = ResolvedIdentity(username="alice", source="$USER")
    res = ReservationState(backend=_EmptyBackend(), identity=identity, skip_check=False)
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab", resources={"r1"})
    with (
        patch("otto.configmodule.get_lab", return_value=lab),
        pytest.raises(_Exit) as exc,
    ):
        check(ctx)
    assert exc.value.exit_code == 1


def test_whoami_builds_backend_on_demand(capsys):
    from unittest.mock import patch

    from otto.configmodule.lab import Lab

    identity = ResolvedIdentity(username="alice", source="--as-user")
    # -R shape: backend not built, but a factory is available.
    res = ReservationState(
        backend=None,
        identity=identity,
        skip_check=True,
        backend_factory=lambda: _FakeBackend(),
    )
    ctx = _make_ctx({"otto_reservation": res})

    with patch("otto.configmodule.get_lab", return_value=Lab(name="test_lab")):
        whoami(ctx)

    out = capsys.readouterr().out
    assert "alice" in out
    assert "fake" in out  # backend_name() from the factory-built backend


def test_check_builds_backend_on_demand(capsys):
    from unittest.mock import patch

    from otto.configmodule.lab import Lab

    identity = ResolvedIdentity(username="alice", source="--as-user")
    res = ReservationState(
        backend=None,
        identity=identity,
        skip_check=True,
        backend_factory=lambda: _FakeBackend(),
    )
    ctx = _make_ctx({"otto_reservation": res})

    lab = Lab(name="test_lab", resources={"r1"})
    with patch("otto.configmodule.get_lab", return_value=lab):
        check(ctx)  # _FakeBackend reserves {"r1"} for everyone → passes

    assert "OK" in capsys.readouterr().out
