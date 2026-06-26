# Backend Conformance Suite + Sample Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a reusable conformance suite — built on otto's `ExpectCollector` — that proves a backend complies with otto's two pluggable interfaces (host source `LabRepository` and `ReservationBackend`), exposed as one ergonomic `assert_*_conforms` helper per interface on a new public `otto.testing` module; plus infra-free sample reference backends as importable `otto.examples.*` modules, conformance-verified against the helpers in CI.

**Architecture:** Each helper constructs a single `ExpectCollector`, runs structural/type rules unconditionally and behavioral round-trip rules when the caller supplies ground truth, then calls `raise_if_failures()` so a backend author sees *every* violation at once. The samples are small, in-memory, dependency-free implementations that satisfy the full contract (the lab sample builds real hosts via `create_host_from_dict`). otto's own unit suite runs both helpers against both the built-ins (`json` host source; `none`/`json` reservations) and the samples.

**Tech Stack:** Python 3.10+, `otto.suite.expect.ExpectCollector` (stdlib-only engine), Pydantic v2 host specs, pytest, Sphinx (nitpicky `-W`, `make doctest` + `make doctest-src`), `ty` type checker.

**Scope note:** This is **Plan C** of the four-phase "pluggable host source + backend conformance" design (`docs/superpowers/specs/2026-06-25-pluggable-host-source-and-conformance-design.md`). It implements **§4 (conformance suite)**, **§5 (sample backends)**, and **§4.4 (`ExpectCollector` tests)**, plus the §7 requirement that conformance runs against built-ins **and** samples. It builds on **Plan A** (pluggable host source — `LabRepository` reshape, `LabNotFoundError`/`LabRepositoryError`, `register_lab_repository`, `build_lab_repository`, all committed) and **Plan B** (reservation modernization — multi-holder `who_reserved`, `register_reservation_backend`, `SupportsUsernameCompletion`, committed). **Out of scope (Plan D):** the host-database guide, the reservations-guide upgrade, the team-setup onboarding hub, and weaving the samples into executable `{doctest}` blocks in the *guides* (`docs/guide/*`). This plan ships the samples with self-contained docstring doctests (covered by `make doctest-src`) and minimal API-reference stubs only — not the narrative guides.

## Global Constraints

- **STAGE-ONLY — never `git commit`.** otto-sh's `prepare-commit-msg` hook needs `/dev/tty` and mis-attributes agent commits; Chris commits. Each task's final step is `git add <listed files>` (NOT commit). The controller captures per-task tree snapshots for diff isolation.
- **BED-FREE ONLY — never touch test-bed / lab resources.** Another agent may be using the lab. Everything here is unit tests, in-memory samples, and conformance helpers. Do **not** run `make coverage` (it hits lab beds), `make nox`, `make coverage-unix`, `make coverage-embedded`, or any Vagrant/QEMU/SSH target. The gate is bed-free: `make coverage-unit`, `make typecheck`, `make docs` (which runs `docs-lint`, `docs-html`, `doctest`, `doctest-src`).
- **Use the reshaped/committed surfaces verbatim:**
  - `LabRepository` protocol: `load_lab(self, name, preferences=None) -> Lab`; `list_labs(self) -> list[str]` (no `search_paths`, no `supports_location`).
  - `from otto.storage import LabRepository, LabNotFoundError, LabRepositoryError, create_host_from_dict, register_lab_repository, JsonFileLabRepository`.
  - `from otto.reservations import ReservationBackend, SupportsUsernameCompletion, NullReservationBackend, JsonReservationBackend, register_reservation_backend`; `from otto.reservations.check import ReservationBackendError`.
  - `who_reserved(resource) -> list[str]` (empty = no holders, never `None`); `get_reserved_resources(username) -> set[str]`.
  - `from otto.configmodule.lab import Lab`; `from otto.host.remote_host import RemoteHost`.
- **`ExpectCollector` is used as-is** (`from otto.suite.expect import ExpectCollector`) — it is already standalone/stdlib-only. Do NOT modify it. API: `expect(condition, msg=None)`, `.failures: list[str]`, `reset()`, `raise_if_failures()`.
- **Never add `from __future__ import annotations`** (this plan and all future code). It stringifies every annotation, and otto's Sphinx **nitpicky** (`-W`, zero ignores) docs gate then emits spurious unresolved cross-reference warnings that fail `make docs`. Use real Python 3.10+ annotations — `X | None`, `list[str]`, `dict[...]` all evaluate at runtime. Import the types you annotate at **module top** so autodoc resolves them to their documented targets; use a quoted forward ref (`"Name"`) only where a genuine import cycle forbids a real import.
- **The conformance helpers import every type they annotate (Lab, RemoteHost, LabRepository, ReservationBackend, SupportsUsernameCompletion, LabNotFoundError) at module top.** `otto.testing` is a leaf module (nothing in otto imports it), so these create no cycle. This deliberately drops the earlier "function-level imports to keep `import otto.testing` cheap" idea — that required forward refs/`TYPE_CHECKING`, which is exactly what trips nitpicky. Module-top real imports → clean autodoc.
- **Minimal valid host dict** for `create_host_from_dict` / the lab sample: `{"ip": <str>, "element": <str>, "creds": {<str>: <str>}}` (`ip`+`element` required on `HostSpec`; `creds` required on `UnixHostSpec`; `resources`/`labs` optional). `make_host_id(element, None, None, None)` returns `element.lower()`.
- **Pytest entry-point for `otto.testing` (spec §4.4 optional): NOT built (YAGNI).** The helpers are importable; an entry-point adds packaging surface for no required benefit. Noted, deferred.
- **New test dirs carry no `__init__.py`** (match sibling `tests/unit/*` dirs).
- DRY, YAGNI, TDD, focused files. Match surrounding style.

