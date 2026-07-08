# Host ID & Naming Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a host's canonical id a slugged derivation of `element` (no separate id field), enforce global id uniqueness, add a lab-scoped logical index that drives readable display names and CLI positional handles, and move all correlation (monitor, session) off the display name onto the id.

**Architecture:** `make_host_id` gains a `slug()` step (lowercase → non-alnum runs → single hyphen), so the id is delimiter-safe and `element` can be a human string; simple `[a-z0-9]` elements slug to themselves so existing ids are byte-identical. A lab-assembly pass stamps a per-`element`-slug `logical_index` (element_id ascending, only when the element repeats) that feeds the space-joined, original-case display `name` and the CLI positional resolver. Uniqueness becomes fail-loud at every registration path. Correlation keys (monitor series/DB, session ids) move from `host.name` to `host.id`.

**Tech Stack:** Python 3.10+, pydantic v2 boundary specs (`OttoModel`, `extra="forbid"`), dataclass runtime hosts, Typer CLI, pytest.

## Global Constraints

- **`slug` is a frozen stability contract.** `slug(s)` = `s.lower()` → replace every maximal run of `[^a-z0-9]` with a single `-` → strip leading/trailing `-`. Empty result is a load error. Once shipped it must never change (it feeds `make_link_id` and sentinels). Pin with a STABILITY-CONTRACT docstring + round-trip tests.
- **The id structure is unchanged:** `slug(element)` + `element_id?` + (`_` + `slug(board)` + `slot?`)?. The **only** `_` is the element↔board separator; hyphens appear **only** inside a slug. Simple `[a-z0-9]` elements must slug to themselves so existing ids/link-ids/fixtures/`test_capability_hosts.py` stay byte-identical.
- **No separate custom-`id` field.** A readable handle is a richer `element`. `element` owns the id; renaming it is an identity change.
- **Display name = correlation-free.** `name` = space-joined, ORIGINAL-CASE `element [logical ID] [board] [slot]`, each part omitted when absent. The number is the logical index (never raw `element_id`), shown only when the element repeats in the lab. An explicit construction-time `name` is an override returned verbatim. Exposed directly on the host as `host.name`.
- **Logical index is NOT a contract.** Lab-scoped, element_id-ascending, re-derived per active-lab composition; never stored, hashed, or used as a correlation key.
- **Uniqueness is fail-loud** across all loaded labs; the error names both offending hosts.
- **No `from __future__ import annotations`** (breaks the Sphinx `-W` docs gate). Use real 3.10+ annotations.
- **Tests use `tmp_path`**, never write inside the repo tree.
- Gates per task: the task's own pytest; full-branch gate at the end = `nox -s lint typecheck` + `make coverage` + `make docs`.

---

### Task 1: `slug()` + `make_host_id` slugging

**Files:**
- Modify: `src/otto/host/remote_host.py:53-71` (add `slug`, slug `make_host_id`)
- Test: `tests/unit/host/test_host_id.py` (new)

**Interfaces:**
- Produces: `slug(value: str) -> str`; `make_host_id(element: str, element_id: int | None, board: str | None, slot: int | None) -> str` (unchanged signature, now slugs its string inputs). Both importable from `otto.host.remote_host`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/host/test_host_id.py`:

```python
"""slug() + make_host_id() — the frozen host-id derivation contract."""

import pytest

from otto.host.remote_host import make_host_id, slug


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("server", "server"),           # simple token -> identity
        ("Server", "server"),           # case-folded
        ("Lab X Server", "lab-x-server"),   # spaces -> single hyphen
        ("Big  Board!", "big-board"),   # punctuation + run collapse
        ("wr_linux", "wr-linux"),       # underscore folds to hyphen (never reaches id as _)
        ("--edge--", "edge"),           # strip leading/trailing hyphens
    ],
)
def test_slug_cases(raw, expected):
    assert slug(raw) == expected


def test_slug_empty_is_empty_string():
    # All-punctuation slugs to empty; callers treat empty as an error.
    assert slug("___") == ""
    assert slug("") == ""


def test_make_host_id_simple_element_is_identity():
    # Contract: a simple [a-z0-9] element slugs to itself, so ids are byte-identical
    # to the pre-slug make_host_id — existing link ids/fixtures are undisturbed.
    assert make_host_id("test", 5, "boardx", 2) == "test5_boardx2"
    assert make_host_id("solo", None, None, None) == "solo"
    assert make_host_id("Test", 5, "BoardX", 2) == "test5_boardx2"


def test_make_host_id_multiword_element_slugs():
    assert make_host_id("Lab X Server", None, None, None) == "lab-x-server"
    assert make_host_id("Lab X Server", 2, None, None) == "lab-x-server2"


def test_make_host_id_only_underscore_is_board_delimiter():
    # Hyphens live only inside slugs; the single underscore is the board delimiter.
    hid = make_host_id("edge node", 1, "line card", 3)
    assert hid == "edge-node1_line-card3"
    assert hid.count("_") == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_host_id.py -v`
Expected: FAIL — `ImportError: cannot import name 'slug'`.

- [ ] **Step 3: Implement `slug` and slug `make_host_id`**

In `src/otto/host/remote_host.py`, add `import re` at the top with the other stdlib imports, and replace the current `make_host_id` (lines 53-71) with:

```python
_SLUG_RUN = re.compile(r"[^a-z0-9]+")


def slug(value: str) -> str:
    """Normalize an identity token into a URL/id-safe slug.

    STABILITY CONTRACT — feeds ``make_host_id`` → ``make_link_id`` → sentinels;
    changing it re-maps every id and invalidates live tunnel markers. Never
    change the algorithm:

    - lower-case;
    - replace every maximal run of characters outside ``[a-z0-9]`` with a
      single ``-`` (so spaces, ``_``, ``.``, ``:``, ``|``, ``/``, and
      punctuation never reach an id);
    - strip leading/trailing ``-``.

    A value that slugs to ``""`` (all punctuation/whitespace) is invalid — the
    caller reports it as a load error.
    """
    return _SLUG_RUN.sub("-", value.lower()).strip("-")


