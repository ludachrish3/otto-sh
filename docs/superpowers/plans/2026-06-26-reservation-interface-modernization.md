# Reservation Interface Modernization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Modernize otto's reservation interface — multi-holder `who_reserved`, named-registry backend selection (drop dotted-path), a true break-glass `-R` that never constructs a broken backend, and optional cached `--as-user` username completion.

**Architecture:** All changes live in `src/otto/reservations/` plus its CLI wiring in `src/otto/cli/main.py` / `src/otto/cli/reservation.py` and the completion cache in `src/otto/configmodule/`. The reservation backend `Protocol` gains a multi-valued `who_reserved` and an optional `SupportsUsernameCompletion` capability. Backend selection moves from `importlib` dotted-path resolution to a name registry mirroring `register_term_backend`/`register_transfer_backend`. The top-level callback's backend construction is extracted into a testable `build_reservation_state()` helper that skips construction entirely under `-R`.

**Tech Stack:** Python 3.10+, Typer (≥0.26, vendored click fork), Pydantic v2, pytest. Implements the design in `docs/superpowers/specs/2026-06-25-pluggable-host-source-and-conformance-design.md` §3a, §3b (reservation half), §3c, §3d.

## Global Constraints

- **Scope = reservations only.** This is "Plan B" of four. The host-source plan (§3, §3b-lab), conformance suite (§4, §5), and narrative docs (§6) are separate plans. Do NOT touch `src/otto/storage/`, `otto.testing`, or `otto.examples` here.
- **Commit policy (otto-specific, overrides the template's `git commit`):** the repo's `prepare-commit-msg` hook needs `/dev/tty` and mis-attributes agent commits as AI-assist "None". Do **not** self-commit. Each "Commit" step means: `git add` the listed files, then surface the shown commit message for Chris to run. The orchestrator collects messages; Chris commits.
- **Per-task gate:** `make coverage` (pinned-Python suite + coverage threshold). `make coverage` does NOT run the type checker, so also run `make typecheck` (ty) when a task changes types/signatures.
- **Final gate (Task 10):** `make coverage` + `make typecheck` + `make docs`. `make nox` (full matrix, needs the dev VM) is Chris's call — do not run it.
- **Dev VM:** never run heavy/parallel test loads (no oversubscribed `-n`); use scoped `pytest` per step and a single `make coverage` at task end.
- **Narrative-doc deferral:** `docs/guide/reservations.md` prose (who_reserved type, `-R`, `list_usernames`) is reconciled in Plan D. Plan B updates only docstrings + `docs/api/reservations.rst` autodoc stubs. This leaves a known interim inconsistency in the guide prose, accepted per the agreed Plan-B-first sequencing.
- **TDD:** write the failing test first, watch it fail for the right reason, implement minimally, watch it pass.

## File Structure

**Created:**
- `src/otto/reservations/registry.py` — name→class registry + `register_reservation_backend` + `get_reservation_backend_class`; seeds built-in `none`/`json`.
- `tests/unit/reservations/test_registry.py` — registry unit tests.
- `tests/unit/reservations/test_wiring.py` — `build_reservation_state` unit tests.
- `tests/unit/reservations/test_protocol.py` — `SupportsUsernameCompletion` structural tests.
- `tests/unit/configmodule/test_completion_cache_usernames.py` — cache round-trip + collector tests.
- `tests/unit/cli/test_username_completer.py` — `--as-user` completer tests.

**Modified:**
- `src/otto/reservations/protocol.py` — `who_reserved -> list[str]`; add `SupportsUsernameCompletion`.
- `src/otto/reservations/null_backend.py` — `who_reserved` returns `[]`.
- `src/otto/reservations/json_backend.py` — `who_reserved` aggregates all holders.
- `src/otto/reservations/check.py` — holder formatting; `ReservationState.backend_factory`; `gate()` warning ordering.
- `src/otto/reservations/__init__.py` — registry export; `build_backend` registry dispatch; `build_reservation_state`; drop `importlib`.
- `src/otto/cli/main.py` — callback uses `build_reservation_state` (skip build under `-R`); `--as-user` `autocompletion`; `_username_completer`.
- `src/otto/cli/reservation.py` — `whoami`/`check` build backend on demand.
- `src/otto/configmodule/completion_cache.py` — `SCHEMA_VERSION` 5→6; `usernames` key; `collect_reservation_usernames`.
- `src/otto/configmodule/__init__.py` — slow-path wiring of the username collector.
- `tests/unit/reservations/test_json_backend.py`, `test_null_backend.py`, `test_check.py`, `test_build_backend.py`, `tests/unit/cli/test_reservation.py` — updated expectations.
- `docs/api/reservations.rst` — autodoc stubs for new public symbols.

---

### Task 1: Multi-holder `who_reserved` (§3a)

A resource may have several concurrent holders. `who_reserved` returns `list[str]` (empty = unreserved) instead of `str | None`. The JSON backend aggregates all active holders (deduped, file order). The check report renders `(unreserved)` or `held by alice, bob`.

**Files:**
- Modify: `src/otto/reservations/protocol.py:77-102`
- Modify: `src/otto/reservations/null_backend.py:18-21`
- Modify: `src/otto/reservations/json_backend.py:70-77`
- Modify: `src/otto/reservations/check.py:113-123`
- Test: `tests/unit/reservations/test_json_backend.py`, `test_null_backend.py`, `test_check.py`, `tests/unit/cli/test_reservation.py`

**Interfaces:**
- Produces: `ReservationBackend.who_reserved(resource: str) -> list[str]` (empty list = no holders; deterministic order; deduped).
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Update the JSON backend `who_reserved` tests**

In `tests/unit/reservations/test_json_backend.py`, replace the entire `class TestWhoReserved` block with:

```python
class TestWhoReserved:

    def test_resource_held_returns_single_holder_list(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["rack3-psu"]},
                {"user": "bob",   "resources": ["rack4-psu"]},
            ],
        })
        assert backend.who_reserved("rack3-psu") == ["alice"]
        assert backend.who_reserved("rack4-psu") == ["bob"]

    def test_unreserved_returns_empty_list(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [],
        })
        assert backend.who_reserved("rack3-psu") == []

    def test_multiple_holders_aggregated_in_file_order(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["shared-lab"]},
                {"user": "bob",   "resources": ["shared-lab"]},
            ],
        })
        assert backend.who_reserved("shared-lab") == ["alice", "bob"]

    def test_duplicate_holder_deduped(self, tmp_path):
        backend = _make_backend(tmp_path, {
            "version": 1,
            "reservations": [
                {"user": "alice", "resources": ["shared-lab"]},
                {"user": "alice", "resources": ["shared-lab", "other"]},
            ],
        })
        assert backend.who_reserved("shared-lab") == ["alice"]
```

- [ ] **Step 2: Run the JSON backend tests to verify they fail**

Run: `uv run pytest tests/unit/reservations/test_json_backend.py::TestWhoReserved -v`
Expected: FAIL — current `who_reserved` returns `"alice"` (a `str`), not `["alice"]`.

- [ ] **Step 3: Implement multi-holder `who_reserved` in the backends + protocol**

In `src/otto/reservations/json_backend.py`, replace the `who_reserved` method (lines 70-77) with:

```python
    def who_reserved(self,
        resource: str,
    ) -> list[str]:
        holders: list[str] = []
        for entry in self._active_entries():
            if resource in entry.resources and entry.user not in holders:
                holders.append(str(entry.user))
        return holders
```

In `src/otto/reservations/null_backend.py`, replace the `who_reserved` method (lines 18-21) with:

```python
    def who_reserved(self,
        resource: str,  # noqa: ARG002 — protocol compliance
    ) -> list[str]:
        return []
```

In `src/otto/reservations/protocol.py`, replace the `who_reserved` signature and its Returns section (lines 77-102) with:

```python
    def who_reserved(self,
        resource: str,
    ) -> list[str]:
        """Return the usernames currently holding ``resource``.

        Used for error messages when a reservation check fails
        (e.g. ``"shared-lab is held by alice, bob"``) so the caller knows who
        to talk to.

        Parameters
        ----------
        resource : str
            Resource identifier to look up.

        Returns
        -------
        list[str]
            The usernames holding the resource, in a deterministic order with
            duplicates removed.  An **empty list** means no one currently holds
            it (there is no ``None`` sentinel — a resource can have any number
            of concurrent holders).

        Raises
        ------
        otto.reservations.check.ReservationBackendError
            On any failure that prevents a definitive answer.
        """
        ...
```

- [ ] **Step 4: Run the JSON + null backend tests to verify they pass**

Run: `uv run pytest tests/unit/reservations/test_json_backend.py::TestWhoReserved tests/unit/reservations/test_null_backend.py -v`
Expected: `test_no_holder` in `test_null_backend.py` still FAILS (asserts `is None`) — fixed next step; the JSON tests PASS.

- [ ] **Step 5: Update the null-backend test and the check report formatting**

In `tests/unit/reservations/test_null_backend.py`, replace `test_no_holder` (lines 17-19) with:

```python
def test_no_holder():
    backend = NullReservationBackend()
    assert backend.who_reserved("any-resource") == []
```

In `src/otto/reservations/check.py`, replace the holder block (lines 113-123) with:

```python
    holders: dict[str, list[str]] = {r: backend.who_reserved(r) for r in sorted(missing)}
    lines = [
        f"User {username!r} does not hold all resources required by lab "
        f"{lab.name!r}. Missing:"
    ]
    for resource, who in holders.items():
        if not who:
            lines.append(f"  - {resource} (unreserved)")
        else:
            lines.append(f"  - {resource} (held by {', '.join(who)})")
    raise MissingReservationError("\n".join(lines))
```

- [ ] **Step 6: Update the check + CLI test doubles to return lists**

In `tests/unit/reservations/test_check.py`, replace `_FakeBackend.who_reserved` (lines 36-37) with:

```python
    def who_reserved(self, resource: str) -> list[str]:
        u = self.owners.get(resource)
        return [u] if u is not None else []
```

Then add this multi-holder formatting test to the end of `class TestCheckReservations` in the same file:

```python
    def test_lists_multiple_holders_in_message(self):
        class _MultiHolderBackend:
            def __init__(self, holders):
                self._h = holders
            def get_reserved_resources(self, username):
                return {r for r, us in self._h.items() if username in us}
            def who_reserved(self, resource):
                return list(self._h.get(resource, []))
            def backend_name(self):
                return "multi"

        lab = Lab(name="shared_lab", resources={"rack1"})
        backend = _MultiHolderBackend(holders={"rack1": ["alice", "bob"]})
        with pytest.raises(MissingReservationError) as exc_info:
            check_reservations(lab, "carol", backend)
        assert "held by alice, bob" in str(exc_info.value)
```

In `tests/unit/cli/test_reservation.py`, replace `_FakeBackend.who_reserved` (lines 41-42) with:

```python
    def who_reserved(self, resource: str) -> list[str]:
        return ["alice"]
```

and replace the `_EmptyBackend.who_reserved` override (lines 119-120) with:

```python
        def who_reserved(self, resource: str) -> list[str]:
            return []
```

- [ ] **Step 7: Run the full reservation + CLI reservation suite**

Run: `uv run pytest tests/unit/reservations/ tests/unit/cli/test_reservation.py -v`
Expected: PASS (all green).

- [ ] **Step 8: Commit (stage + surface message per Global Constraints)**

```bash
git add src/otto/reservations/protocol.py src/otto/reservations/null_backend.py \
        src/otto/reservations/json_backend.py src/otto/reservations/check.py \
        tests/unit/reservations/test_json_backend.py tests/unit/reservations/test_null_backend.py \
        tests/unit/reservations/test_check.py tests/unit/cli/test_reservation.py
```

Message: `feat(reservations): multi-holder who_reserved (list[str], empty = unreserved)`

---

### Task 2: Reservation backend registry (§3b)

Introduce a name→class registry mirroring `register_term_backend`/`register_transfer_backend`, seeded with built-ins `none`/`json`. This is the foundation for dropping dotted-path resolution in Task 3.

**Files:**
- Create: `src/otto/reservations/registry.py`
- Modify: `src/otto/reservations/__init__.py:43-45` (add export)
- Test: `tests/unit/reservations/test_registry.py`

**Interfaces:**
- Produces: `register_reservation_backend(name: str, cls: type) -> None`; `get_reservation_backend_class(name: str) -> type` (raises `ValueError` listing registered names on miss); module dict `_RESERVATION_BACKENDS: dict[str, type]` seeded with `none`/`json`.

- [ ] **Step 1: Write the failing registry test**

Create `tests/unit/reservations/test_registry.py`:

```python
"""Unit tests for the reservation backend registry."""

import pytest

from otto.reservations import (
    JsonReservationBackend,
    NullReservationBackend,
    register_reservation_backend,
)
from otto.reservations.registry import (
    _RESERVATION_BACKENDS,
    get_reservation_backend_class,
)


def test_builtins_registered():
    assert get_reservation_backend_class("none") is NullReservationBackend
    assert get_reservation_backend_class("json") is JsonReservationBackend


def test_register_and_lookup():
    class MyBackend:
        def get_reserved_resources(self, username):
            return set()

        def who_reserved(self, resource):
            return []

        def backend_name(self):
            return "mine"

    register_reservation_backend("mine-test", MyBackend)
    try:
        assert get_reservation_backend_class("mine-test") is MyBackend
    finally:
        _RESERVATION_BACKENDS.pop("mine-test", None)


def test_unknown_name_lists_registered():
    with pytest.raises(ValueError, match="Unknown reservation backend"):
        get_reservation_backend_class("does-not-exist")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/reservations/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: otto.reservations.registry` / `ImportError: register_reservation_backend`.

- [ ] **Step 3: Create the registry module**

Create `src/otto/reservations/registry.py`:

```python
"""Name → class registry for reservation backends.

Mirrors otto's other extension registries (``register_term_backend`` /
``register_transfer_backend`` / ``register_host_class``): a custom backend
registers a bare name from an ``init`` module, and ``[reservations] backend =
"<name>"`` selects it. Built-ins ``none`` and ``json`` are pre-registered at
import so they resolve through the same path.
"""

from __future__ import annotations

# Name -> ReservationBackend-compatible class. ``build_backend`` constructs the
# resolved class (built-ins keep their bespoke construction; custom backends get
# url= + their ``[reservations.<name>]`` kwargs).
_RESERVATION_BACKENDS: dict[str, type] = {}


def register_reservation_backend(name: str, cls: type) -> None:
    """Make a custom reservation backend selectable as ``backend = "<name>"``.

    Call from an ``init`` module listed in ``.otto/settings.toml``. The class
    must satisfy the :class:`~otto.reservations.protocol.ReservationBackend`
    protocol.
    """
    _RESERVATION_BACKENDS[name] = cls


def get_reservation_backend_class(name: str) -> type:
    """Return the backend class registered under *name*.

    Raises
    ------
    ValueError
        If *name* is not registered; the message lists the registered names.
    """
    try:
        return _RESERVATION_BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_RESERVATION_BACKENDS))
        raise ValueError(
            f"Unknown reservation backend {name!r}. Registered backends: {known}. "
            f"Custom backends can be added via register_reservation_backend()."
        ) from None


def _register_builtins() -> None:
    from .json_backend import JsonReservationBackend
    from .null_backend import NullReservationBackend

    _RESERVATION_BACKENDS.setdefault("none", NullReservationBackend)
    _RESERVATION_BACKENDS.setdefault("json", JsonReservationBackend)


_register_builtins()
```

- [ ] **Step 4: Export `register_reservation_backend` from the package**

In `src/otto/reservations/__init__.py`, after the `from .protocol import (...)` block (ends line 45), add:

```python
from .registry import (
    register_reservation_backend as register_reservation_backend,
)
```

- [ ] **Step 5: Run the registry test to verify it passes**

Run: `uv run pytest tests/unit/reservations/test_registry.py -v`
Expected: PASS.

- [ ] **Step 6: Commit (stage + surface message)**

```bash
git add src/otto/reservations/registry.py src/otto/reservations/__init__.py \
        tests/unit/reservations/test_registry.py
```

Message: `feat(reservations): add named backend registry (register_reservation_backend)`

---

### Task 3: `build_backend` resolves via registry; drop dotted-path (§3b)

Replace `importlib` dotted-path resolution with a registry lookup. `none`/`json` keep their bespoke construction; any other name is resolved from the registry and constructed with `url=` + `[reservations.<name>]` kwargs.

**Files:**
- Modify: `src/otto/reservations/__init__.py:9` (drop `importlib`), `:48-147` (`build_backend` body + docstring)
- Test: `tests/unit/reservations/test_build_backend.py` (replace `TestDottedPath`)

**Interfaces:**
- Consumes: `get_reservation_backend_class` (Task 2).
- Produces: `build_backend(settings, repo_dir)` unchanged signature; unknown name now raises `ValueError("Unknown reservation backend ...")`.

- [ ] **Step 1: Rewrite the factory's third-party tests**

In `tests/unit/reservations/test_build_backend.py`: delete the top-level `import sys` and `import types` lines (lines 4-5), and replace the entire `class TestDottedPath` (lines 80-156) with:

```python
class TestRegisteredBackend:

    def test_registered_name_resolved_with_url_and_kwargs(self, tmp_path):
        from otto.reservations import register_reservation_backend
        from otto.reservations.registry import _RESERVATION_BACKENDS

        class FakeBackend:
            def __init__(self, *, api_key: str = "", url=None):
                self.api_key = api_key
                self.url = url

            def get_reserved_resources(self, username):
                return set()

            def who_reserved(self, resource):
                return []

            def backend_name(self):
                return "fake"

        register_reservation_backend("fake-test", FakeBackend)
        try:
            backend = build_backend(
                {
                    "backend": "fake-test",
                    "url": "https://api.example",
                    "fake-test": {"api_key": "secret"},
                },
                repo_dir=tmp_path,
            )
            assert isinstance(backend, FakeBackend)
            assert backend.api_key == "secret"
            assert backend.url == "https://api.example"
        finally:
            _RESERVATION_BACKENDS.pop("fake-test", None)

    def test_registered_name_without_url(self, tmp_path):
        from otto.reservations import register_reservation_backend
        from otto.reservations.registry import _RESERVATION_BACKENDS

        class FakeBackend:
            def __init__(self, *, api_key: str = ""):
                self.api_key = api_key

            def get_reserved_resources(self, username):
                return set()

            def who_reserved(self, resource):
                return []

            def backend_name(self):
                return "fake"

        register_reservation_backend("fake-test-2", FakeBackend)
        try:
            backend = build_backend(
                {"backend": "fake-test-2", "fake-test-2": {"api_key": "secret"}},
                repo_dir=tmp_path,
            )
            assert isinstance(backend, FakeBackend)
            assert backend.api_key == "secret"
        finally:
            _RESERVATION_BACKENDS.pop("fake-test-2", None)

    def test_unknown_backend_name_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown reservation backend"):
            build_backend({"backend": "mystery"}, tmp_path)
```

- [ ] **Step 2: Run the factory tests to verify the new ones fail**

Run: `uv run pytest tests/unit/reservations/test_build_backend.py::TestRegisteredBackend -v`
Expected: FAIL — `build_backend` still treats `"fake-test"` as a dotted path (no `:` → raises "Unknown reservation backend" for the first two cases because they expect registry resolution, and the kwargs/url assertions never run).

- [ ] **Step 3: Rewrite `build_backend` to use the registry**

In `src/otto/reservations/__init__.py`, delete the `import importlib` line (line 9). Then replace the body from `if backend_name == "none":` through the end of the function (lines 99-147) with:

```python
    if backend_name == "none":
        return NullReservationBackend()

    if backend_name == "json":
        json_settings = settings.get("json", {}) or {}
        path_raw = json_settings.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            raise ValueError(
                "[reservations.json] requires a 'path' string pointing at the "
                "reservation file"
            )
        path = Path(path_raw)
        if not path.is_absolute():
            path = repo_dir / path
        return JsonReservationBackend(url=url, path=path)

    # Custom backend: resolved by registered name (register_reservation_backend
    # from an init module). No dotted-path / importlib resolution.
    from .registry import get_reservation_backend_class

    cls = get_reservation_backend_class(backend_name)
    extra_kwargs: dict[str, Any] = settings.get(backend_name) or {}
    if url is not None:
        return cls(url=url, **extra_kwargs)  # type: ignore[no-any-return]
    return cls(**extra_kwargs)  # type: ignore[no-any-return]
```

Then update the `backend` bullet in the `build_backend` docstring (lines 60-62) to:

```python
        * ``backend`` — ``"json"``, ``"none"``, or a name registered via
          :func:`register_reservation_backend` from an init module. Defaults to
          ``"none"`` when absent.
```

- [ ] **Step 4: Run the full factory suite to verify it passes**

Run: `uv run pytest tests/unit/reservations/test_build_backend.py -v`
Expected: PASS (TestNoneBackend, TestEnvelopeValidation, TestJsonBackend, TestRegisteredBackend).

- [ ] **Step 5: Run the type checker (importlib removal + signature)**

Run: `make typecheck`
Expected: no new errors in `otto/reservations/__init__.py` (e.g. no "unused import importlib").

- [ ] **Step 6: Commit (stage + surface message)**

```bash
git add src/otto/reservations/__init__.py tests/unit/reservations/test_build_backend.py
```

Message: `refactor(reservations)!: select backends by registered name, drop dotted-path resolution`

---

### Task 4: `gate()` warns even when the backend is absent (§3c)

Reorder `gate()` so the loud "check SKIPPED" warning fires whenever `skip_check` is set — before the `backend is None` early return. This keeps `-R` (which yields `backend=None` in Task 5) from skipping silently, while preserving the no-backend/no-context no-op paths.

**Files:**
- Modify: `src/otto/reservations/check.py:126-154` (`gate`)
- Test: `tests/unit/reservations/test_check.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `gate()` emits the SKIPPED warning when `skip_check` is true regardless of `backend`; still a silent no-op when `res is None` or (`backend is None` and not `skip_check`).

- [ ] **Step 1: Write the failing test**

Add to `class TestGate` in `tests/unit/reservations/test_check.py`:

```python
    def test_skip_flag_warns_even_when_backend_none(self, caplog):
        import logging

        lab = _lab_with_resources()
        identity = ResolvedIdentity(username="alice", source="$USER")
        # backend=None models the -R break-glass path (Task 5): construction skipped.
        res = ReservationState(backend=None, identity=identity, skip_check=True)
        ctx = _fake_ctx({"otto_reservation": res})

        with (
            caplog.at_level(logging.WARNING, logger="otto"),
            patch("otto.configmodule.get_lab", return_value=lab),
        ):
            gate(ctx)  # must not raise

        assert any("skipped" in rec.message.lower() for rec in caplog.records)
        assert any("alice" in rec.message for rec in caplog.records)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest "tests/unit/reservations/test_check.py::TestGate::test_skip_flag_warns_even_when_backend_none" -v`
Expected: FAIL — current `gate()` returns at `if res is None or res.backend is None: return` before the skip warning, so no warning is logged.

- [ ] **Step 3: Reorder `gate()`**

In `src/otto/reservations/check.py`, replace the entire `gate` function body (lines 133-154) with:

```python
    res = ctx.meta.get("otto_reservation")
    if res is None:
        return

    from ..configmodule import get_lab

    # A skipped check (-R) must be loud and is independent of whether a backend
    # was constructed — under -R no backend is built (backend is None).
    if res.skip_check:
        lab = get_lab()
        username = res.identity.username if res.identity is not None else "<unknown>"
        needed = required_resources(lab)
        from rich import print as rprint
        rprint(
            f"[bold red]\N{WARNING SIGN}  Reservation check SKIPPED for user "
            f"{username!r} on lab {lab.name!r}. Required resources: {sorted(needed)!r}[/bold red]"
        )
        logger.warning(
            "Reservation check skipped for user %r on lab %r. Required: %r",
            username, lab.name, sorted(needed),
        )
        return

    if res.backend is None:
        return

    lab = get_lab()
    assert res.identity is not None, "identity must be resolved before gate() runs"
    check_reservations(lab, res.identity.username, res.backend)
```

- [ ] **Step 4: Run the gate tests to verify they pass**

Run: `uv run pytest tests/unit/reservations/test_check.py::TestGate tests/unit/test_reservations_meta.py -v`
Expected: PASS (new test + `test_skip_flag_short_circuits`, `test_no_backend_is_noop`, `test_normal_path_calls_check`, `test_failing_check_propagates`, and the two meta no-op tests).

- [ ] **Step 5: Commit (stage + surface message)**

```bash
git add src/otto/reservations/check.py tests/unit/reservations/test_check.py
```

Message: `fix(reservations): gate() warns on skipped check even when no backend is built`

---

### Task 5: `-R` skips backend construction; extract `build_reservation_state` (§3c)

Extract the callback's reservation-state assembly into a testable helper that does **not** construct the backend under `-R` (so a backend that fails or hangs in `__init__` can never block lab access). Attach a `backend_factory` thunk so reservation subcommands can build on demand (Task 6).

**Files:**
- Modify: `src/otto/reservations/check.py:34-38` (`ReservationState` + import)
- Modify: `src/otto/reservations/__init__.py` (add `build_reservation_state`)
- Modify: `src/otto/cli/main.py:301-339` (callback)
- Test: `tests/unit/reservations/test_wiring.py`

**Interfaces:**
- Consumes: `build_backend`, `resolve_username`, `ReservationState`.
- Produces: `build_reservation_state(repos, *, as_user, skip_reservation_check, cwd_fallback) -> ReservationState`; `ReservationState.backend_factory: Callable[[], ReservationBackend] | None`.

- [ ] **Step 1: Write the failing wiring test**

Create `tests/unit/reservations/test_wiring.py`:

```python
"""Unit tests for build_reservation_state — the callback's reservation assembly."""

import types

import pytest

import otto.reservations as r
from otto.reservations import (
    NullReservationBackend,
    ReservationBackendError,
    build_reservation_state,
)


def _repo(reservation_settings, sut_dir):
    return types.SimpleNamespace(
        reservation_settings=reservation_settings, sut_dir=sut_dir
    )


def test_skip_does_not_build_backend(tmp_path, monkeypatch):
    def _spy(settings, repo_dir):
        raise AssertionError("build_backend must not be called under -R")

    monkeypatch.setattr(r, "build_backend", _spy)
    state = build_reservation_state(
        [_repo({"backend": "none"}, tmp_path)],
        as_user=None,
        skip_reservation_check=True,
        cwd_fallback=tmp_path,
    )
    assert state.backend is None
    assert state.skip_check is True
    assert state.backend_factory is not None


def test_no_skip_builds_backend(tmp_path):
    state = build_reservation_state(
        [_repo({"backend": "none"}, tmp_path)],
        as_user=None,
        skip_reservation_check=False,
        cwd_fallback=tmp_path,
    )
    assert isinstance(state.backend, NullReservationBackend)
    assert state.skip_check is False


def test_factory_builds_on_demand(tmp_path):
    state = build_reservation_state(
        [_repo({"backend": "none"}, tmp_path)],
        as_user=None,
        skip_reservation_check=True,
        cwd_fallback=tmp_path,
    )
    assert isinstance(state.backend_factory(), NullReservationBackend)


def test_build_failure_propagates(tmp_path, monkeypatch):
    def _boom(settings, repo_dir):
        raise ReservationBackendError("unreachable")

    monkeypatch.setattr(r, "build_backend", _boom)
    with pytest.raises(ReservationBackendError):
        build_reservation_state(
            [_repo({"backend": "x"}, tmp_path)],
            as_user=None,
            skip_reservation_check=False,
            cwd_fallback=tmp_path,
        )


def test_as_user_sets_identity(tmp_path):
    state = build_reservation_state(
        [_repo({"backend": "none"}, tmp_path)],
        as_user="bob",
        skip_reservation_check=False,
        cwd_fallback=tmp_path,
    )
    assert state.identity.username == "bob"
    assert state.identity.source == "--as-user"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/reservations/test_wiring.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_reservation_state'`.

- [ ] **Step 3: Add `backend_factory` to `ReservationState`**

In `src/otto/reservations/check.py`, in the `if TYPE_CHECKING:` block (lines 26-29) add a `Callable` import so it reads:

```python
if TYPE_CHECKING:
    from collections.abc import Callable

    from ..configmodule.lab import Lab
    from .identity import ResolvedIdentity
    from .protocol import ReservationBackend
```

Then replace the `ReservationState` dataclass (lines 34-38) with:

```python
@dataclass(frozen=True)
class ReservationState:
    backend: "ReservationBackend | None" = None
    identity: "ResolvedIdentity | None" = None
    skip_check: bool = False
    # Builds the backend on demand. Set even under -R (where ``backend`` is
    # None) so reservation subcommands can construct it only when needed.
    backend_factory: "Callable[[], ReservationBackend] | None" = None
```

- [ ] **Step 4: Add `build_reservation_state` to the package**

In `src/otto/reservations/__init__.py`, append at the end of the file:

```python
def build_reservation_state(
    repos: list[Any],
    *,
    as_user: str | None,
    skip_reservation_check: bool,
    cwd_fallback: Path,
) -> ReservationState:
    """Resolve the per-invocation reservation state from the active repos.

    The first repo with a ``[reservations]`` section wins. With
    ``skip_reservation_check`` (the ``-R`` break-glass flag) the backend is
    **not** constructed at all — a scheduler that fails or hangs in its
    constructor can never block lab access. A ``backend_factory`` thunk is
    always attached so ``otto reservation`` subcommands can build it on demand.

    Raises
    ------
    ReservationBackendError
        If construction fails and ``skip_reservation_check`` is False.
    """
    reservation_settings: dict[str, Any] = {}
    reservation_repo_dir: Path = repos[0].sut_dir if repos else cwd_fallback
    for repo in repos:
        if repo.reservation_settings:
            reservation_settings = repo.reservation_settings
            reservation_repo_dir = repo.sut_dir
            break

    def _factory() -> ReservationBackend:
        return build_backend(reservation_settings, reservation_repo_dir)

    backend: ReservationBackend | None = None
    if not skip_reservation_check:
        backend = _factory()  # may raise ReservationBackendError

    identity = resolve_username(as_user)
    return ReservationState(
        backend=backend,
        identity=identity,
        skip_check=skip_reservation_check,
        backend_factory=_factory,
    )
```

- [ ] **Step 5: Run the wiring tests to verify they pass**

Run: `uv run pytest tests/unit/reservations/test_wiring.py -v`
Expected: PASS.

- [ ] **Step 6: Rewire the top-level callback to use the helper**

In `src/otto/cli/main.py`, replace the reservation block (lines 301-339, from the `# Build the reservation backend` comment through the `ctx.meta["otto_reservation"] = ReservationState(...)` assignment) with:

```python
    # Resolve reservation identity + backend (first repo with a [reservations]
    # section wins). With -R the backend is NOT constructed at all, so a broken
    # or hanging scheduler can never block lab access (break-glass).
    from ..reservations import (
        ReservationBackendError,
        build_reservation_state,
    )

    try:
        reservation_state = build_reservation_state(
            repos,
            as_user=as_user,
            skip_reservation_check=skip_reservation_check,
            cwd_fallback=Path.cwd(),
        )
    except ReservationBackendError as e:
        rprint(
            f"[bold red]Reservation backend unavailable:[/bold red] {e}\n"
            f"Pass [bold]--skip-reservation-check[/bold] / [bold]-R[/bold] to proceed without the check."
        )
        raise typer.Exit(1) from e

    identity = reservation_state.identity
    if identity is not None and identity.source == "--as-user":
        rprint(
            f"[bold magenta][reservations] acting as {identity.username!r} "
            f"(--as-user)[/bold magenta]"
        )

    ctx.meta["otto_reservation"] = reservation_state
```

- [ ] **Step 7: Run the reservation suite + type check**

Run: `uv run pytest tests/unit/reservations/ tests/unit/cli/test_reservation.py -v && make typecheck`
Expected: PASS; no new type errors.

- [ ] **Step 8: Commit (stage + surface message)**

```bash
git add src/otto/reservations/check.py src/otto/reservations/__init__.py \
        src/otto/cli/main.py tests/unit/reservations/test_wiring.py
```

Message: `feat(reservations): -R skips backend construction entirely (break-glass)`

---

### Task 6: `whoami`/`check` build the backend on demand (§3c)

Under `-R` the backend is `None` in `ReservationState`, but the explicit `otto reservation whoami`/`check` subcommands still want it — so they build it via `backend_factory` when needed.

**Files:**
- Modify: `src/otto/cli/reservation.py:46-92`
- Test: `tests/unit/cli/test_reservation.py`

**Interfaces:**
- Consumes: `ReservationState.backend_factory` (Task 5).
- Produces: no new public symbols.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/cli/test_reservation.py`:

```python
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest "tests/unit/cli/test_reservation.py::test_whoami_builds_backend_on_demand" "tests/unit/cli/test_reservation.py::test_check_builds_backend_on_demand" -v`
Expected: FAIL — `whoami` reads `res.backend` (None) → prints `<none>` and `check` exits 1 ("not configured").

- [ ] **Step 3: Build on demand in `whoami` and `check`**

In `src/otto/cli/reservation.py`, replace the `whoami` function body (lines 48-62) with:

```python
    """Show the resolved reservation identity and backend."""
    from ..configmodule import get_lab
    res = ctx.meta.get("otto_reservation")
    backend = None
    if res is not None:
        backend = res.backend or (
            res.backend_factory() if res.backend_factory else None
        )
    backend_name = backend.backend_name() if backend else "<none>"
    identity = res.identity if res else None
    if identity is None:
        rprint("[yellow]No identity resolved (did the top-level callback run?)[/yellow]")
        raise typer.Exit(1)

    rprint(
        f"username: [bold]{identity.username}[/bold]\n"
        f"source:   {identity.source}\n"
        f"backend:  {backend_name}\n"
        f"lab:      {get_lab().name}"
    )
```

Then replace the `check` function body (lines 67-91) with:

```python
    """Run the reservation check for the top-level ``--lab`` and report."""
    from ..configmodule import get_lab
    res = ctx.meta.get("otto_reservation")

    backend = None
    if res is not None:
        backend = res.backend or (
            res.backend_factory() if res.backend_factory else None
        )
    if res is None or backend is None or res.identity is None:
        rprint("[red]Reservation backend or identity not configured.[/red]")
        raise typer.Exit(1)

    lab = get_lab()
    username = res.identity.username
    needed = required_resources(lab)

    rprint(
        f"Checking reservations for [bold]{username}[/bold] "
        f"against lab [bold]{lab.name}[/bold]"
    )
    rprint(f"Required resources: {sorted(needed)}")

    try:
        check_reservations(lab, username, backend)
    except MissingReservationError as e:
        rprint(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    rprint("[green]OK — all required resources are reserved.[/green]")
```

- [ ] **Step 4: Run the CLI reservation suite to verify it passes**

Run: `uv run pytest tests/unit/cli/test_reservation.py -v`
Expected: PASS (new on-demand tests + all existing tests).

- [ ] **Step 5: Commit (stage + surface message)**

```bash
git add src/otto/cli/reservation.py tests/unit/cli/test_reservation.py
```

Message: `feat(reservations): reservation subcommands build backend on demand under -R`

---

### Task 7: Optional `SupportsUsernameCompletion` capability (§3d)

Add a separate `@runtime_checkable` Protocol so a backend may optionally expose `list_usernames()` for `--as-user` completion. Backends that can't enumerate users simply omit it.

**Files:**
- Modify: `src/otto/reservations/protocol.py` (add Protocol), `src/otto/reservations/__init__.py:43-45` (export)
- Test: `tests/unit/reservations/test_protocol.py`

**Interfaces:**
- Produces: `SupportsUsernameCompletion` (Protocol) with `list_usernames(self) -> list[str]`; exported from `otto.reservations`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/reservations/test_protocol.py`:

```python
"""Structural tests for the optional username-completion capability."""

from otto.reservations import SupportsUsernameCompletion


def test_class_with_list_usernames_satisfies():
    class B:
        def list_usernames(self):
            return ["alice"]

    assert isinstance(B(), SupportsUsernameCompletion)


def test_class_without_list_usernames_does_not():
    class B:
        pass

    assert not isinstance(B(), SupportsUsernameCompletion)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/reservations/test_protocol.py -v`
Expected: FAIL with `ImportError: cannot import name 'SupportsUsernameCompletion'`.

- [ ] **Step 3: Add the Protocol**

In `src/otto/reservations/protocol.py`, append at the end of the file:

```python
@runtime_checkable
class SupportsUsernameCompletion(Protocol):
    """Optional capability: enumerate usernames for ``--as-user`` completion.

    A backend that can list its users implements ``list_usernames``; otto
    detects it structurally (``isinstance(backend, SupportsUsernameCompletion)``)
    and feeds the values into ``--as-user`` tab-completion (cached, see
    :func:`otto.configmodule.completion_cache.collect_reservation_usernames`).
    Backends that cannot enumerate users simply omit it.
    """

    def list_usernames(self) -> list[str]:
        """Return all usernames the backend knows about, for completion."""
        ...
```

- [ ] **Step 4: Export it from the package**

In `src/otto/reservations/__init__.py`, in the `from .protocol import (...)` block (lines 43-45), add the new name so it reads:

```python
from .protocol import (
    ReservationBackend as ReservationBackend,
)
from .protocol import (
    SupportsUsernameCompletion as SupportsUsernameCompletion,
)
```

- [ ] **Step 5: Run the protocol test to verify it passes**

Run: `uv run pytest tests/unit/reservations/test_protocol.py -v`
Expected: PASS.

- [ ] **Step 6: Commit (stage + surface message)**

```bash
git add src/otto/reservations/protocol.py src/otto/reservations/__init__.py \
        tests/unit/reservations/test_protocol.py
```

Message: `feat(reservations): add optional SupportsUsernameCompletion capability`

---

### Task 8: Cache usernames + best-effort collector + slow-path wiring (§3d)

Bump the completion-cache schema, carry a `usernames` list, add a best-effort `collect_reservation_usernames(repos)` (builds the backend, calls `list_usernames()` when supported, swallows every failure), and wire it into the slow-path cache writer.

**Files:**
- Modify: `src/otto/configmodule/completion_cache.py:88` (`SCHEMA_VERSION`), `:375-395` (`read_cache`), `:398-445` (`write_cache`), add `collect_reservation_usernames`
- Modify: `src/otto/configmodule/__init__.py:66-73` (import), `:103-113` (slow-path call)
- Test: `tests/unit/configmodule/test_completion_cache_usernames.py`

**Interfaces:**
- Consumes: `build_backend`, `SupportsUsernameCompletion` (Task 7).
- Produces: `collect_reservation_usernames(repos) -> list[str]` (sorted; `[]` on any failure); cache payload key `usernames`; `read_cache(...)` result dict gains `usernames`; `write_cache(..., usernames=None)`.

- [ ] **Step 1: Write the failing cache tests**

Create `tests/unit/configmodule/test_completion_cache_usernames.py`:

```python
"""Tests for cached --as-user usernames + the best-effort collector."""

import types

import otto.configmodule.completion_cache as cc


def test_usernames_round_trip(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(cc, "_cache_path", lambda: cache_file)
    monkeypatch.setattr(cc, "compute_fingerprint", lambda repos: "fp")
    repos = [object()]  # truthy; not inspected (fingerprint patched)

    cc.write_cache(repos, [], [], [], usernames=["alice", "bob"])
    result = cc.read_cache(repos)

    assert result is not None
    assert result["usernames"] == ["alice", "bob"]


def test_collect_usernames_from_capable_backend(tmp_path):
    from otto.reservations import register_reservation_backend
    from otto.reservations.registry import _RESERVATION_BACKENDS

    class UCBackend:
        def __init__(self, **kwargs):
            pass

        def get_reserved_resources(self, username):
            return set()

        def who_reserved(self, resource):
            return []

        def backend_name(self):
            return "uc"

        def list_usernames(self):
            return ["bob", "alice"]

    register_reservation_backend("uc-test", UCBackend)
    try:
        repo = types.SimpleNamespace(
            reservation_settings={"backend": "uc-test"}, sut_dir=tmp_path
        )
        assert cc.collect_reservation_usernames([repo]) == ["alice", "bob"]
    finally:
        _RESERVATION_BACKENDS.pop("uc-test", None)


def test_collect_usernames_empty_when_capability_absent(tmp_path):
    repo = types.SimpleNamespace(
        reservation_settings={"backend": "none"}, sut_dir=tmp_path
    )
    assert cc.collect_reservation_usernames([repo]) == []


def test_collect_usernames_empty_when_no_reservation_settings(tmp_path):
    repo = types.SimpleNamespace(reservation_settings={}, sut_dir=tmp_path)
    assert cc.collect_reservation_usernames([repo]) == []


def test_collect_usernames_swallows_build_errors(tmp_path):
    # An unknown backend name makes build_backend raise ValueError; the collector
    # must swallow it and return [] (best-effort, never block the slow path).
    repo = types.SimpleNamespace(
        reservation_settings={"backend": "no-such-backend"}, sut_dir=tmp_path
    )
    assert cc.collect_reservation_usernames([repo]) == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/configmodule/test_completion_cache_usernames.py -v`
Expected: FAIL — `write_cache` has no `usernames` kwarg; `collect_reservation_usernames` does not exist.

- [ ] **Step 3: Bump the schema and carry `usernames` through read/write**

In `src/otto/configmodule/completion_cache.py`:

Change line 88 from `SCHEMA_VERSION = 5` to:

```python
SCHEMA_VERSION = 6
```

In `read_cache`, after `transfer_backends = entry.get('transfer_backends', [])` (line 380) add:

```python
    usernames = entry.get('usernames', [])
```

Extend the `isinstance` guard (lines 381-387) to include `usernames`:

```python
    if (not isinstance(instructions, list)
            or not isinstance(suites, list)
            or not isinstance(hosts, list)
            or not isinstance(docker_hosts, list)
            or not isinstance(term_backends, list)
            or not isinstance(transfer_backends, list)
            or not isinstance(usernames, list)):
        return None
```

Add `usernames` to the returned dict (after the `'transfer_backends'` entry, line 394):

```python
        'usernames': usernames,
```

In `write_cache`, add a parameter to the signature (after `transfer_backends` on line 405):

```python
    usernames: list[str] | None = None,
```

and add to the written entry (after the `'transfer_backends'` line 444):

```python
        'usernames': usernames or [],
```

- [ ] **Step 4: Add the best-effort collector**

In `src/otto/configmodule/completion_cache.py`, after `collect_backend_names` (ends line 539), add:

```python
def collect_reservation_usernames(repos: list['Repo']) -> list[str]:
    """Best-effort usernames for ``--as-user`` completion (cached).

    Builds the selected reservation backend (first repo with a
    ``[reservations]`` section) and, when it implements
    :class:`~otto.reservations.protocol.SupportsUsernameCompletion`, returns
    ``list_usernames()`` sorted. Runs on the slow path; any failure (no backend
    configured, build error, enumeration error, missing capability) yields
    ``[]`` so completion degrades gracefully and never blocks real work.
    """
    from ..reservations import build_backend
    from ..reservations.protocol import SupportsUsernameCompletion

    for repo in repos:
        settings = getattr(repo, 'reservation_settings', None)
        if not settings:
            continue
        try:
            backend = build_backend(settings, repo.sut_dir)
            if isinstance(backend, SupportsUsernameCompletion):
                return sorted(backend.list_usernames())
        except Exception:
            return []
        return []
    return []
```

- [ ] **Step 5: Run the cache tests to verify they pass**

Run: `uv run pytest tests/unit/configmodule/test_completion_cache_usernames.py -v`
Expected: PASS.

- [ ] **Step 6: Wire the collector into the slow-path cache writer**

In `src/otto/configmodule/__init__.py`, add `collect_reservation_usernames` to the import block (lines 66-73) so it reads:

```python
from .completion_cache import (
    collect_backend_names,
    collect_docker_capable_host_ids,
    collect_host_ids,
    collect_reservation_usernames,
    read_cache,
    write_cache,
)
```

(Keep any other names already in that block; only add `collect_reservation_usernames` in alphabetical position.)

Then in the slow-path block (lines 103-113), add the collection call and pass it to `write_cache`:

```python
    _host_ids = collect_host_ids(_repos)
    _docker_host_ids = collect_docker_capable_host_ids(_repos)
    _backends = collect_backend_names()
    _usernames = collect_reservation_usernames(_repos)
    try:
        write_cache(
            _repos, _instructions, _suites, _host_ids, _docker_host_ids,
            term_backends=_backends['term_backends'],
            transfer_backends=_backends['transfer_backends'],
            usernames=_usernames,
        )
    except OSError:
        # Cache writes are best-effort — never block real work on them.
        pass
```

- [ ] **Step 7: Run the configmodule + reservation suites**

Run: `uv run pytest tests/unit/configmodule/ tests/unit/reservations/ -q`
Expected: PASS (existing completion-cache tests still green with `SCHEMA_VERSION = 6`).

- [ ] **Step 8: Commit (stage + surface message)**

```bash
git add src/otto/configmodule/completion_cache.py src/otto/configmodule/__init__.py \
        tests/unit/configmodule/test_completion_cache_usernames.py
```

Message: `feat(reservations): cache --as-user usernames (schema v6) via best-effort collector`

---

### Task 9: `--as-user` tab-completion (§3d)

Give `--as-user` a completion callback that prefers the cached usernames (slow-path populated) and falls back to a live best-effort collection — mirroring `_host_id_completer`.

**Files:**
- Modify: `src/otto/cli/main.py` (define `_username_completer` before `app = typer.Typer`; add `autocompletion=` to the `--as-user` Option at lines 210-218)
- Test: `tests/unit/cli/test_username_completer.py`

**Interfaces:**
- Consumes: `get_completion_names` (cache read, gains `usernames` in Task 8), `collect_reservation_usernames` (Task 8).
- Produces: `_username_completer(ctx, incomplete) -> list[str]`.

- [ ] **Step 1: Write the failing completer test**

Create `tests/unit/cli/test_username_completer.py`:

```python
"""Tests for the --as-user shell-completion callback."""


def test_username_completer_prefers_cache(monkeypatch):
    import otto.configmodule as cm

    monkeypatch.setattr(
        cm, "get_completion_names",
        lambda: {"usernames": ["alice", "alfred", "bob"]},
    )
    from otto.cli.main import _username_completer

    assert _username_completer(None, "al") == ["alfred", "alice"]


def test_username_completer_falls_back_to_live(monkeypatch):
    import otto.configmodule as cm
    import otto.configmodule.completion_cache as cc

    monkeypatch.setattr(cm, "get_completion_names", lambda: None)
    monkeypatch.setattr(cm, "get_repos", lambda: [])
    monkeypatch.setattr(
        cc, "collect_reservation_usernames", lambda repos: ["zoe", "zed"]
    )
    from otto.cli.main import _username_completer

    assert _username_completer(None, "z") == ["zed", "zoe"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/cli/test_username_completer.py -v`
Expected: FAIL with `ImportError: cannot import name '_username_completer' from 'otto.cli.main'`.

- [ ] **Step 3: Define the completer**

In `src/otto/cli/main.py`, immediately before `app = typer.Typer(` (line 100), add:

```python
def _username_completer(ctx: "typer.Context", incomplete: str) -> list[str]:
    """Completion source for ``--as-user``: usernames the reservation backend knows.

    Prefers the completion-cache snapshot (slow-path populated, so no backend is
    built in the completion fast path); falls back to a live best-effort
    collection on a cache miss. Empty when the backend can't enumerate users.
    """
    from ..configmodule import get_completion_names, get_repos
    from ..configmodule.completion_cache import collect_reservation_usernames

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get('usernames'), list):
        names = cached['usernames']
    else:
        names = collect_reservation_usernames(get_repos())
    return sorted(n for n in names if n.startswith(incomplete))
```

- [ ] **Step 4: Attach it to the `--as-user` option**

In `src/otto/cli/main.py`, in the `as_user` option (lines 210-218), add `autocompletion=_username_completer,` so it reads:

```python
    as_user: Annotated[str | None,
        typer.Option('--as-user',
            metavar='USERNAME',
            autocompletion=_username_completer,
            help=(
                "Check reservations as USERNAME instead of the current user. "
                "Use when a teammate has the shared lab booked under their name."
            ),
        ),
    ] = None,
```

- [ ] **Step 5: Run the completer test to verify it passes**

Run: `uv run pytest tests/unit/cli/test_username_completer.py -v`
Expected: PASS.

- [ ] **Step 6: Commit (stage + surface message)**

```bash
git add src/otto/cli/main.py tests/unit/cli/test_username_completer.py
```

Message: `feat(reservations): cached tab-completion for --as-user`

---

### Task 10: API docs stubs + full gate

Document the new public symbols for the nitpicky docs build, then run the full local gate.

**Files:**
- Modify: `docs/api/reservations.rst:92` (add autodoc stubs)

**Interfaces:**
- Consumes: `register_reservation_backend`, `build_reservation_state` (exported from `otto.reservations`), `SupportsUsernameCompletion` (auto-included by the `otto.reservations.protocol` automodule).

- [ ] **Step 1: Add autodoc stubs for the new public API**

In `docs/api/reservations.rst`, after `.. autofunction:: otto.reservations.build_backend` (line 92), add:

```rst

.. autofunction:: otto.reservations.register_reservation_backend

.. autofunction:: otto.reservations.build_reservation_state
```

(`SupportsUsernameCompletion` is picked up automatically by the existing
`.. automodule:: otto.reservations.protocol` directive.)

- [ ] **Step 2: Build the docs (nitpicky, warnings-as-errors)**

Run: `make docs`
Expected: PASS — no unresolved cross-references; `register_reservation_backend`, `build_reservation_state`, and `SupportsUsernameCompletion` all render. If a `Callable`/`ReservationBackend` reference fails to resolve in `ReservationState`, confirm `collections.abc.Callable` is under the `TYPE_CHECKING` block (Task 5 Step 3) — intersphinx resolves `Callable` from the Python inventory.

- [ ] **Step 3: Run the type checker**

Run: `make typecheck`
Expected: PASS — no new `ty` errors across `otto/reservations/`, `otto/cli/`, `otto/configmodule/`.

- [ ] **Step 4: Run the coverage gate**

Run: `make coverage`
Expected: PASS — suite green and coverage threshold met.

- [ ] **Step 5: Commit (stage + surface message)**

```bash
git add docs/api/reservations.rst
```

Message: `docs(reservations): document register_reservation_backend + build_reservation_state`

- [ ] **Step 6: Hand off the full gate**

`make nox` (full Python matrix, needs the dev VM with Vagrant hosts) is Chris's call — do not run it. Report that `make coverage`, `make typecheck`, and `make docs` are green and surface all staged commit messages for Chris to commit.

---

## Self-Review

**1. Spec coverage** (against `docs/superpowers/specs/2026-06-25-...-design.md`):
- §3a multi-holder `who_reserved` → Task 1. ✓
- §3b named-registry migration (`register_reservation_backend`, drop dotted-path) → Tasks 2-3. ✓
- §3c break-glass: gate warning ordering → Task 4; `-R` skips construction → Task 5; subcommands build on demand → Task 6. ✓
- §3d optional `SupportsUsernameCompletion` → Task 7; cache schema + collector + slow-path wiring → Task 8; `--as-user` completion → Task 9. ✓
- New public API documented → Task 10. ✓
- Out of scope for Plan B (handoff): conformance suite running against these backends (Plan C); `docs/guide/reservations.md` narrative (Plan D); host-source work (Plan A).

**2. Placeholder scan:** No "TBD"/"handle errors"/"similar to" — every code step shows full code; every test step shows full test bodies. ✓

**3. Type consistency:** `who_reserved -> list[str]` is used identically in protocol/null/json/check/test doubles. `backend_factory` is defined in Task 5 and consumed in Task 6. `collect_reservation_usernames` signature `(repos) -> list[str]` is defined in Task 8 and consumed in Tasks 8/9. `build_reservation_state(repos, *, as_user, skip_reservation_check, cwd_fallback)` is defined in Task 5 and called identically in `cli/main.py`. `usernames` cache key name is identical in `write_cache`, `read_cache`, the collector, and the completer. ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-26-reservation-interface-modernization.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