---

## File Structure

**New source**
- `src/otto/testing/__init__.py` — public surface; re-exports the two helpers.
- `src/otto/testing/conformance.py` — `assert_lab_repository_conforms`, `assert_reservation_backend_conforms`.
- `src/otto/examples/__init__.py` — package marker + short docstring.
- `src/otto/examples/lab_repository.py` — `ExampleLabRepository` (in-memory `LabRepository`).
- `src/otto/examples/reservations.py` — `ExampleReservationBackend` (in-memory `ReservationBackend` + `SupportsUsernameCompletion`).

**New tests**
- `tests/unit/suite/test_expect.py` — `ExpectCollector` unit tests (§4.4).
- `tests/unit/testing/test_conformance.py` — helpers vs the built-ins + the error-contract failing sample.
- `tests/unit/examples/test_lab_repository.py` — sample lab repo behavior + conformance.
- `tests/unit/examples/test_reservations.py` — sample reservation backend behavior + conformance.

**New docs (minimal stubs only — guides are Plan D)**
- `docs/api/testing.rst`, `docs/api/examples.rst`; add both to `docs/api/index.rst`.

---

## Task 1: `ExpectCollector` unit tests

The conformance helpers stand on `ExpectCollector`, which today has **no** dedicated tests and shows 0% coverage in the unit gate. Cover it first (§4.4). The engine is unchanged — this task only adds tests.

**Files:**
- Test: `tests/unit/suite/test_expect.py` *(new)*

**Interfaces:**
- Consumes: `otto.suite.expect.ExpectCollector`, `otto.suite.expect.expect`.

- [ ] **Step 1: Write the tests**

Create `tests/unit/suite/test_expect.py`:

```python
"""Unit tests for the standalone ExpectCollector (the conformance engine)."""

import logging

import pytest

from otto.suite.expect import ExpectCollector, expect


class TestExpectCollector:

    def test_passing_expect_records_nothing(self):
        c = ExpectCollector()
        c.expect(1 == 1)
        c.expect(True, "should not record")
        assert c.failures == []

    def test_failing_expect_records_report(self):
        c = ExpectCollector()
        x = 42
        c.expect(x == 99, "math is broken")
        assert len(c.failures) == 1
        report = c.failures[0]
        assert "math is broken" in report
        assert "x = 42" in report  # locals captured

    def test_multiple_failures_accumulate_in_order(self):
        c = ExpectCollector()
        c.expect(False, "first")
        c.expect(False, "second")
        assert len(c.failures) == 2
        assert "first" in c.failures[0]
        assert "second" in c.failures[1]

    def test_reset_clears_failures(self):
        c = ExpectCollector()
        c.expect(False, "boom")
        assert c.failures
        c.reset()
        assert c.failures == []

    def test_raise_if_failures_raises_with_aggregate_report(self):
        c = ExpectCollector()
        c.expect(False, "alpha")
        c.expect(False, "beta")
        with pytest.raises(AssertionError) as exc:
            c.raise_if_failures()
        msg = str(exc.value)
        assert "2 expectation(s) failed" in msg
        assert "alpha" in msg
        assert "beta" in msg

    def test_raise_if_failures_no_raise_when_clean(self):
        c = ExpectCollector()
        c.expect(True)
        c.raise_if_failures()  # must not raise

    def test_logger_warns_on_failure(self, caplog):
        logger = logging.getLogger("otto.test.expect")
        c = ExpectCollector(logger=logger)
        with caplog.at_level(logging.WARNING, logger="otto.test.expect"):
            c.expect(False, "logged failure")
        assert any("logged failure" in r.message for r in caplog.records)

    def test_module_level_expect_uses_explicit_collector(self):
        c = ExpectCollector()
        expect(2 + 2 == 5, "via module fn", collector=c)
        assert len(c.failures) == 1
        assert "via module fn" in c.failures[0]
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/python -m pytest tests/unit/suite/test_expect.py -v`
Expected: PASS (8 passed). The engine already exists, so these pass immediately and lock its behavior + raise coverage on `expect.py`.

- [ ] **Step 3: Stage (do NOT commit)**

```bash
git add tests/unit/suite/test_expect.py
```

---

## Task 2: `otto.testing` conformance helpers + built-in conformance

Build the two `assert_*_conforms` helpers and prove them against otto's built-ins (the `json` host source; `none`/`json` reservation backends), plus the error-contract failing-sample test.

**Files:**
- Create: `src/otto/testing/__init__.py`, `src/otto/testing/conformance.py`
- Test: `tests/unit/testing/test_conformance.py` *(new)*

**Interfaces:**
- Consumes: `ExpectCollector`; `otto.storage` (`LabRepository`, `LabNotFoundError`, `JsonFileLabRepository`); `otto.reservations` (`ReservationBackend`, `SupportsUsernameCompletion`, `NullReservationBackend`, `JsonReservationBackend`); `otto.reservations.check.ReservationBackendError`; `otto.configmodule.lab.Lab`; `otto.host.remote_host.RemoteHost`.
- Produces:
  - `otto.testing.assert_lab_repository_conforms(repo, *, expected_labs: list[str] | None = None) -> None`
  - `otto.testing.assert_reservation_backend_conforms(backend, *, known_user: str | None = None, known_resources: list[str] | None = None) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/testing/test_conformance.py`:

