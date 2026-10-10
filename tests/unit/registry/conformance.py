"""The five conformance case kinds, as reusable assertion functions.

A seam's case module (``tests/unit/registry/cases/test_case_<seam>.py``) calls
the functions that fit its tables, with record factories for its own entry
types, and lists the tables it covers in a module-level ``COVERS``.
``test_conformance.py`` discovers every case module and asserts that every
live otto table is covered.

Every function leaves the table as it found it (a snapshot and a restore
around the assertions), and registers from synthetic modules, so attribution
is what the engine captures, not what a caller supplied.
"""

import contextlib
import copy
import dataclasses
import sys
from collections.abc import Callable
from typing import Any

import pytest

from otto import registry as reg
from otto.registry import DuplicateRegistration, IncompleteRegistration, Ref, RegistrationRefused
from tests._fixtures.registrant import from_module

USER = "conformance_user.init"
"""The synthetic user module every case registers from."""


def with_a_list(record: Any) -> Any:
    """A copy of *record* whose first field holds a ``list``."""
    bad = copy.copy(record)
    first = next(iter(_field_names(record)))
    object.__setattr__(bad, first, [getattr(record, first)])
    return bad


def _field_names(record: Any) -> list[str]:
    if dataclasses.is_dataclass(record):
        return [f.name for f in dataclasses.fields(record)]
    return list(type(record).model_fields)