def make_host_id(
    element: str,
    element_id: int | None,
    board: str | None,
    slot: int | None,
) -> str:
    """Compose a host's ``id`` from its identity fields — the single source of the id format.

    Called by ``RemoteHost._generate_id`` and by host_preferences selector
    matching (so a selector regex matches the same string a built host reports).
    ``element``/``board`` are slugged (§ ``slug``); the only structural
    delimiter is ``_`` between the element-slug and the board-slug. A simple
    ``[a-z0-9]`` element slugs to itself, so its id is byte-identical to the
    pre-slug format.
    """
    element_id_str = "" if element_id is None else f"{element_id}"
    ne = f"{slug(element)}{element_id_str}"
    if board is None:
        return ne
    slot_str = "" if slot is None else f"{slot}"
    return f"{ne}_{slug(board)}{slot_str}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_host_id.py -v`
Expected: PASS (all).

- [ ] **Step 5: Verify the existing format-lock test still passes**

Run: `uv run pytest tests/unit/host/test_capability_hosts.py -v`
Expected: PASS — `make_host_id("Test",5,"BoardX",2) == "test5_boardx2"` etc. unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/otto/host/remote_host.py tests/unit/host/test_host_id.py
git commit -m "feat(host): slug element/board in make_host_id; add slug() stability contract"
```

---

### Task 2: Input charset + integer validators

**Files:**
- Modify: `src/otto/models/host.py` (add validators to `HostSpec`, after the existing `_coerce_interface_shorthand` at ~line 251)
- Test: `tests/unit/models/test_host_spec_identity.py` (new)

**Interfaces:**
- Consumes: `slug` from `otto.host.remote_host` (Task 1).
- Produces: `HostSpec` rejects an `element`/`board` that slugs to empty, and a negative `element_id`/`slot`, at validation time with a `ValueError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/models/test_host_spec_identity.py`:

```python
"""HostSpec identity-field validation: element/board must slug non-empty; ids >= 0."""

import pytest
from pydantic import ValidationError

from otto.models.host import UnixHostSpec


def _spec(**over):
    base = {"ip": "10.0.0.1", "element": "server"}
    base.update(over)
    return UnixHostSpec.model_validate(base)


def test_valid_multiword_element_accepted():
    spec = _spec(element="Lab X Server")
    assert spec.element == "Lab X Server"  # raw string preserved on the spec


def test_element_that_slugs_empty_is_rejected():
    with pytest.raises(ValidationError, match="slug"):
        _spec(element="___")


def test_board_that_slugs_empty_is_rejected():
    with pytest.raises(ValidationError, match="slug"):
        _spec(element="server", board="!!!")


def test_negative_element_id_rejected():
    with pytest.raises(ValidationError):
        _spec(element_id=-1)


def test_negative_slot_rejected():
    with pytest.raises(ValidationError):
        _spec(board="blade", slot=-2)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/models/test_host_spec_identity.py -v`
Expected: FAIL — `_spec(element="___")` does not raise (no validator yet).

- [ ] **Step 3: Add the validators**

In `src/otto/models/host.py`, add after the `_coerce_interface_shorthand` validator (~line 251). Import `slug` lazily inside the validator to avoid an import cycle (`models` is imported early):

```python
    @field_validator("element", "board")
    @classmethod
    def _validate_slugs_nonempty(cls, v: str | None) -> str | None:
        """``element``/``board`` are free human strings but must slug to a
        non-empty ``[a-z0-9-]`` token (else they cannot form a valid id)."""
        if v is None:
            return v
        from ..host.remote_host import slug

        if not slug(v):
            raise ValueError(
                f"{v!r} slugs to an empty id (needs at least one letter or digit)"
            )
        return v

    @field_validator("element_id", "slot")
    @classmethod
    def _validate_nonnegative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"must be >= 0, got {v}")
        return v
```

Confirm `field_validator` is already imported at the top of `src/otto/models/host.py` (it is used by the existing validators).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/models/test_host_spec_identity.py -v`
Expected: PASS (all).

- [ ] **Step 5: Run the existing HostSpec/model tests for regressions**

Run: `uv run pytest tests/unit/models/ tests/unit/host/test_capability_hosts.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/otto/models/host.py tests/unit/models/test_host_spec_identity.py
git commit -m "feat(models): reject element/board that slug empty and negative element_id/slot"
```

---

### Task 3: Move correlation off the display name (monitor + session use `host.id`)

**Files:**
- Modify: `src/otto/monitor/collector.py` (every `host_name = target.host.name` → `.id`)
- Modify: `src/otto/host/unix_host.py:343,371,512,549` (`host_id=self.name` → `host_id=self.id`)
- Test: `tests/unit/monitor/test_attribution_by_id.py` (new)

**Interfaces:**
- Produces: monitor series keys and DB rows keyed on `host.id`; `SessionManager` receives `host_id=host.id`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/monitor/test_attribution_by_id.py`:

```python
"""Monitor attributes series/log rows by host.id, not the display name."""

from otto.host.unix_host import UnixHost


def _host(**over):
    base = {"ip": "10.0.0.1", "element": "server", "name": "Friendly Label"}
    base.update(over)
    return UnixHost(**base)


def test_id_and_name_diverge_for_this_host():
    # A construction-time name override makes name != id, so attribution source matters.
    h = _host()
    assert h.id == "server"
    assert h.name == "Friendly Label"