```python
"""Conformance helpers verified against otto's built-in backends + an error sample."""

import json
from pathlib import Path

import pytest

from otto.reservations import (
    JsonReservationBackend,
    NullReservationBackend,
)
from otto.reservations.check import ReservationBackendError
from otto.storage import JsonFileLabRepository
from otto.testing import (
    assert_lab_repository_conforms,
    assert_reservation_backend_conforms,
)


def _hosts_file(path: Path) -> None:
    (path / "hosts.json").write_text(json.dumps([
        {"ip": "10.0.0.1", "element": "a", "creds": {"u": "p"},
         "resources": ["a"], "labs": ["alpha"]},
        {"ip": "10.0.0.2", "element": "b", "creds": {"u": "p"},
         "resources": ["b"], "labs": ["beta"]},
    ]))


def _reservations_file(path: Path) -> Path:
    f = path / "reservations.json"
    f.write_text(json.dumps({
        "version": 1,
        "reservations": [
            {"user": "alice", "resources": ["lab-a", "shared"]},
            {"user": "bob", "resources": ["lab-b", "shared"]},
        ],
    }))
    return f


class TestLabRepositoryConformance:

    def test_json_builtin_conforms(self, tmp_path):
        _hosts_file(tmp_path)
        repo = JsonFileLabRepository([tmp_path])
        # Must not raise.
        assert_lab_repository_conforms(repo, expected_labs=["alpha", "beta"])

    def test_non_conforming_repo_raises_with_aggregate(self):
        class Broken:
            def load_lab(self, name, preferences=None):
                return "not a lab"  # wrong type

            def list_labs(self):
                return "not a list"  # wrong type

        with pytest.raises(AssertionError) as exc:
            assert_lab_repository_conforms(Broken())
        assert "LabRepository" in str(exc.value)


class TestReservationBackendConformance:

    def test_null_builtin_conforms(self):
        assert_reservation_backend_conforms(NullReservationBackend())

    def test_json_builtin_conforms_with_round_trip(self, tmp_path):
        f = _reservations_file(tmp_path)
        backend = JsonReservationBackend(path=f)
        assert_reservation_backend_conforms(
            backend, known_user="alice", known_resources=["lab-a", "shared"]
        )

    def test_non_conforming_backend_raises(self):
        class Broken:
            def get_reserved_resources(self, username):
                return ["not", "a", "set"]  # wrong type

            def who_reserved(self, resource):
                return None  # wrong type — must be list

            def backend_name(self):
                return ""  # empty — invalid

        with pytest.raises(AssertionError) as exc:
            assert_reservation_backend_conforms(Broken())
        assert "ReservationBackend" in str(exc.value)


class TestReservationErrorContract:
    """The error-contract rule (§4.3) is exercised by a purpose-built failing
    sample, not the generic helper (which cannot force a healthy backend to fail).
    """

    def test_failure_modes_raise_reservation_backend_error(self):
        class FailingBackend:
            def get_reserved_resources(self, username):
                raise ReservationBackendError("scheduler unreachable")

            def who_reserved(self, resource):
                raise ReservationBackendError("scheduler unreachable")

            def backend_name(self):
                return "failing"

        backend = FailingBackend()
        with pytest.raises(ReservationBackendError):
            backend.get_reserved_resources("anyone")
        with pytest.raises(ReservationBackendError):
            backend.who_reserved("anything")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/testing/test_conformance.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'otto.testing'`.

- [ ] **Step 3: Write the conformance helpers**

Create `src/otto/testing/conformance.py`:

```python
"""Reusable conformance suites for otto's pluggable backend interfaces.

Two helpers — one per interface — assert that a backend satisfies otto's
contract. Each runs every rule as a non-fatal ``expect()`` on a single
:class:`~otto.suite.expect.ExpectCollector`, then raises once with *all*
violations, so a backend author sees every problem at once instead of fixing
them one failed assertion at a time.

Structural/type rules always run. Behavioral round-trip rules run only when the
caller supplies known ground truth (so a SUT author can leverage their own
fixtures).

Usage::

    from otto.testing import (
        assert_lab_repository_conforms,
        assert_reservation_backend_conforms,
    )

    def test_my_backend_conforms():
        assert_reservation_backend_conforms(
            MyBackend(), known_user="alice", known_resources=["lab-a"]
        )
"""

from ..configmodule.lab import Lab
from ..host.remote_host import RemoteHost
from ..reservations import ReservationBackend, SupportsUsernameCompletion
from ..storage import LabNotFoundError, LabRepository
from ..suite.expect import ExpectCollector

# Sentinels for "this name definitely does not exist" probes.
_NO_SUCH_LAB = "__otto_conformance_no_such_lab__"
_PROBE_USER = "__otto_conformance_probe_user__"
_PROBE_RESOURCE = "__otto_conformance_probe_resource__"


def assert_lab_repository_conforms(
    repo: LabRepository,
    *,
    expected_labs: list[str] | None = None,
) -> None:
    """Assert *repo* satisfies the :class:`~otto.storage.protocol.LabRepository` contract.

    Runs structural rules unconditionally; for every listed lab, asserts it
    loads to a valid :class:`~otto.configmodule.lab.Lab`; asserts an unknown
    name raises :class:`~otto.storage.LabNotFoundError`. When *expected_labs*
    is given, also asserts each appears in ``list_labs()`` and loads. Raises a
    single :class:`AssertionError` aggregating every violated rule.

    Parameters
    ----------
    repo : LabRepository
        The backend instance under test.
    expected_labs : list[str] | None
        Optional lab names the caller knows the backend should provide.
    """
    c = ExpectCollector()

    c.expect(
        isinstance(repo, LabRepository),
        "LabRepository: must satisfy the runtime_checkable LabRepository protocol",
    )
    c.expect(
        callable(getattr(repo, "load_lab", None)),
        "LabRepository: load_lab must be callable",
    )
    c.expect(
        callable(getattr(repo, "list_labs", None)),
        "LabRepository: list_labs must be callable",
    )

    names = repo.list_labs()
    names_ok = isinstance(names, list)
    c.expect(
        names_ok,
        f"LabRepository: list_labs() must return a list, got {type(names).__name__}",
    )
    if names_ok:
        for n in names:
            c.expect(
                isinstance(n, str),
                f"LabRepository: list_labs() entries must be str, got "
                f"{type(n).__name__} ({n!r})",
            )

        for n in names:
            if not isinstance(n, str):
                continue
            try:
                lab = repo.load_lab(n)
            except Exception as e:  # noqa: BLE001 — record, never abort the suite
                c.expect(False, f"LabRepository: load_lab({n!r}) raised "
                                f"{type(e).__name__}: {e}")
                continue
            is_lab = isinstance(lab, Lab)
            c.expect(is_lab, f"LabRepository: load_lab({n!r}) must return a Lab, "
                             f"got {type(lab).__name__}")
            if is_lab:
                for host_id, host in lab.hosts.items():
                    c.expect(
                        isinstance(host, RemoteHost),
                        f"LabRepository: lab {n!r} host {host_id!r} must be a "
                        f"RemoteHost, got {type(host).__name__}",
                    )
                    c.expect(
                        host_id == getattr(host, "id", None),
                        f"LabRepository: lab {n!r} host key {host_id!r} must equal "
                        f"host.id {getattr(host, 'id', None)!r}",
                    )
                lab2 = repo.load_lab(n)
                c.expect(
                    sorted(lab.hosts) == sorted(lab2.hosts)
                    and lab.resources == lab2.resources,
                    f"LabRepository: load_lab({n!r}) must be idempotent "
                    f"(two calls must yield equivalent labs)",
                )

    # Unknown lab must raise LabNotFoundError (not return None / bare KeyError).
    try:
        repo.load_lab(_NO_SUCH_LAB)
        c.expect(
            False,
            f"LabRepository: load_lab({_NO_SUCH_LAB!r}) must raise LabNotFoundError "
            f"for an unknown lab, but it returned normally",
        )
    except LabNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        c.expect(
            False,
            f"LabRepository: an unknown lab must raise LabNotFoundError, got "
            f"{type(e).__name__}: {e}",
        )

    if expected_labs is not None:
        listed = set(names) if names_ok else set()
        for n in expected_labs:
            c.expect(n in listed,
                     f"LabRepository: expected lab {n!r} to appear in list_labs()")
            try:
                repo.load_lab(n)
            except Exception as e:  # noqa: BLE001
                c.expect(False, f"LabRepository: expected lab {n!r} to load, got "
                                f"{type(e).__name__}: {e}")

    c.raise_if_failures()


def assert_reservation_backend_conforms(
    backend: ReservationBackend,
    *,
    known_user: str | None = None,
    known_resources: list[str] | None = None,
) -> None:
    """Assert *backend* satisfies the ReservationBackend contract.

    Structural/type rules always run. When *known_user* and *known_resources*
    (resources that user is known to hold) are both given, round-trip
    consistency rules run too. The optional
    :class:`~otto.reservations.SupportsUsernameCompletion` capability is checked
    only when the backend implements it. Raises a single :class:`AssertionError`
    aggregating every violated rule.

    Parameters
    ----------
    backend : ReservationBackend
        The backend instance under test.
    known_user : str | None
        A username known to hold ``known_resources`` (enables round-trip rules).
    known_resources : list[str] | None
        Resources ``known_user`` is known to currently hold.
    """
    c = ExpectCollector()

    c.expect(
        isinstance(backend, ReservationBackend),
        "ReservationBackend: must satisfy the runtime_checkable ReservationBackend protocol",
    )
    c.expect(callable(getattr(backend, "get_reserved_resources", None)),
             "ReservationBackend: get_reserved_resources must be callable")
    c.expect(callable(getattr(backend, "who_reserved", None)),
             "ReservationBackend: who_reserved must be callable")
    c.expect(callable(getattr(backend, "backend_name", None)),
             "ReservationBackend: backend_name must be callable")

    name = backend.backend_name()
    c.expect(
        isinstance(name, str) and name != "",
        f"ReservationBackend: backend_name() must return a non-empty str, got {name!r}",
    )
    c.expect(name == backend.backend_name(),
             "ReservationBackend: backend_name() must be stable across calls")

    probe_user = known_user if known_user is not None else _PROBE_USER
    reserved = backend.get_reserved_resources(probe_user)
    reserved_ok = isinstance(reserved, set)
    c.expect(
        reserved_ok,
        f"ReservationBackend: get_reserved_resources() must return a set, got "
        f"{type(reserved).__name__}",
    )
    if reserved_ok:
        for r in reserved:
            c.expect(isinstance(r, str),
                     f"ReservationBackend: get_reserved_resources() entries must be str, "
                     f"got {type(r).__name__}")

    probe_resource = known_resources[0] if known_resources else _PROBE_RESOURCE
    holders = backend.who_reserved(probe_resource)
    holders_ok = isinstance(holders, list)
    c.expect(
        holders_ok,
        f"ReservationBackend: who_reserved() must return a list (empty = no holders, "
        f"never None), got {type(holders).__name__}",
    )
    if holders_ok:
        for u in holders:
            c.expect(isinstance(u, str),
                     f"ReservationBackend: who_reserved() entries must be str, "
                     f"got {type(u).__name__}")

    if known_user is not None and known_resources is not None:
        held = backend.get_reserved_resources(known_user)
        for r in known_resources:
            r_holders = backend.who_reserved(r)
            c.expect(
                isinstance(r_holders, list) and known_user in r_holders,
                f"ReservationBackend: who_reserved({r!r}) must include known holder "
                f"{known_user!r}, got {r_holders!r}",
            )
            c.expect(
                isinstance(held, set) and r in held,
                f"ReservationBackend: get_reserved_resources({known_user!r}) must "
                f"include {r!r}, got {held!r}",
            )
            if isinstance(r_holders, list):
                for u in r_holders:
                    c.expect(
                        r in backend.get_reserved_resources(u),
                        f"ReservationBackend: round-trip — {u!r} holds {r!r} per "
                        f"who_reserved, but {r!r} not in get_reserved_resources({u!r})",
                    )

    if isinstance(backend, SupportsUsernameCompletion):
        usernames = backend.list_usernames()
        u_ok = isinstance(usernames, list)
        c.expect(
            u_ok,
            f"SupportsUsernameCompletion: list_usernames() must return a list, got "
            f"{type(usernames).__name__}",
        )
        if u_ok:
            for u in usernames:
                c.expect(isinstance(u, str),
                         f"SupportsUsernameCompletion: list_usernames() entries must be "
                         f"str, got {type(u).__name__}")

    c.raise_if_failures()
```