class _Restored:
    """Snapshot *table* on entry, restore it on exit, whatever the assertions did."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def __enter__(self) -> None:
        self.state = self.table._snapshot()

    def __exit__(self, *exc: object) -> None:
        self.table._restore(self.state)


def assert_raw_registry(
    table: Any,
    *,
    make: Callable[[int], tuple[str, Any]],
    invalid: tuple[str, Any] | None = None,
    lazy: tuple[str, Any] | None = None,
    wrongly_typed_lazy: tuple[str, Any] | None = None,
    require_repo: str | None = None,
) -> None:
    """The raw-registry case: duplicates, overwrite, records, imports, refusal, atomicity.

    *make(i)* returns ``(name, record)``: a valid record, distinct for each
    *i*, and the name the seam needs it under (a class seam's name is its
    class's ``type_name``, so every *i* may share one name). *invalid* is a
    ``(name, record)`` the table's ``validate`` rejects (with a ``ValueError``,
    or a ``TypeError`` for a value of the wrong type). *lazy* is a
    ``(name, record)`` holding a ``Ref`` to a module not yet imported, which
    resolves cleanly; *wrongly_typed_lazy* one whose ``Ref`` resolves to a
    value ``check_resolved`` rejects. *require_repo* names the repo to
    register under for a ``RequireRepo`` table.
    """
    with _Restored(table):
        _assert_raw_registry(table, make, invalid, lazy, wrongly_typed_lazy, require_repo)


def _assert_raw_registry(table, make, invalid, lazy, wrongly_typed_lazy, require_repo) -> None:
    repo = reg.registering_repo(require_repo) if require_repo else contextlib.nullcontext()
    with repo:
        name, first = make(1)
        assert name not in table, f"{name!r} is already registered; pick a name the case owns"
        from_module(USER, table.register, name, first)
        names = table.names()
        assert table.peek(name) == first

        # A duplicate raises and changes nothing.
        revision = table.revision
        with pytest.raises(DuplicateRegistration):
            from_module("other_user.init", table.register, name, make(2)[1])
        assert (table.names(), table.peek(name), table.origin(name), table.revision) == (
            names,
            first,
            USER,
            revision,
        )

        # overwrite=True replaces completely, in place, with new attribution.
        generation = table._generation(name)
        replacement = make(3)[1]
        from_module("other_user.init", table.register, name, replacement, overwrite=True)
        assert table.names() == names
        assert table.peek(name) == replacement
        assert table.origin(name) == "other_user.init"
        assert table.revision == revision + 1
        assert table._generation(name) != generation

        # An invalid eager overwrite leaves the old entry.
        if invalid is not None:
            bad_name, bad_record = invalid
            with pytest.raises((ValueError, TypeError)):  # the seam's own validate error
                from_module(USER, table.register, bad_name, bad_record, overwrite=True)
            assert table.peek(name) == replacement
            assert table.names() == names

        # Incomplete registrations (the record check runs before any name check).
        record_type = type(first)
        for bad in (record_type, {"x": 1}, Ref("json:dumps"), object(), with_a_list(make(6)[1])):
            with pytest.raises(IncompleteRegistration):
                from_module(USER, table.register, "__c_bad", bad)
            assert "__c_bad" not in table

        # Batches are atomic: a failing last item, or a repeated key, commits nothing.
        revision = table.revision
        with pytest.raises(IncompleteRegistration):
            from_module(
                USER,
                table.register_many,
                [(name, make(7)[1]), ("__c_e", record_type)],
                overwrite=True,
            )
        with pytest.raises(DuplicateRegistration):
            from_module(
                USER, table.register_many, [(name, make(7)[1]), (name, make(8)[1])], overwrite=True
            )
        assert table.peek(name) == replacement and table.revision == revision  # noqa: PT018

        # A check may not write any table.
        with reg._checking(), pytest.raises(RegistrationRefused):
            from_module(USER, table.register, name, make(9)[1], overwrite=True)
        assert table.peek(name) == replacement

        if lazy is not None:
            lazy_name, record = lazy
            module = _ref_module(record)
            assert module not in sys.modules, f"{module} is imported before the case runs"
            if lazy_name in table:
                table.unregister(lazy_name)
            from_module(USER, table.register, lazy_name, record)
            table.names()
            assert lazy_name in table
            table.origin(lazy_name)
            table.peek(lazy_name)
            table._snapshot()
            with pytest.raises(DuplicateRegistration):
                from_module(USER, table.register, lazy_name, record)
            assert module not in sys.modules, "a listing, collision or snapshot imported a Ref"
            revision, generation = table.revision, table._generation(lazy_name)
            table.get(lazy_name)
            assert (table.revision, table._generation(lazy_name)) == (revision, generation)

        if wrongly_typed_lazy is not None:
            wrong_name, record = wrongly_typed_lazy
            if wrong_name in table:
                table.unregister(wrong_name)
            from_module(USER, table.register, wrong_name, record)
            for _ in range(2):  # it raises again: nothing was cached, the retry re-checks
                with pytest.raises(Exception):  # noqa: B017, PT011 — the seam's check_resolved error
                    table.get(wrong_name)
                assert any(isinstance(v, Ref) for v in _values(table.peek(wrong_name)))

    # Under test loading the refusal fires before validate and changes nothing.
    revision = table.revision
    held = table.names()
    with reg.loading_test_files(), pytest.raises(RegistrationRefused):
        from_module("test_conformance_probe", table.register, name, make(10)[1], overwrite=True)
    assert table.names() == held and table.revision == revision  # noqa: PT018

    # Teardown unregister and restore succeed outside any init import.
    victim = held[-1]
    state = table._snapshot()
    table.unregister(victim)
    table._restore(state)
    assert victim in table


def _values(record: Any) -> list[Any]:
    return [getattr(record, name) for name in _field_names(record)]


def _ref_module(record: Any) -> str:
    ref = next(v for v in _values(record) if isinstance(v, Ref))
    return ref.target.partition(":")[0]


def assert_wrapper_matches_raw(
    table: Any,
    *,
    via_wrapper: Callable[..., None],
    record_for: Callable[[], Any],
    decorator_module: str | None = None,
) -> None:
    """The wrapper case: the wrapper and the raw path agree on record, attribution, collisions.

    *via_wrapper(overwrite=...)* registers one payload through the public wrapper,
    under the one name the seam gives it; *record_for()* is the record the raw
    ``.register()`` needs for that payload. *decorator_module* is the module a
    decorator wrapper must credit (default: the calling module).

    Pass the wrapper with its payload bound, as
    ``functools.partial(register_x, name, payload)``: a partial adds no
    Python frame, so the frame walk goes from the marked wrapper straight to
    the case's synthetic caller, and what is checked is the real wrapper's
    attribution. A ``lambda`` is a frame of the case module, an unmarked
    helper around the wrapper, and the engine rightly credits it instead.
    """
    user = decorator_module or USER
    with _Restored(table):
        before = set(table.names())
        from_module(user, via_wrapper, overwrite=False)
        added = [n for n in table.names() if n not in before]
        assert len(added) == 1, f"the wrapper registered {added}, not one new name"
        name = added[0]
        assert table.peek(name) == record_for()
        assert table.origin(name) == user
        with pytest.raises(DuplicateRegistration):
            from_module(user, via_wrapper, overwrite=False)
        with pytest.raises(DuplicateRegistration):
            from_module(user, table.register, name, record_for())
        table.unregister(name)
        from_module(user, table.register, name, record_for())
        assert table.peek(name) == record_for()
        assert table.origin(name) == user
        revision = table.revision
        from_module("other_user.init", via_wrapper, overwrite=True)
        assert table.origin(name) == "other_user.init"
        assert table.revision == revision + 1


def assert_backend_registry(
    table: Any,
    *,
    env: Any,
    make_spy_entry: Callable[[], tuple[Any, Any]],
    good_raw: dict[str, object],
    bad_raw: dict[str, object],
) -> None:
    """The backend case: names do not matter; prepare parses once; build constructs once.

    *make_spy_entry()* returns ``(entry, spy)``, where ``spy.parses`` and
    ``spy.builds`` count the config model's parses and the factory's (or a
    class backend's ``create``) calls. A class backend has no configuration,
    so it parses nothing. *bad_raw* must fail to parse, and none of its values
    may appear in the error.
    """
    with _Restored(table):
        for name in ("json", "none", "__b_other"):
            entry, spy = make_spy_entry()
            parses = 0 if entry.config is None else 1
            from_module(USER, table.register, name, entry, overwrite=True)
            prepared = table.prepare(name, good_raw, env)
            assert (spy.parses, spy.builds) == (parses, 0), name
            table.build(prepared)
            table.build(prepared)
            assert (spy.parses, spy.builds) == (parses, 2), name

            with pytest.raises(Exception) as err:  # noqa: PT011 — the seam's construction error
                table.prepare(name, bad_raw, env)
            for value in bad_raw.values():
                assert str(value) not in str(err.value), (name, value)

            from_module(USER, table.register, name, make_spy_entry()[0], overwrite=True)
            builds = spy.builds
            with pytest.raises(Exception, match="stale preparation"):
                table.build(prepared)
            assert spy.builds == builds


def assert_subscription(sub: Any, *, value_a: Any, value_b: Any) -> None:
    """The subscription case: order, repeats, cancel-once, attribution, refusal."""
    with _Restored(sub):
        start = len(sub.items())
        token = from_module(USER, sub.subscribe, value_a)
        with reg.registering_repo("conformance_repo"):
            from_module(USER, sub.subscribe, value_b)
        from_module(USER, sub.subscribe, value_a)
        added = sub.items()[start:]
        assert [s.value for s in added] == [value_a, value_b, value_a]
        assert [(s.origin, s.repo) for s in added] == [
            (USER, None),
            (USER, "conformance_repo"),
            (USER, None),
        ]
        token.cancel()
        token.cancel()
        assert [s.value for s in sub.items()[start:]] == [value_b, value_a]
        with reg.loading_test_files(), pytest.raises(RegistrationRefused):
            from_module("test_conformance_probe", sub.subscribe, value_a)
        assert len(sub.items()) == start + 2


def assert_view(
    view: Any,
    *,
    sources: list[Any],
    contribute: Callable[[str], None],
    clash: Callable[[], None],
    refusal: "type[BaseException]" = ValueError,
) -> None:
    """The view case: derivation follows its sources; no mutation; a rejected clash changes nothing.

    *contribute(name)* registers one contribution under *name* in a source;
    *clash()* attempts a contribution that a source's ``validate`` rejects,
    with *refusal*, the seam's own error type.
    """
    states = [(source, source._snapshot()) for source in sources]
    try:
        for method in ("register", "register_many", "unregister"):
            assert not hasattr(view, method), method
        token = view.revision
        contribute("__v_one")
        assert view.revision != token
        assert "__v_one" in view and view.get("__v_one") is not None  # noqa: PT018
        before = ([s._snapshot() for s in sources], view.items())
        with pytest.raises(refusal):
            clash()
        assert ([s._snapshot() for s in sources], view.items()) == before
    finally:
        for source, state in states:
            source._restore(state)