def test_collector_attributes_by_id():
    # Both attribution SOURCES (shell + SNMP) feed target.host.id; diagnostic
    # log messages may still use the display name.
    import re

    from otto.monitor import collector as collector_mod

    text = open(collector_mod.__file__).read()  # noqa: SIM115 — one-shot read in a test
    # Shell path: _process_host_results(...) is called with the id (tolerate newline/indent).
    assert re.search(r"_process_host_results\(\s*target\.host\.id", text)
    # SNMP path: the host_name source is the id, not the name.
    assert "host_name = target.host.id" in text
    assert "host_name = target.host.name" not in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/monitor/test_attribution_by_id.py -v`
Expected: FAIL — `target.host.name` still present in `collector.py`.

- [ ] **Step 3: Change monitor attribution to `host.id`**

In `src/otto/monitor/collector.py`, change the two attribution SOURCES to `target.host.id`:

- line ~301 — the `target.host.name` passed as the first argument to `_process_host_results(...)` (the shell-collection attribution source, which becomes the series/DB `host_name`) → `target.host.id`.
- line ~553 — `host_name = target.host.name` (the SNMP-collection attribution source) → `host_name = target.host.id`.

**Leave** the two diagnostic `logger.warning(...)` uses of `target.host.name` (lines ~311 and ~368) unchanged — those are human-readable log messages (display), not attribution. The local variable name `host_name` stays; only its source changes. Do not rename the `host` column in `db.py`/`store.py` — they store whatever string they are handed, which is now the id.

- [ ] **Step 4: Fix the `host_id=self.name` conflation**

In `src/otto/host/unix_host.py`, change each `host_id=self.name` to `host_id=self.id` (lines 343, 371, 512, 549 — the `SessionManager(...)` / session-open call sites). Leave the sibling `name=self.name` arguments untouched (those are genuinely the display name).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/monitor/test_attribution_by_id.py tests/unit/monitor/ -v`
Expected: PASS. If a monitor test asserts a series key built from the old display name, update it to the id — that is the intended behavior change.

- [ ] **Step 6: Commit**

```bash
git add src/otto/monitor/collector.py src/otto/host/unix_host.py tests/unit/monitor/test_attribution_by_id.py
git commit -m "fix(monitor,host): attribute series/sessions by host.id, not display name"
```

---

### Task 4: Logical index field + lab-assembly pass

**Files:**
- Modify: `src/otto/host/remote_host.py` (add `logical_index` field)
- Modify: `src/otto/configmodule/lab.py` (add `_assign_logical_indices`; call in `load_lab` and `__add__`)
- Test: `tests/unit/configmodule/test_logical_index.py` (new)