- [ ] **Step 4: Write the package surface**

Create `src/otto/testing/__init__.py`:

```python
"""Public testing helpers for otto backend authors.

Conformance suites that assert a backend satisfies one of otto's pluggable
interfaces. Import the helper for the interface you implement and call it from a
pytest test (it raises a single ``AssertionError`` listing every contract
violation):

    from otto.testing import (
        assert_lab_repository_conforms,
        assert_reservation_backend_conforms,
    )
"""

from .conformance import (
    assert_lab_repository_conforms as assert_lab_repository_conforms,
)
from .conformance import (
    assert_reservation_backend_conforms as assert_reservation_backend_conforms,
)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/testing/test_conformance.py -v`
Expected: PASS (6 passed) — built-ins conform; non-conforming fakes raise aggregated `AssertionError`; the error-contract sample raises `ReservationBackendError`.

- [ ] **Step 6: Importability check**

Run: `.venv/bin/python -c "import otto.testing; from otto.testing import assert_reservation_backend_conforms, assert_lab_repository_conforms; print('ok')"`
Expected: prints `ok` (the module-top imports resolve with no cycle).

- [ ] **Step 7: Stage (do NOT commit)**

```bash
git add src/otto/testing/__init__.py src/otto/testing/conformance.py tests/unit/testing/test_conformance.py
```

---

## Task 3: `otto.examples.lab_repository` sample + conformance

Ship an in-memory reference `LabRepository`, conformance-verified against the Task-2 helper.

**Files:**
- Create: `src/otto/examples/__init__.py`, `src/otto/examples/lab_repository.py`
- Test: `tests/unit/examples/test_lab_repository.py` *(new)*

**Interfaces:**
- Consumes: `otto.storage` (`LabNotFoundError`, `create_host_from_dict`, `register_lab_repository`); `otto.configmodule.lab.Lab`; `assert_lab_repository_conforms` (Task 2).
- Produces: `otto.examples.lab_repository.ExampleLabRepository(*, repo_dir=None, labs=None)` with `load_lab(name, preferences=None) -> Lab` and `list_labs() -> list[str]`; built-in `_DEMO_LABS` covering labs `east`/`west`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/examples/test_lab_repository.py`:

```python
"""Behavior + conformance for the ExampleLabRepository reference backend."""

import pytest

from otto.configmodule.lab import Lab
from otto.examples.lab_repository import ExampleLabRepository
from otto.host.remote_host import RemoteHost
from otto.storage import LabNotFoundError, register_lab_repository
from otto.storage.registry import _LAB_REPOSITORIES
from otto.testing import assert_lab_repository_conforms


def test_default_demo_dataset_lists_and_loads():
    repo = ExampleLabRepository()
    assert repo.list_labs() == ["east", "west"]
    lab = repo.load_lab("east")
    assert isinstance(lab, Lab)
    assert lab.name == "east"
    assert len(lab.hosts) == 1
    host = next(iter(lab.hosts.values()))
    assert isinstance(host, RemoteHost)


def test_unknown_lab_raises_lab_not_found():
    repo = ExampleLabRepository()
    with pytest.raises(LabNotFoundError):
        repo.load_lab("does-not-exist")


def test_custom_dataset_overrides_demo():
    repo = ExampleLabRepository(labs={
        "only": [{"ip": "10.9.9.9", "element": "node", "creds": {"u": "p"},
                  "resources": ["node"]}],
    })
    assert repo.list_labs() == ["only"]
    assert "node" in repo.load_lab("only").hosts


def test_accepts_repo_dir_for_registry_compatibility(tmp_path):
    # build_lab_repository constructs a custom backend as cls(repo_dir=..., **kwargs)
    repo = ExampleLabRepository(repo_dir=tmp_path)
    assert repo.list_labs() == ["east", "west"]


def test_sample_conforms():
    assert_lab_repository_conforms(
        ExampleLabRepository(), expected_labs=["east", "west"]
    )


def test_registrable_by_name():
    register_lab_repository("example-host-source-test", ExampleLabRepository)
    try:
        assert _LAB_REPOSITORIES["example-host-source-test"] is ExampleLabRepository
    finally:
        _LAB_REPOSITORIES.pop("example-host-source-test", None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/examples/test_lab_repository.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'otto.examples'`.

- [ ] **Step 3: Write the package marker**

Create `src/otto/examples/__init__.py`:

```python
"""Reference backend implementations for otto's pluggable interfaces.

Small, dependency-free, copyable samples that satisfy otto's backend contracts
and are conformance-verified in otto's own test suite:

- :mod:`otto.examples.lab_repository` — an in-memory host source.
- :mod:`otto.examples.reservations` — an in-memory reservation backend.

Copy one as a starting point for your own backend, or register it by name from
an ``init`` module to use it directly.
"""
```

- [ ] **Step 4: Write the sample**

Create `src/otto/examples/lab_repository.py`:

```python
"""In-memory reference :class:`~otto.storage.protocol.LabRepository` (sample).

A teaching/reference host-source backend: it holds a mapping of lab name to a
list of host dicts and builds real hosts via
:func:`otto.storage.create_host_from_dict`. It needs no files or network, so it
runs inside doctests and the conformance suite, and SUT authors can copy it as a
starting point.

Register it from an ``init`` module and select it by name::

    from otto.storage import register_lab_repository
    from otto.examples.lab_repository import ExampleLabRepository

    register_lab_repository("example", ExampleLabRepository)

then in ``.otto/settings.toml``::

    [lab]
    backend = "example"

Direct usage:

>>> from otto.examples.lab_repository import ExampleLabRepository
>>> repo = ExampleLabRepository()
>>> repo.list_labs()
['east', 'west']
>>> lab = repo.load_lab("east")
>>> lab.name
'east'
>>> len(lab.hosts)
1
"""

from pathlib import Path
from typing import Any

from ..configmodule.lab import Lab
from ..storage import (
    LabNotFoundError,
    create_host_from_dict,
)

# A tiny built-in dataset so the sample works out of the box (doctests +
# conformance). Each value is a list of host dicts as they'd appear in a
# hosts.json entry; the mapping key supplies lab membership here, so the
# host-level "labs" field is unnecessary.
_DEMO_LABS: dict[str, list[dict[str, Any]]] = {
    "east": [
        {"ip": "10.0.0.1", "element": "router1", "creds": {"admin": "admin"},
         "resources": ["router1"]},
    ],
    "west": [
        {"ip": "10.0.1.1", "element": "router2", "creds": {"admin": "admin"},
         "resources": ["router2"]},
    ],
}


class ExampleLabRepository:
    """In-memory :class:`~otto.storage.protocol.LabRepository` reference backend.

    Parameters
    ----------
    repo_dir : Path | None
        Accepted for factory/registry uniformity — :func:`otto.storage.build_lab_repository`
        constructs a custom backend as ``cls(repo_dir=..., **kwargs)``. This
        in-memory sample has no files to resolve, so it is ignored.
    labs : dict[str, list[dict]] | None
        Optional mapping of lab name to host dicts. Defaults to a small built-in
        demo dataset.
    """

    def __init__(
        self,
        *,
        repo_dir: Path | None = None,  # noqa: ARG002 — factory/registry uniformity
        labs: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._labs: dict[str, list[dict[str, Any]]] = (
            {k: list(v) for k, v in _DEMO_LABS.items()} if labs is None else labs
        )

    def load_lab(
        self,
        name: str,
        preferences: dict[str, dict[str, Any]] | None = None,
    ) -> Lab:
        if name not in self._labs:
            known = ", ".join(sorted(self._labs)) or "(none)"
            raise LabNotFoundError(
                f"Lab {name!r} not found. Known labs: {known}"
            )
        lab = Lab(name=name)
        for host_data in self._labs[name]:
            host = create_host_from_dict(host_data, preferences=preferences)
            lab.add_host(host)
            lab.resources.update(host.resources)
        return lab

    def list_labs(self) -> list[str]:
        return sorted(self._labs)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/examples/test_lab_repository.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Run the sample's docstring doctests**

Run: `.venv/bin/python -m pytest --doctest-modules src/otto/examples/lab_repository.py -q`
Expected: PASS (the module doctest passes).

- [ ] **Step 7: Stage (do NOT commit)**

```bash
git add src/otto/examples/__init__.py src/otto/examples/lab_repository.py tests/unit/examples/test_lab_repository.py
```

---

## Task 4: `otto.examples.reservations` sample + conformance

Ship an in-memory reference `ReservationBackend` that also implements the optional `SupportsUsernameCompletion` capability, conformance-verified (including round-trip + the capability rule) against the Task-2 helper.

**Files:**
- Create: `src/otto/examples/reservations.py`
- Test: `tests/unit/examples/test_reservations.py` *(new)*

**Interfaces:**
- Consumes: `otto.reservations` (`register_reservation_backend`, `SupportsUsernameCompletion`); `assert_reservation_backend_conforms` (Task 2).
- Produces: `otto.examples.reservations.ExampleReservationBackend(*, url=None, reservations=None)` with `get_reserved_resources(username) -> set[str]`, `who_reserved(resource) -> list[str]` (multi-holder, sorted), `backend_name() -> str` (`"example"`), `list_usernames() -> list[str]`; built-in demo where `alice`→`{lab-a, shared}`, `bob`→`{lab-b, shared}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/examples/test_reservations.py`:

```python
"""Behavior + conformance for the ExampleReservationBackend reference backend."""

from otto.examples.reservations import ExampleReservationBackend
from otto.reservations import SupportsUsernameCompletion, register_reservation_backend
from otto.reservations.registry import _RESERVATION_BACKENDS
from otto.testing import assert_reservation_backend_conforms


def test_backend_name_stable():
    backend = ExampleReservationBackend()
    assert backend.backend_name() == "example"
    assert backend.backend_name() == backend.backend_name()


def test_get_reserved_resources_is_a_str_set():
    backend = ExampleReservationBackend()
    assert backend.get_reserved_resources("alice") == {"lab-a", "shared"}
    assert backend.get_reserved_resources("nobody") == set()


def test_who_reserved_multi_holder_sorted():
    backend = ExampleReservationBackend()
    # "shared" is held by both alice and bob — deterministic, deduped.
    assert backend.who_reserved("shared") == ["alice", "bob"]
    assert backend.who_reserved("lab-a") == ["alice"]
    assert backend.who_reserved("unheld") == []


def test_implements_username_completion():
    backend = ExampleReservationBackend()
    assert isinstance(backend, SupportsUsernameCompletion)
    assert backend.list_usernames() == ["alice", "bob"]


def test_custom_dataset_overrides_demo():
    backend = ExampleReservationBackend(reservations={"carol": ["x"]})
    assert backend.list_usernames() == ["carol"]
    assert backend.who_reserved("x") == ["carol"]


def test_accepts_url_for_factory_uniformity():
    # build_backend may call cls(url=url, **kwargs).
    backend = ExampleReservationBackend(url="https://example")
    assert backend.backend_name() == "example"


def test_sample_conforms_with_round_trip_and_capability():
    assert_reservation_backend_conforms(
        ExampleReservationBackend(),
        known_user="alice",
        known_resources=["lab-a", "shared"],
    )


def test_registrable_by_name():
    register_reservation_backend("example-reservations-test", ExampleReservationBackend)
    try:
        assert _RESERVATION_BACKENDS["example-reservations-test"] is ExampleReservationBackend
    finally:
        _RESERVATION_BACKENDS.pop("example-reservations-test", None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/examples/test_reservations.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'otto.examples.reservations'`.

- [ ] **Step 3: Write the sample**

Create `src/otto/examples/reservations.py`:

```python
"""In-memory reference :class:`~otto.reservations.protocol.ReservationBackend` (sample).

A teaching/reference reservation backend backed by a plain ``user -> resources``
mapping. It needs no files or network, demonstrates multi-holder
``who_reserved`` and the optional
:class:`~otto.reservations.SupportsUsernameCompletion` capability, and is
conformance-verified in otto's own suite.

Register it from an ``init`` module and select it by name::

    from otto.reservations import register_reservation_backend
    from otto.examples.reservations import ExampleReservationBackend

    register_reservation_backend("example", ExampleReservationBackend)

then in ``.otto/settings.toml``::

    [reservations]
    backend = "example"

Direct usage:

>>> from otto.examples.reservations import ExampleReservationBackend
>>> backend = ExampleReservationBackend()
>>> backend.backend_name()
'example'
>>> backend.who_reserved("shared")
['alice', 'bob']
>>> sorted(backend.get_reserved_resources("alice"))
['lab-a', 'shared']
>>> backend.list_usernames()
['alice', 'bob']
"""

# A tiny built-in dataset: "shared" is held by two users to demonstrate the
# multi-holder who_reserved contract.
_DEMO_RESERVATIONS: dict[str, list[str]] = {
    "alice": ["lab-a", "shared"],
    "bob": ["lab-b", "shared"],
}


class ExampleReservationBackend:
    """In-memory :class:`~otto.reservations.protocol.ReservationBackend` reference backend.

    Also implements the optional
    :class:`~otto.reservations.SupportsUsernameCompletion` capability.

    Parameters
    ----------
    url : str | None
        Accepted for factory uniformity — :func:`otto.reservations.build_backend`
        may call ``cls(url=url, ...)``. This in-memory sample ignores it.
    reservations : dict[str, list[str]] | None
        Optional mapping of username to the resources they hold. Defaults to a
        small built-in demo dataset.
    """

    def __init__(
        self,
        *,
        url: str | None = None,  # noqa: ARG002 — factory uniformity
        reservations: dict[str, list[str]] | None = None,
    ) -> None:
        source = _DEMO_RESERVATIONS if reservations is None else reservations
        self._by_user: dict[str, set[str]] = {
            user: set(resources) for user, resources in source.items()
        }

    def get_reserved_resources(self, username: str) -> set[str]:
        return set(self._by_user.get(username, set()))

    def who_reserved(self, resource: str) -> list[str]:
        # Deterministic order, duplicates removed (a user holds a resource once).
        return sorted(
            user for user, resources in self._by_user.items() if resource in resources
        )

    def backend_name(self) -> str:
        return "example"

    def list_usernames(self) -> list[str]:
        return sorted(self._by_user)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/examples/test_reservations.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Run the sample's docstring doctests**

Run: `.venv/bin/python -m pytest --doctest-modules src/otto/examples/reservations.py -q`
Expected: PASS.

- [ ] **Step 6: Stage (do NOT commit)**

```bash
git add src/otto/examples/reservations.py tests/unit/examples/test_reservations.py
```

---

## Task 5: API-doc stubs + full bed-free gate

Document the new public modules (keeps nitpicky green and gives the reference surface its API page) and run the complete bed-free gate.

**Files:**
- Create: `docs/api/testing.rst`, `docs/api/examples.rst`
- Modify: `docs/api/index.rst`

- [ ] **Step 1: Create `docs/api/testing.rst`**

```rst
testing
=======

Reusable conformance helpers for otto's pluggable backend interfaces. Call one
per interface from a pytest test; each raises a single ``AssertionError``
listing every contract violation.

.. autofunction:: otto.testing.assert_lab_repository_conforms

.. autofunction:: otto.testing.assert_reservation_backend_conforms
```

- [ ] **Step 2: Create `docs/api/examples.rst`**

```rst
examples
========

Reference backend implementations — small, dependency-free, copyable samples
that satisfy otto's backend contracts and are conformance-verified in otto's own
test suite.

.. automodule:: otto.examples.lab_repository

.. automodule:: otto.examples.reservations
```

- [ ] **Step 3: Add both to the API toctree**

In `docs/api/index.rst`, add `testing` and `examples` to the `toctree` (after `storage`):

```rst
   reservations
   storage
   testing
   examples
   utils
```

- [ ] **Step 4: Build the docs (nitpicky `-W` + both doctest runners)**

Run: `make docs`
Expected: 0 warnings. This runs `docs-lint`, `docs-html` (nitpicky), `doctest` (Sphinx — executes the sample docstring doctests rendered via autodoc), and `doctest-src` (`pytest --doctest-modules src/otto` — executes the same doctests). All must pass. If a numpydoc/autodoc cross-reference is unresolved, the fix is local to these doc files (qualify the reference or confirm the target module is documented). Re-run until clean.

- [ ] **Step 5: Type-check**

Run: `make typecheck`
Expected: clean (`All checks passed!`). The `# noqa: ARG002` on the unused `repo_dir`/`url` params is intentional (factory/registry uniformity). No file in this plan uses `from __future__ import annotations`.

- [ ] **Step 6: Full bed-free coverage gate**

Run: `make coverage-unit`
Expected: all `tests/unit` (+ unit-marked `tests/e2e`) pass; coverage ≥85% (the new `expect.py` coverage from Task 1, plus the helpers/samples exercised by Tasks 2–4, should raise the total). Do **not** run `make coverage` / `make nox` (bed-dependent — Chris's call).

- [ ] **Step 7: Stage (do NOT commit)**

```bash
git add docs/api/testing.rst docs/api/examples.rst docs/api/index.rst
```

- [ ] **Step 8: Hand off**

Report the full staged file list and gate results to the controller for the final whole-branch review. Do **not** commit — Chris commits.

---

## Self-Review (controller, before dispatching Task 1)

1. **Spec coverage:**
   - §4.1 public surface (`otto.testing` with one helper per interface, single `ExpectCollector`, structural always + behavioral on ground truth, `raise_if_failures`) → Task 2. ✅
   - §4.2 LabRepository rules (isinstance; callables; `list_labs()→list[str]`; each lab loads to a `Lab`; host invariants key==id & RemoteHost; unknown→`LabNotFoundError`; idempotent; `expected_labs`) → Task 2 helper + Tasks 2/3 tests. ✅
   - §4.3 ReservationBackend rules (isinstance; callables; non-empty stable `backend_name`; `get_reserved_resources→set[str]`; `who_reserved→list[str]` never None; round-trip on ground truth; error-contract via failing sample; optional `SupportsUsernameCompletion`) → Task 2 helper + Tasks 2/4 tests. ✅
   - §4.4 `ExpectCollector` tests → Task 1; pytest entry-point → explicitly deferred (Global Constraints). ✅
   - §5 samples (`otto.examples.lab_repository` builds hosts via `create_host_from_dict`, raises `LabNotFoundError`, registrable; `otto.examples.reservations` multi-holder, stable name, round-trip, registrable, `list_usernames`) → Tasks 3, 4. ✅
   - §7 conformance against built-ins (Task 2) **and** samples (Tasks 3, 4); doctests via `make doctest`/`doctest-src`; bed-free gate green → Tasks 3–5. ✅
2. **Out of scope (correctly deferred to Plan D):** host-database guide, reservations-guide upgrade, team-setup hub, executable `{doctest}` blocks in `docs/guide/*`. Samples ship with self-contained docstring doctests + API stubs only. ✅
3. **Type consistency:** helper signatures `assert_lab_repository_conforms(repo, *, expected_labs=None)` and `assert_reservation_backend_conforms(backend, *, known_user=None, known_resources=None)` are used identically across Tasks 2/3/4. Sample constructors `ExampleLabRepository(*, repo_dir=None, labs=None)` and `ExampleReservationBackend(*, url=None, reservations=None)` match the factory-construction contracts from Plans A/B. `who_reserved`/`get_reserved_resources`/`list_labs`/`load_lab` signatures match the committed protocols. ✅
4. **Engine reuse / docs hygiene:** `ExpectCollector` used unmodified; no `from __future__ import annotations` anywhere; conformance helpers import annotated types at module top (real types → clean autodoc under nitpicky). ✅
5. **Bed safety:** no task runs any lab/bed/Vagrant target; gate is `coverage-unit` + `typecheck` + `docs`. ✅