**Interfaces:**
- Consumes: `slug` (Task 1).
- Produces: `RemoteHost.logical_index: int | None` (default `None`); `Lab._assign_logical_indices() -> None` (stamps each host's `logical_index`: `1..N` by `element_id` ascending within its `slug(element)` group when the group has ≥2 members, else `None`).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/configmodule/test_logical_index.py`:

```python
"""Lab-assembly stamps a per-element-slug logical index (element_id ascending)."""

from otto.configmodule.lab import Lab
from otto.host.unix_host import UnixHost


def _mk(element, element_id=None, ip="10.0.0.1"):
    return UnixHost(ip=ip, element=element, element_id=element_id)


def _lab(*hosts):
    lab = Lab(name="t")
    for h in hosts:
        lab.add_host(h)
    lab._assign_logical_indices()
    return lab


def test_unique_element_has_no_logical_index():
    a = _mk("server")
    _lab(a)
    assert a.logical_index is None


def test_repeated_element_numbered_by_element_id_ascending():
    a = _mk("server", element_id=103)
    b = _mk("server", element_id=47)
    c = _mk("server", element_id=288)
    _lab(a, b, c)
    assert (b.logical_index, a.logical_index, c.logical_index) == (1, 2, 3)


def test_grouping_is_by_element_slug():
    # "Server" and "server" share a slug -> same group.
    a = _mk("Server", element_id=1)
    b = _mk("server", element_id=2)
    _lab(a, b)
    assert (a.logical_index, b.logical_index) == (1, 2)


def test_reassigned_after_merge():
    lab_a = Lab(name="a")
    a = _mk("server", element_id=1)
    lab_a.add_host(a)
    lab_a._assign_logical_indices()
    assert a.logical_index is None  # alone in lab_a

    lab_b = Lab(name="b")
    b = _mk("server", element_id=2, ip="10.0.0.2")
    lab_b.add_host(b)
    merged = lab_a + lab_b
    assert (a.logical_index, b.logical_index) == (1, 2)  # re-derived over the union
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/configmodule/test_logical_index.py -v`
Expected: FAIL — `AttributeError: 'UnixHost' object has no attribute 'logical_index'`.

- [ ] **Step 3: Add the `logical_index` field**

In `src/otto/host/remote_host.py`, in the `RemoteHost` dataclass field block (near the `id`/`name` fields around line 112-115), add:

```python
    logical_index: int | None = field(default=None, init=False, repr=False)
    """Lab-scoped position among same-``slug(element)`` siblings (1-based, by
    ``element_id`` ascending), stamped by ``Lab._assign_logical_indices``;
    ``None`` when the element is unique in the lab. Display/CLI sugar only —
    never stored, hashed, or used as a correlation key."""
```

Confirm `field` is imported from `dataclasses` at the top of `remote_host.py` (it is — other `init=False` fields use it).

- [ ] **Step 4: Add the assembly pass and wire it in**

In `src/otto/configmodule/lab.py`, add a method to `Lab` (after `static_links`), importing `slug` lazily:

```python
    def _assign_logical_indices(self) -> None:
        """Stamp each host's ``logical_index``: 1..N by ``element_id`` ascending
        within its ``slug(element)`` group, but only when the group has >1
        member (a unique element gets ``None``). Idempotent; re-run after merges.
        """
        from collections import defaultdict

        from ..host.remote_host import RemoteHost, slug

        groups: "dict[str, list[RemoteHost]]" = defaultdict(list)
        for host in self.hosts.values():
            if isinstance(host, RemoteHost) and host.element:
                groups[slug(host.element)].append(host)
        for members in groups.values():
            if len(members) < 2:
                members[0].logical_index = None
                continue
            ordered = sorted(
                members,
                key=lambda h: (h.element_id is None, h.element_id or 0, h.id),
            )
            for position, host in enumerate(ordered, start=1):
                host.logical_index = position
```

Wire it into `load_lab` — after the built-in `local` host injection (after the `if BUILTIN_LOCAL_HOST_ID not in lab.hosts:` block, before `return lab`):

```python
    lab._assign_logical_indices()

    return lab
```

And re-run it at the end of `Lab.__add__` (before `return self`):

```python
        self._assign_logical_indices()

        return self
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/configmodule/test_logical_index.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/otto/host/remote_host.py src/otto/configmodule/lab.py tests/unit/configmodule/test_logical_index.py
git commit -m "feat(host,lab): stamp lab-scoped logical_index (element_id asc, only when element repeats)"
```

---

### Task 5: Display name — spaced, original-case, logical-index-aware

**Files:**
- Modify: `src/otto/host/remote_host.py:258-263` (`_generate_name`)
- Modify: `src/otto/host/unix_host.py:314-318` and `src/otto/host/embedded_host.py:282-286` (`__post_init__` override tracking)
- Modify: `src/otto/configmodule/lab.py` (`_assign_logical_indices` also refreshes generated names)
- Test: `tests/unit/host/test_display_name.py` (new)

**Interfaces:**
- Consumes: `RemoteHost.logical_index` (Task 4).
- Produces: `host.name` = explicit override if given, else space-joined original-case `element [logical_index] [board] [slot]`. `RemoteHost._name_overridden: bool` distinguishes the two.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/host/test_display_name.py`:

```python
"""Display name = space-joined, original-case element [logical] [board] [slot]."""

from otto.configmodule.lab import Lab
from otto.host.unix_host import UnixHost


def _mk(element, element_id=None, board=None, slot=None, name="", ip="10.0.0.1"):
    return UnixHost(ip=ip, element=element, element_id=element_id, board=board, slot=slot, name=name)


def test_unique_element_no_number_original_case():
    # Standalone host (no lab assembly) -> logical_index is None -> no number.
    h = _mk("Lab X Server")
    assert h.name == "Lab X Server"


def test_board_and_slot_space_separated_original_case():
    h = _mk("Node", board="Blade", slot=3)
    assert h.name == "Node Blade 3"


def test_repeated_element_shows_logical_index():
    a = _mk("Server", element_id=103)
    b = _mk("Server", element_id=47, ip="10.0.0.2")
    lab = Lab(name="t")
    lab.add_host(a)
    lab.add_host(b)
    lab._assign_logical_indices()
    assert b.name == "Server 1"
    assert a.name == "Server 2"


def test_explicit_name_override_wins():
    h = _mk("Server", element_id=1, name="The Big One")
    lab = Lab(name="t")
    lab.add_host(h)
    lab.add_host(_mk("Server", element_id=2, ip="10.0.0.9"))
    lab._assign_logical_indices()
    assert h.name == "The Big One"  # override survives assembly


def test_board_host_with_logical_index_orders_parts():
    a = _mk("Node", element_id=1, board="Blade", slot=2)
    b = _mk("Node", element_id=2, board="Blade", slot=5, ip="10.0.0.2")
    lab = Lab(name="t")
    lab.add_host(a)
    lab.add_host(b)
    lab._assign_logical_indices()
    assert a.name == "Node 1 Blade 2"
    assert b.name == "Node 2 Blade 5"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/host/test_display_name.py -v`
Expected: FAIL — names still use the old `"{element}{element_id} {board}{slot}"` format (e.g. `"Node Blade3"`).

- [ ] **Step 3: Rewrite `_generate_name`**

Replace `_generate_name` in `src/otto/host/remote_host.py` (lines 258-263) with:

```python
    def _generate_name(self) -> str:
        """Space-joined, ORIGINAL-CASE display label: ``element [logical] [board] [slot]``.

        The number is the lab-scoped ``logical_index`` (never the raw
        ``element_id``), present only when the element repeats in the lab. Parts
        are omitted when absent. This is a label, not an id — case is preserved.
        """
        parts: list[str] = [self.element]
        if self.logical_index is not None:
            parts.append(str(self.logical_index))
        if self.board:
            parts.append(self.board)
            if self.slot is not None:
                parts.append(str(self.slot))
        return " ".join(parts)
```

- [ ] **Step 4: Track override + generate name without the number at construction**

In `src/otto/host/unix_host.py` `__post_init__` (lines 314-318), replace:

```python
        self.id = self._generate_id()
        if not self.name:
            self.name = self._generate_name()
```

with:

```python
        self.id = self._generate_id()
        self._name_overridden = bool(self.name)
        if not self._name_overridden:
            self.name = self._generate_name()  # no lab context yet -> no number
```

Make the identical change in `src/otto/host/embedded_host.py` `__post_init__` (lines 282-286).

Add the `_name_overridden` field to `RemoteHost` (near `logical_index`, `src/otto/host/remote_host.py`):

```python
    _name_overridden: bool = field(default=False, init=False, repr=False)
    """True when ``name`` was supplied at construction (an override); such names
    are never regenerated by the lab-assembly pass."""
```

- [ ] **Step 5: Refresh generated names in the assembly pass**

In `src/otto/configmodule/lab.py`, extend `_assign_logical_indices` so that after stamping each host's `logical_index`, non-overridden names are regenerated. Replace the two `host.logical_index = ...`/`members[0].logical_index = None` assignments' surrounding loop body so each stamped host also refreshes its name:

```python
        for members in groups.values():
            if len(members) < 2:
                members[0].logical_index = None
                _refresh_name(members[0])
                continue
            ordered = sorted(
                members,
                key=lambda h: (h.element_id is None, h.element_id or 0, h.id),
            )
            for position, host in enumerate(ordered, start=1):
                host.logical_index = position
                _refresh_name(host)
```

and add a module-level helper in `lab.py`:

```python
def _refresh_name(host: "Host") -> None:
    """Recompute a non-overridden host's display name from its current logical_index."""
    if getattr(host, "_name_overridden", False):
        return
    generate = getattr(host, "_generate_name", None)
    if generate is not None:
        host.name = generate()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/host/test_display_name.py -v`
Expected: PASS (all).

- [ ] **Step 7: Update any existing display-name assertions**

Run: `grep -rn '\.name ==' tests/unit tests/integration | grep -i host`
Update any assertion that expected the old `"elementN board S"` (no-space-before-number, concatenated board/slot) form to the new spaced form. Then:

Run: `uv run pytest tests/unit/host/ -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/otto/host/remote_host.py src/otto/host/unix_host.py src/otto/host/embedded_host.py src/otto/configmodule/lab.py tests/unit/host/test_display_name.py
git commit -m "feat(host): space-joined original-case display name using logical index; override-aware"
```

---

### Task 6: Fail-loud global uniqueness

**Files:**
- Modify: `src/otto/configmodule/lab.py` (`__add__` uniqueness; shared `_reject_duplicate_id` helper reused by `add_host`)
- Modify: `src/otto/storage/json_repository.py` (addressing-map duplicate detection)
- Modify: `src/otto/docker/compose.py:288,442` (route direct writes through `add_host`)
- Test: `tests/unit/configmodule/test_id_uniqueness.py` (new)

**Interfaces:**
- Consumes: `Lab.hosts` keyed by id.
- Produces: a `ValueError` (or `KeyError` where `add_host` already raises it) naming both hosts on any duplicate id, at every registration path.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/configmodule/test_id_uniqueness.py`:

```python
"""Duplicate host ids fail loud at every registration path."""

import pytest

from otto.configmodule.lab import Lab
from otto.host.unix_host import UnixHost


def _mk(element, ip="10.0.0.1", element_id=None):
    return UnixHost(ip=ip, element=element, element_id=element_id)


def test_add_host_rejects_duplicate():
    lab = Lab(name="t")
    lab.add_host(_mk("server"))
    with pytest.raises((KeyError, ValueError), match="server"):
        lab.add_host(_mk("server", ip="10.0.0.2"))


def test_merge_rejects_colliding_id():
    # Two DIFFERENT hosts (different ip) colliding on id -> fail loud.
    a = Lab(name="a")
    a.add_host(_mk("server"))  # ip 10.0.0.1
    b = Lab(name="b")
    b.add_host(_mk("server", ip="10.0.0.2"))
    with pytest.raises((KeyError, ValueError), match="server"):
        _ = a + b


def test_same_host_in_two_labs_merges_without_error():
    # A host declared in multiple labs is reconstructed as a distinct object per
    # lab but has the same id AND ip -> dedup on merge, NOT a collision.
    a = Lab(name="a")
    a.add_host(_mk("server", ip="10.0.0.5"))
    b = Lab(name="b")
    b.add_host(_mk("server", ip="10.0.0.5"))  # same id, same ip = the same host
    merged = a + b  # must not raise
    assert merged.hosts["server"].ip == "10.0.0.5"


def test_distinct_slug_collision_detected():
    # Two different raw elements that slug to the same id collide.
    a = Lab(name="a")
    a.add_host(UnixHost(ip="10.0.0.1", element="Lab X Server"))
    with pytest.raises((KeyError, ValueError), match="lab-x-server"):
        a.add_host(UnixHost(ip="10.0.0.2", element="lab-x-server"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/configmodule/test_id_uniqueness.py -v`
Expected: FAIL — `test_merge_rejects_colliding_id` does not raise (`__add__` uses `dict.update`, silently overwriting).

- [ ] **Step 3: Add a shared duplicate check and use it in `__add__`**

In `src/otto/configmodule/lab.py`, add a helper method on `Lab` and call it from `__add__`. Replace `self.hosts.update(other.hosts)` (line 89) with a guarded loop:

```python
        for host in other.hosts.values():
            existing = self.hosts.get(host.id)
            # A host declared in multiple labs is reconstructed as a DISTINCT
            # object per lab (the repository builds fresh per load_lab), so object
            # identity cannot tell "same host, two labs" from "two different hosts,
            # colliding id". Use the connection identity (ip): same id + same ip =
            # the same host (dedup, no error); same id + different ip = two
            # different machines colliding (fail loud).
            if existing is not None and existing is not host and existing.ip != host.ip:
                raise ValueError(
                    f"Duplicate host id {host.id!r} for different hosts "
                    f"({existing.ip} in {self.name!r} vs {host.ip} in {other.name!r}). "
                    f"Differentiate the element string, assign/uniquify element_id, "
                    f"or set board/slot."
                )
            self.hosts[host.id] = host
```

(The existing `add_host` `KeyError` already covers the single-lab path; keep it. Within one lab a duplicate id is always an error; across merged labs the ip check permits a host legitimately shared across labs while catching genuinely distinct collisions.)

- [ ] **Step 4: Detect duplicates in the cross-lab addressing map**

In `src/otto/storage/json_repository.py`, where the cross-lab addressing map is built (`addressing[host_id] = host_addressing`, ~line 154-162), surface a duplicate id that maps to a *different* addressing with a **WARNING, not a raise**. This map spans ALL lab files — including labs unrelated to the one being loaded — and the loop is deliberately resilient (per-item skip, added in #1) so an unrelated lab's problem never crashes this load. The hard failure belongs to the loaded-lab paths (`add_host` within a lab, `Lab.__add__` across merged labs), not here. Replace the silent last-wins:

```python
            if host_id in addressing and addressing[host_id] != host_addressing:
                logger.warning(
                    "Duplicate host id %r across lab files with differing addressing; "
                    "keeping the first. Differentiate the element, element_id, or board/slot.",
                    host_id,
                )
                continue
            addressing[host_id] = host_addressing
```

(`logger` is already defined at module scope in this file. The variables `host_id` / `host_addressing` / `addressing` match the existing code — the new guard goes right before the final `addressing[host_id] = host_addressing` line, inside the `for h in all_hosts_data:` loop.)

- [ ] **Step 5: Route docker container registration through `add_host`**

In `src/otto/docker/compose.py`, replace the direct `lab.hosts[host.id] = host` (line 288) and `lab.hosts[placeholder.id] = placeholder` (line 442) with `lab.add_host(host)` / `lab.add_host(placeholder)` so the duplicate guard applies. Confirm no code path *intends* to overwrite an existing container host; if a re-register is intentional, delete-then-add explicitly rather than silently overwriting.

(The docker dotted id `parent.project.service` is already lower-cased and its segments come from Docker-Compose names constrained to `[a-z0-9._-]`, so it is charset-safe as-is — spec §6.4 needs no separate slug step here; only the uniqueness guard is added.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/configmodule/test_id_uniqueness.py tests/unit/docker/ tests/unit/storage/ -v`
Expected: PASS. Fix any docker test that relied on silent container re-registration by making the re-register explicit.

- [ ] **Step 7: Commit**

```bash
git add src/otto/configmodule/lab.py src/otto/storage/json_repository.py src/otto/docker/compose.py tests/unit/configmodule/test_id_uniqueness.py
git commit -m "feat(lab): fail loud on duplicate host id across add_host, merge, addressing, docker"
```

---

### Task 7: CLI positional resolution + shadow warning

**Files:**
- Modify: `src/otto/context.py:100-112` (`get_host` positional fallback)
- Modify: `src/otto/configmodule/lab.py` (add `Lab.resolve_handle` + shadow-warning emit in `_assign_logical_indices`)
- Test: `tests/unit/configmodule/test_handle_resolution.py` (new)

**Interfaces:**
- Consumes: `Lab.hosts`, `RemoteHost.logical_index`, `slug` (Task 1/4).
- Produces: `Lab.resolve_handle(handle: str) -> Host | None` (canonical id exact match → else positional `<element-slug><N>` → else `None`); `get_host` uses it before raising.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/configmodule/test_handle_resolution.py`:

```python
"""CLI handle resolution: canonical id wins, positional (element-slug + N) falls back."""

from otto.configmodule.lab import Lab
from otto.host.unix_host import UnixHost


def _lab(*specs):
    lab = Lab(name="t")
    for element, eid, ip in specs:
        lab.add_host(UnixHost(ip=ip, element=element, element_id=eid))
    lab._assign_logical_indices()
    return lab


def test_exact_canonical_id_wins():
    lab = _lab(("server", 47, "10.0.0.1"), ("server", 103, "10.0.0.2"))
    assert lab.resolve_handle("server47").id == "server47"


def test_positional_fallback_large_element_ids():
    lab = _lab(("server", 47, "10.0.0.1"), ("server", 103, "10.0.0.2"))
    # No canonical "server1"/"server2" -> positional.
    assert lab.resolve_handle("server1").id == "server47"
    assert lab.resolve_handle("server2").id == "server103"


def test_multiword_element_slug_handle():
    lab = _lab(("Lab X Server", None, "10.0.0.1"))
    assert lab.resolve_handle("lab-x-server").id == "lab-x-server"


def test_unknown_handle_returns_none():
    lab = _lab(("server", 1, "10.0.0.1"))
    assert lab.resolve_handle("nope9") is None
    assert lab.resolve_handle("server5") is None  # no 5th server, no canonical


def test_canonical_shadows_positional():
    # ids {2,5}: canonical "server2" (id 2) wins over positional 2 (id 5).
    lab = _lab(("server", 2, "10.0.0.1"), ("server", 5, "10.0.0.2"))
    assert lab.resolve_handle("server2").id == "server2"  # canonical, not the 2nd
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/configmodule/test_handle_resolution.py -v`
Expected: FAIL — `AttributeError: 'Lab' object has no attribute 'resolve_handle'`.

- [ ] **Step 3: Implement `Lab.resolve_handle`**

In `src/otto/configmodule/lab.py`, add to `Lab`:

```python
    def resolve_handle(self, handle: str) -> "Host | None":
        """Resolve a typed CLI handle to a host: exact canonical id wins, else
        the positional ``<element-slug><N>`` form (N-th host of that element by
        logical index), else ``None``.
        """
        host = self.hosts.get(handle)
        if host is not None:
            return host
        import re

        from ..host.remote_host import RemoteHost, slug

        m = re.fullmatch(r"(.*?)(\d+)", handle)
        if not m:
            return None
        prefix, number = m.group(1), int(m.group(2))
        for candidate in self.hosts.values():
            if (
                isinstance(candidate, RemoteHost)
                and candidate.logical_index == number
                and slug(candidate.element) == prefix
            ):
                return candidate
        return None
```

- [ ] **Step 4: Use it from `get_host`**

In `src/otto/context.py`, change the `get_host` lookup (lines 104-109) so a positional handle resolves before the `KeyError`:

```python
        host = self.lab.resolve_handle(host_id)
        if host is None:
            raise KeyError(
                f"No host {host_id!r} in lab {self.lab.name!r}. Available: {sorted(self.lab.hosts)}"
            )
```

(Replace the `try/except KeyError` block that did `self.lab.hosts[host_id]`.)

- [ ] **Step 5: Add the shadow warning**

Extend `Lab._assign_logical_indices` (Task 4) so that, after stamping a group, it warns when a canonical id `<element-slug><N>` exists but points at a host that is NOT that group's N-th by logical index. Add, inside the `len(members) >= 2` branch after the enumerate loop:

```python
            by_position = {h.logical_index: h for h in ordered}
            for host in ordered:
                if host.element_id is None:
                    continue
                shadowed = self.hosts.get(f"{slug(host.element)}{host.element_id}")
                positional = by_position.get(host.element_id)
                if shadowed is not None and positional is not None and shadowed is not positional:
                    getLogger("otto").warning(
                        "Host id %r shadows the display label of %r (logical %d): "
                        "typing %r reaches the id-%d host, not the labelled one.",
                        shadowed.id, positional.name, host.element_id,
                        shadowed.id, host.element_id,
                    )
```

(`getLogger` is already imported in `lab.py`.)

- [ ] **Step 6: Write the shadow-warning test**

Append to `tests/unit/configmodule/test_handle_resolution.py`:

```python
def test_shadow_warning_fires_on_mixed_set(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="otto"):
        _lab(("server", 2, "10.0.0.1"), ("server", 5, "10.0.0.2"))
    assert any("shadows" in r.message for r in caplog.records)


def test_no_shadow_warning_for_large_ids(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="otto"):
        _lab(("server", 47, "10.0.0.1"), ("server", 103, "10.0.0.2"))
    assert not any("shadows" in r.message for r in caplog.records)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/configmodule/test_handle_resolution.py tests/unit/test_context.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/otto/configmodule/lab.py src/otto/context.py tests/unit/configmodule/test_handle_resolution.py
git commit -m "feat(cli): resolve positional element-slug handles; warn on canonical/logical shadow"
```

---

### Task 8: Shared logical-index helper + completion logical handles

**Context — why a shared helper.** `collect_host_ids` builds host objects individually via `create_host_from_dict` and never assembles a `Lab` or runs `_assign_logical_indices`, so those hosts have `logical_index = None`. To offer logical handles in completion that MATCH what `resolve_handle` resolves at runtime, both must derive positions from ONE definition. This task extracts a pure `logical_indices` helper, has `Lab._assign_logical_indices` delegate to it (Task 4/7 tests are the regression net proving behavior is unchanged), and has completion use it.

**Files:**
- Modify: `src/otto/configmodule/lab.py` (add module-level `logical_indices`; refactor `_assign_logical_indices` to delegate)
- Modify: `src/otto/configmodule/completion_cache.py` (`collect_host_ids` + `collect_host_ids_by_lab` emit positional handles)
- Test: `tests/unit/configmodule/test_logical_indices_helper.py` (new); `tests/unit/configmodule/test_completion_logical_handles.py` (new)

**Interfaces:**
- Produces: `logical_indices(hosts: Iterable[Any]) -> dict[str, int]` — host id → 1-based position within its `slug(element)` group, ordered by `element_id` ascending (`id` tie-break), only for groups with >1 member. Duck-typed on `.element`/`.element_id`/`.id`; non-`RemoteHost`/empty-`element` hosts skipped.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/configmodule/test_logical_indices_helper.py`:

```python
"""logical_indices() is the single source for positions; Lab stamps must agree."""

from otto.configmodule.lab import Lab, logical_indices
from otto.host.unix_host import UnixHost


def _h(element, element_id=None, ip="10.0.0.1"):
    return UnixHost(ip=ip, element=element, element_id=element_id, creds=[])


def test_unique_element_absent_from_map():
    assert logical_indices([_h("server")]) == {}


def test_repeated_numbered_by_element_id_ascending():
    a = _h("server", 103, "10.0.0.1")
    b = _h("server", 47, "10.0.0.2")
    c = _h("server", 288, "10.0.0.3")
    assert logical_indices([a, b, c]) == {"server47": 1, "server103": 2, "server288": 3}


def test_slug_grouping_case_insensitive():
    a = _h("Server", 1, "10.0.0.1")
    b = _h("server", 2, "10.0.0.2")
    assert logical_indices([a, b]) == {"server1": 1, "server2": 2}


def test_stamps_agree_with_helper():
    # The Lab assembly pass must stamp exactly what the helper computes.
    lab = Lab(name="t")
    lab.add_host(_h("server", 47, "10.0.0.1"))
    lab.add_host(_h("server", 103, "10.0.0.2"))
    lab.add_host(_h("router", ip="10.0.0.3"))  # unique -> None
    lab._assign_logical_indices()
    stamped = {h.id: h.logical_index for h in lab.hosts.values() if h.logical_index is not None}
    assert stamped == logical_indices(lab.hosts.values())
```

Create `tests/unit/configmodule/test_completion_logical_handles.py`:

```python
"""Completion enumerates canonical ids AND positional logical handles."""

import json

from otto.configmodule.completion_cache import collect_host_ids


def _repo(tmp_path):
    # Minimal repo layout: a labs/ dir with one lab.json holding two servers.
    from otto.configmodule.repo import Repo  # adjust import to the actual Repo type

    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    (labs_dir / "lab.json").write_text(
        json.dumps(
            {
                "hosts": [
                    {"ip": "10.0.0.1", "element": "server", "element_id": 47, "labs": ["east"]},
                    {"ip": "10.0.0.2", "element": "server", "element_id": 103, "labs": ["east"]},
                ]
            }
        )
    )
    return Repo(labs=[labs_dir])  # adjust construction to the actual Repo API


def test_collect_host_ids_includes_logical_handles(tmp_path):
    ids = set(collect_host_ids([_repo(tmp_path)]))
    assert {"server47", "server103"} <= ids       # canonical
    assert {"server1", "server2"} <= ids           # logical handles
```

Note: adapt the `Repo` import/construction in `_repo` to the actual type used by the existing completion tests (see `tests/unit/configmodule/` for the pattern — `collect_host_ids` takes `list[Repo]` and reads each repo's `labs` dirs for `lab.json`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/configmodule/test_logical_indices_helper.py tests/unit/configmodule/test_completion_logical_handles.py -v`
Expected: FAIL — `ImportError: cannot import name 'logical_indices'` / missing logical handles.

- [ ] **Step 3: Add `logical_indices` and refactor `_assign_logical_indices` to delegate**

In `src/otto/configmodule/lab.py`, add a module-level function (near `_refresh_name`). Add `from collections.abc import Iterable` under the existing `TYPE_CHECKING` block for the annotation:

```python
def logical_indices(hosts: "Iterable[Any]") -> dict[str, int]:
    """Host id -> 1-based logical index within its ``slug(element)`` group.

    Ordered by ``element_id`` ascending (``id`` tie-break); only groups with more
    than one member are numbered (a unique element is absent from the map).
    Duck-typed on ``element``/``element_id``/``id``; non-``RemoteHost`` or
    empty-``element`` hosts are skipped. THE single source of truth for logical
    positions, shared by ``Lab._assign_logical_indices`` (stamping) and completion
    (handles), so the CLI's positional handles always match ``resolve_handle``.
    """
    from collections import defaultdict

    from ..host.remote_host import RemoteHost, slug

    groups: "dict[str, list[Any]]" = defaultdict(list)
    for host in hosts:
        if isinstance(host, RemoteHost) and host.element:
            groups[slug(host.element)].append(host)
    positions: dict[str, int] = {}
    for members in groups.values():
        if len(members) < 2:  # noqa: PLR2004 — a group of 1 is "unique", not numbered
            continue
        ordered = sorted(members, key=lambda h: (h.element_id is None, h.element_id or 0, h.id))
        for pos, host in enumerate(ordered, start=1):
            positions[host.id] = pos
    return positions
```

Replace the body of `Lab._assign_logical_indices` (keep its docstring) so it delegates to `logical_indices`, stamps + refreshes names, and preserves the shadow warning via a reverse `(group, position) -> host` map:

```python
    def _assign_logical_indices(self) -> None:
        """Stamp each host's ``logical_index`` within its element-slug group.

        Delegates grouping/ordering to :func:`logical_indices` (the single source
        shared with completion), refreshes non-overridden display names, and warns
        when a canonical id shadows a different host's logical position. Idempotent.
        """
        from ..host.remote_host import RemoteHost, slug

        positions = logical_indices(self.hosts.values())
        by_group_pos: "dict[tuple[str, int], RemoteHost]" = {}
        for host in self.hosts.values():
            if not (isinstance(host, RemoteHost) and host.element):
                continue
            host.logical_index = positions.get(host.id)
            _refresh_name(host)
            if host.logical_index is not None:
                by_group_pos[(slug(host.element), host.logical_index)] = host
        # Shadow warning: a canonical id <element-slug><element_id> that resolves to
        # a DIFFERENT host than that group's element_id-th by logical index means
        # "type what you see" would reach the wrong host (only possible for a small
        # element_id colliding with a logical position — see the spec's {2,5} case).
        for host in self.hosts.values():
            if not (
                isinstance(host, RemoteHost)
                and host.logical_index is not None
                and host.element_id is not None
            ):
                continue
            key = slug(host.element)
            shadowed = self.hosts.get(f"{key}{host.element_id}")
            positional = by_group_pos.get((key, host.element_id))
            if shadowed is not None and positional is not None and shadowed is not positional:
                getLogger("otto").warning(
                    "Host id %r shadows the display label of %r (logical %d): "
                    "typing %r reaches the id-%d host, not the labelled one.",
                    shadowed.id,
                    positional.name,
                    host.element_id,
                    shadowed.id,
                    host.element_id,
                )
```

- [ ] **Step 4: Verify Task 4 & Task 7 behavior is unchanged**

Run: `uv run pytest tests/unit/configmodule/test_logical_index.py tests/unit/configmodule/test_handle_resolution.py tests/unit/configmodule/test_logical_indices_helper.py -v`
Expected: PASS — the delegation preserves stamping AND the shadow fire/no-fire behavior; the new consistency test passes.

- [ ] **Step 5: Emit logical handles in completion**

In `src/otto/configmodule/completion_cache.py`, import the helper (`from .lab import logical_indices`) and `slug` (`from ..host.remote_host import slug`). In `collect_host_ids`, additionally accumulate the built `host` objects into a dict keyed by id (dedup) alongside the existing `ids.add(host.id)` — e.g. `built[host.id] = host` where `host = create_host_from_dict(host_data)`. After the collection loops, before the final `return`, fold in the logical handles:

```python
    positions = logical_indices(built.values())
    for host in built.values():
        pos = positions.get(host.id)
        if pos is not None:
            ids.add(f"{slug(host.element)}{pos}")
```

Apply the SAME pattern in `collect_host_ids_by_lab` per-lab bucket (compute `logical_indices` over each lab's built hosts and add its handles to that lab's id list), so `otto host <TAB>` offers logical handles both with and without a selected lab. Keep the built-in-host seeding, container-id synthesis, and lab filtering intact; the handles are added, not substituted.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/configmodule/test_completion_logical_handles.py tests/unit/configmodule/ -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/otto/configmodule/lab.py src/otto/configmodule/completion_cache.py tests/unit/configmodule/test_logical_indices_helper.py tests/unit/configmodule/test_completion_logical_handles.py
git commit -m "feat(completion): shared logical_indices helper; offer positional handles alongside canonical ids"
```

---

### Task 9: Documentation

**Files:**
- Modify: `docs/guide/lab-config.md` (host identity & naming section)
- Test: `make docs` (Sphinx `-W`)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add the host-identity documentation**

In `docs/guide/lab-config.md`, add a "Host identity & naming" subsection to the hosts documentation covering, with examples:

- `element` is the human-readable string and the id source: it is slugged (lower-case, spaces/punctuation → hyphens) into the canonical id — `"Lab X Server"` → `lab-x-server`. There is no separate id field; **renaming `element` changes the host's id** (and any declared-link route over it).
- Disambiguating repeats: give distinct `element` strings, an `element_id`, or `board`/`slot`. Duplicate ids fail loud at load.
- Display name (`host.name`): space-joined, original-case `element [logical number] [board] [slot]`; the small logical number appears only when the element repeats; an explicit `name` overrides it.
- CLI handles: type the id (slug) or the positional `<element><N>` form; tab-complete offers both.

Use a concrete `lab.json` snippet:

```json
{
  "hosts": [
    { "ip": "10.0.0.2", "element": "Lab X Server" },
    { "ip": "10.0.0.3", "element": "dut", "element_id": 47 },
    { "ip": "10.0.0.4", "element": "dut", "element_id": 103 }
  ]
}
```

with a table showing each host's resulting id, display name, and CLI handles
(`lab-x-server`; `dut47`/`dut103` with labels `dut 1`/`dut 2` and handles
`dut1`/`dut2`).

- [ ] **Step 2: Build the docs**

Run: `make docs`
Expected: exit 0, no Sphinx `-W` warnings.

- [ ] **Step 3: Commit**

```bash
git add docs/guide/lab-config.md
git commit -m "docs(guide): document host identity (element-slug id), display name, and CLI handles"
```

---

## Final gate (after all tasks)

- [ ] `nox -s lint typecheck` → success
- [ ] `make coverage` → all pass, coverage not regressed
- [ ] `make docs` → no `-W` warnings
- [ ] Whole-branch review on **fable** (per spec §9); dispatch fix subagents for Critical/Important findings; re-review.
- [ ] `superpowers:finishing-a-development-branch`.
