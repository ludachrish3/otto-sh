"""The engine's one step matrix, one test per cell and per rule.

Each test names the mutation of ``otto.registry`` that must turn it red. The
first test pins the matrix literally; every other test pins one cell or rule
behaviourally, so a mutation applied to ``_STEPS`` and ``SPEC_TABLE`` alike is
still caught.
"""

import sys
import types
from dataclasses import dataclass

import pytest

from otto import registry as reg
from otto.registry import (
    _STEPS,
    DuplicateRegistration,
    IncompleteRegistration,
    Justified,
    Ref,
    RegistrationRefused,
    Registry,
    RequireRepo,
    _Op,
    _Step,
)


@dataclass(frozen=True)
class Rec:
    value: object


@dataclass(frozen=True)
class LazyRec:
    value: "object | Ref"


def _reg(**kw) -> "Registry[Rec]":
    return Registry("widget", entry=Rec, register_hint="register_widget()", **kw)


def _lazy(**kw) -> "Registry[LazyRec]":
    kw.setdefault("check_resolved", lambda name, entry: None)
    return Registry("widget", entry=LazyRec, register_hint="register_widget()", **kw)


def _module(monkeypatch, name: str, source: str, tmp_path) -> None:
    (tmp_path / f"{name}.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)


def _from_module(module: str, fn, *args, **kwargs):
    """Call *fn* from a frame whose module is *module* (attribution is by frame)."""
    code = compile("result = fn(*args, **kwargs)", f"<{module}>", "exec")
    scope = {"__name__": module, "fn": fn, "args": args, "kwargs": kwargs}
    exec(code, scope)  # noqa: S102 — a synthetic caller module, the attribution under test
    return scope["result"]


def _home(monkeypatch, name: str, **values) -> None:
    """Expose *values* to a generated module as ``import <name>``."""
    monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**values))


S = _Step
SPEC_TABLE = {
    _Op.REGISTER: {
        S.REFUSE_DURING_TEST_LOAD,
        S.NESTED_WRITE_GUARD,
        S.CAPABILITIES,
        S.RECORD,
        S.COLLISIONS,
        S.VALIDATE,
        S.BUMP_REVISION,
        S.BUMP_GENERATION,
    },
    _Op.UNREGISTER: {S.NESTED_WRITE_GUARD, S.BUMP_REVISION, S.BUMP_GENERATION},
    _Op.RESTORE: {S.BUMP_REVISION, S.BUMP_GENERATION},
    _Op.PUBLISH: {S.RECORD, S.CHECK_RESOLVED},
    _Op.SUBSCRIBE: {S.REFUSE_DURING_TEST_LOAD, S.NESTED_WRITE_GUARD, S.BUMP_REVISION},
    _Op.CANCEL: {S.NESTED_WRITE_GUARD, S.BUMP_REVISION},
}


def test_the_step_matrix_is_the_spec_table():
    """The spec's table, transcribed. Mutation: drop any one step from any one _STEPS row."""
    assert {op: set(steps) for op, steps in _STEPS.items()} == SPEC_TABLE
    assert list(_Step) == sorted(_Step, key=lambda s: s.value)  # execution order is the table's


# --- step 2: test-load refusal ---------------------------------------------


def test_register_refuses_a_user_module_while_test_files_load():
    """Mutation: remove REFUSE_DURING_TEST_LOAD from _STEPS[_Op.REGISTER]."""
    r = _reg()
    with reg.loading_test_files(), pytest.raises(RegistrationRefused, match="init module"):
        _from_module("test_widgets", r.register, "w", Rec(1))
    assert "w" not in r
    assert r.revision == 0


def test_the_refusal_runs_before_validate():
    """Mutation: move VALIDATE before REFUSE_DURING_TEST_LOAD in _Step order."""

    def validate(name, entry, proposed):
        raise AssertionError("validate ran before the refusal")

    r = _reg(validate=validate)
    with reg.loading_test_files(), pytest.raises(RegistrationRefused):
        _from_module("test_widgets", r.register, "w", Rec(1))


def test_unregister_and_restore_and_cancel_skip_the_refusal():
    """Mutations: add REFUSE_DURING_TEST_LOAD to UNREGISTER, RESTORE or CANCEL (one at a time)."""
    r = _reg()
    _from_module("repo_init", r.register, "w", Rec(1))
    state = r._snapshot()
    _from_module("repo_init", r.register, "x", Rec(2))
    sub = reg.Subscription("hook", register_hint="x")
    token = _from_module("repo_init", sub.subscribe, len)
    with reg.loading_test_files():
        _from_module("test_widgets", r.unregister, "x")
        r._restore(state)
        _from_module("test_widgets", token.cancel)
    assert r.names() == ["w"]
    assert sub.items() == []


def test_publish_skips_the_refusal(monkeypatch, tmp_path):
    """A Ref's first get inside a pytest session publishes. Mutation: add REFUSE to PUBLISH."""
    _module(monkeypatch, "late_value_mod", "VALUE = 7\n", tmp_path)
    r = _lazy()
    _from_module("repo_init", r.register, "w", LazyRec(Ref("late_value_mod:VALUE")))
    with reg.loading_test_files():
        assert r.get("w").value == 7
    assert r.peek("w").value == 7  # published


# --- step 3: nested-write guard -------------------------------------------


@pytest.mark.parametrize(
    "op", ["register", "unregister", "unregister_unknown", "subscribe", "cancel"]
)
def test_a_check_cannot_write_any_table(op):
    """A refused nested write raises RegistrationRefused, before any other check.

    Mutation: remove NESTED_WRITE_GUARD; or look the name up before the guard
    (an unknown name inside a check then raises ValueError, not the refusal).
    """
    other = _reg()
    other.register("pre", Rec(0))
    sub = reg.Subscription("hook", register_hint="x")
    token = sub.subscribe(len)
    writes = {
        "register": lambda: other.register("w2", Rec(2)),
        "unregister": lambda: other.unregister("pre"),
        "unregister_unknown": lambda: other.unregister("nope"),
        "subscribe": lambda: sub.subscribe(abs),
        "cancel": token.cancel,
    }

    def validate(name, entry, proposed):
        writes[op]()

    r = _reg(validate=validate)
    with pytest.raises(RegistrationRefused, match="while a registry check runs"):
        r.register("w", Rec(1))
    assert "w" not in r and "pre" in other and len(sub.items()) == 1  # noqa: PT018


def test_a_check_may_read_and_publish(monkeypatch, tmp_path):
    """Reads stay allowed, including a get that publishes.

    Mutation: add NESTED_WRITE_GUARD to PUBLISH.
    """
    _module(monkeypatch, "pub_mod", "VALUE = 3\n", tmp_path)
    source = _lazy()
    source.register("s", LazyRec(Ref("pub_mod:VALUE")))
    r = _reg(validate=lambda name, entry, proposed: source.get("s"))
    r.register("w", Rec(1))
    assert source.peek("s").value == 3


def test_a_ref_that_registers_at_import_is_refused_inside_a_check_and_retried(
    monkeypatch, tmp_path
):
    """Refused, nothing cached, the next get retries. Not special-cased."""
    target = _reg()
    _home(monkeypatch, "_nested_home", TARGET=target, Rec=Rec)
    _module(
        monkeypatch,
        "registers_on_import",
        "import _nested_home\n"
        "_nested_home.TARGET.register('side', _nested_home.Rec(9))\n"
        "VALUE = 5\n",
        tmp_path,
    )
    source = _lazy()
    source.register("s", LazyRec(Ref("registers_on_import:VALUE")))
    r = _reg(validate=lambda name, entry, proposed: source.get("s"))
    with pytest.raises(RegistrationRefused):
        r.register("w", Rec(1))
    assert isinstance(source.peek("s").value, Ref)  # nothing cached
    monkeypatch.delitem(sys.modules, "registers_on_import", raising=False)
    assert source.get("s").value == 5  # retried outside the check
    assert target.origin("side") == "registers_on_import"


# --- step 4: capabilities --------------------------------------------------


def test_require_repo_refuses_register_outside_an_init_import():
    """Mutation: remove CAPABILITIES from REGISTER."""
    r = _reg(capabilities=[Justified(RequireRepo(), reason="r")])
    with pytest.raises(RegistrationRefused, match="repo init module"):
        r.register("w", Rec(1))
    assert "w" not in r
    assert r.revision == 0
    with reg.registering_repo("a"):
        r.register("w", Rec(1))
    assert r.repo("w") == "a"


def test_require_repo_never_blocks_unregister_restore_or_publish(monkeypatch, tmp_path):
    """Mutations: add CAPABILITIES to UNREGISTER / RESTORE / PUBLISH (one at a time)."""
    _module(monkeypatch, "require_repo_mod", "VALUE = 4\n", tmp_path)
    r = _lazy(capabilities=[Justified(RequireRepo(), reason="r")])
    with reg.registering_repo("a"):
        r.register("w", LazyRec(Ref("require_repo_mod:VALUE")))
        state = r._snapshot()
        r.register("x", LazyRec(2))
    r.unregister("x")
    r._restore(state)
    assert r.get("w").value == 4
    assert r.names() == ["w"]


# --- step 5: record type and freeze ----------------------------------------


def test_publish_freezes_only_resolved_fields(monkeypatch, tmp_path):
    """Mutation: remove RECORD from PUBLISH."""
    _module(monkeypatch, "list_mod", "LIST = [1]\n", tmp_path)
    r = _lazy()
    r.register("w", LazyRec(Ref("list_mod:LIST")))
    with pytest.raises(IncompleteRegistration, match="list"):
        r.get("w")
    assert isinstance(r.peek("w").value, Ref)


def test_restore_does_not_check_records():
    """Mutation: add RECORD to RESTORE."""
    r = _reg()
    r.register("w", Rec(1))
    state = r._snapshot()
    object.__setattr__(r.peek("w"), "value", [1])  # a state the test changed
    r._restore(state)
    assert r.peek("w").value == [1]


# --- step 6: collisions ----------------------------------------------------


def test_collisions_raise_naming_both_origins():
    """Mutation: remove COLLISIONS from REGISTER."""
    r = _reg()
    _from_module("repo_a.init", r.register, "w", Rec(1))
    with pytest.raises(DuplicateRegistration) as err:
        _from_module("repo_b.init", r.register, "w", Rec(2))
    text = str(err.value)
    assert "repo_a.init" in text and "repo_b.init" in text  # noqa: PT018
    assert "Pass overwrite=True" in text
    assert r.get("w") == Rec(1) and r.origin("w") == "repo_a.init" and r.revision == 1  # noqa: PT018


def test_overwrite_never_covers_a_repeat_within_one_batch():
    """Mutation: delete the repeat check in the collision step."""
    r = _reg()
    with pytest.raises(DuplicateRegistration, match="repeated"):
        r.register_many([("w", Rec(1)), ("w", Rec(2))], overwrite=True)
    assert "w" not in r
    assert r.revision == 0


def test_subscribe_allows_repeats():
    """Mutation: add COLLISIONS to SUBSCRIBE."""
    sub = reg.Subscription("hook", register_hint="x")
    sub.subscribe(len)
    sub.subscribe(len)
    assert [s.value for s in sub.items()] == [len, len]


# --- step 7: checks --------------------------------------------------------


def test_validate_receives_the_proposed_table_with_the_batch_applied():
    """Mutation: build Proposed from the stored entries only."""
    seen = []
    r = _reg(validate=lambda name, entry, proposed: seen.append(dict(proposed)))
    r.register("a", Rec(1))
    seen.clear()
    r.register_many([("a", Rec(9)), ("b", Rec(2))], overwrite=True)
    assert seen == [{"a": Rec(9), "b": Rec(2)}] * 2


def test_validate_does_not_run_on_unregister_restore_or_publish(monkeypatch, tmp_path):
    """Mutation: add VALIDATE to UNREGISTER, RESTORE or PUBLISH."""
    _module(monkeypatch, "no_validate_mod", "VALUE = 6\n", tmp_path)
    armed = []

    def validate(name, entry, proposed):
        if armed:
            raise AssertionError("validate ran")

    r = _lazy(validate=validate)
    r.register("w", LazyRec(Ref("no_validate_mod:VALUE")))
    r.register("x", LazyRec(1))
    state = r._snapshot()
    armed.append(True)
    r.unregister("x")
    r._restore(state)
    assert r.get("w").value == 6


def test_check_resolved_runs_at_the_first_get_only(monkeypatch, tmp_path):
    """Mutations: add CHECK_RESOLVED to REGISTER, or drop it from PUBLISH."""
    _module(monkeypatch, "checked_mod", "VALUE = 8\n", tmp_path)
    calls = []
    r = _lazy(check_resolved=lambda name, entry: calls.append(entry))
    r.register("w", LazyRec(Ref("checked_mod:VALUE")))
    assert calls == []
    r.get("w")
    r.get("w")
    assert calls == [LazyRec(8)]


@dataclass(frozen=True)
class NestedRec:
    refs: "tuple[object, ...]"


def _nested(**kw) -> "Registry[NestedRec]":
    return Registry("widget", entry=NestedRec, register_hint="register_widget()", **kw)


def test_check_resolved_runs_once_for_a_record_whose_refs_are_all_nested():
    """The hook gets the record as stored, nested Refs opaque, exactly once.

    Mutation: return early from the first get when no top-level Ref exists.
    """
    calls = []
    r = _nested(check_resolved=lambda name, entry: calls.append(entry))
    record = NestedRec((Ref("never_imported_mod:X"),))
    r.register("w", record)
    assert calls == []
    assert r.get("w") is record
    assert r.get("w") is record
    assert calls == [record]
    assert "never_imported_mod" not in sys.modules
    assert r.revision == 1


def test_a_failed_check_of_a_nested_only_record_caches_nothing():
    """A failing hook raises at get and the next get runs it again.

    Mutation: mark the record checked before the hook runs.
    """
    calls = []

    def check(name, entry):
        calls.append(entry)
        if len(calls) == 1:
            raise ValueError("not yet")

    r = _nested(check_resolved=check)
    r.register("w", NestedRec((Ref("never_imported_mod:X"),)))
    with pytest.raises(ValueError, match="not yet"):
        r.get("w")
    r.get("w")
    r.get("w")
    assert len(calls) == 2


def test_a_restore_keeps_an_unchecked_nested_only_record_owing_its_check():
    """A snapshot taken before the first get restores a record whose check is still owed.

    Mutation: drop the owed-check flag from the snapshot row.
    """
    calls = []
    r = _nested(check_resolved=lambda name, entry: calls.append(entry))
    r.register("w", NestedRec((Ref("never_imported_mod:X"),)))
    state = r._snapshot()
    r._restore(state)
    r.get("w")
    assert len(calls) == 1


# --- steps 8 and 9: revision and generation --------------------------------


def test_register_bumps_revision_once_per_batch():
    """Mutation: bump per entry."""
    r = _reg()
    r.register_many([("a", Rec(1)), ("b", Rec(2)), ("c", Rec(3))])
    assert r.revision == 1


def test_an_empty_batch_commits_nothing():
    """An empty list or an exhausted generator neither commits nor bumps.

    Mutation: commit a REGISTER for an empty batch.
    """
    r = _reg()
    r.register_many([])
    exhausted = iter([("a", Rec(1))])
    list(exhausted)
    r.register_many(exhausted)
    assert r.revision == 0
    assert r.names() == []


def test_overwrite_replaces_completely_in_place():
    """Mutation: drop BUMP_GENERATION from REGISTER."""
    r = _reg()
    for name in ("a", "b", "c"):
        _from_module("x", r.register, name, Rec(name))
    generation = r._generation("b")
    revision = r.revision
    _from_module("y", r.register, "b", Rec("B"), overwrite=True)
    assert r.names() == ["a", "b", "c"]
    assert r.get("b") == Rec("B") and r.origin("b") == "y"  # noqa: PT018
    assert r.revision == revision + 1
    assert r._generation("b") != generation


def test_unregister_bumps_revision():
    """Mutation: drop BUMP_REVISION from UNREGISTER."""
    r = _reg()
    r.register("w", Rec(1))
    r.unregister("w")
    assert r.revision == 2


def test_restore_bumps_revision_and_generation():
    """Mutations: drop BUMP_REVISION or BUMP_GENERATION from RESTORE."""
    r = _reg()
    r.register("w", Rec(1))
    state = r._snapshot()
    g0 = r._generation("w")
    revision = r.revision
    r._restore(state)
    assert r.revision == revision + 1
    assert r._generation("w") != g0


def test_publish_bumps_neither_revision_nor_generation(monkeypatch, tmp_path):
    """Mutations: add BUMP_REVISION, or BUMP_GENERATION alone, to PUBLISH."""
    _module(monkeypatch, "quiet_mod", "VALUE = 2\n", tmp_path)
    r = _lazy()
    r.register("w", LazyRec(Ref("quiet_mod:VALUE")))
    rev, gen = r.revision, r._generation("w")
    assert r.get("w").value == 2
    assert (r.revision, r._generation("w")) == (rev, gen)


def test_subscribe_and_cancel_carry_no_generation():
    """Mutation: add BUMP_GENERATION to SUBSCRIBE or CANCEL: the write raises RuntimeError."""
    sub = reg.Subscription("hook", register_hint="x")
    token = sub.subscribe(len)
    assert sub.revision == 1
    token.cancel()
    assert sub.revision == 2


def test_publish_is_dropped_when_the_entry_changed_during_resolution(monkeypatch, tmp_path):
    """Mutation: publish without the generation check."""
    r = _lazy()
    _home(monkeypatch, "_replacing_home", TARGET=r, LazyRec=LazyRec)
    _module(
        monkeypatch,
        "replacing_mod",
        "import _replacing_home\n"
        "_replacing_home.TARGET.register('w', _replacing_home.LazyRec(1), overwrite=True)\n"
        "VALUE = 'old'\n",
        tmp_path,
    )
    r.register("w", LazyRec(Ref("replacing_mod:VALUE")))
    assert r.get("w") == LazyRec("old")
    assert r.peek("w") == LazyRec(1)  # the newer entry kept


def test_a_failed_resolution_after_a_replacement_keeps_the_replacement(monkeypatch, tmp_path):
    """Mutation: publish (or restore the old Ref entry) on a failed resolution."""
    r = _lazy()
    _home(monkeypatch, "_failing_home", TARGET=r, LazyRec=LazyRec)
    _module(
        monkeypatch,
        "failing_mod",
        "import _failing_home\n"
        "_failing_home.TARGET.register('w', _failing_home.LazyRec(1), overwrite=True)\n"
        "raise ImportError('broken after replacing')\n",
        tmp_path,
    )
    r.register("w", LazyRec(Ref("failing_mod:VALUE")))
    with pytest.raises(ImportError, match="broken after replacing"):
        r.get("w")
    assert r.peek("w") == LazyRec(1)
    assert r.get("w") == LazyRec(1)


def test_cancel_twice_removes_one_occurrence_and_bumps_once():
    """Mutation: make the second cancel bump, or remove the presence check."""
    sub = reg.Subscription("hook", register_hint="x")
    first = sub.subscribe(len)
    sub.subscribe(len)
    revision = sub.revision
    first.cancel()
    first.cancel()
    assert [s.value for s in sub.items()] == [len]
    assert sub.revision == revision + 1


# --- atomicity -------------------------------------------------------------


def test_a_failed_register_changes_nothing():
    """Mutation: write inside the loop before validating the rest."""

    def validate(name, entry, proposed):
        if name == "b":
            raise ValueError("bad b")

    r = _reg(validate=validate)
    _from_module("first", r.register, "pre", Rec(0))
    before = (r.names(), [r.origin(n) for n in r.names()], r.revision)
    with pytest.raises(ValueError, match="bad b"):
        r.register_many([("a", Rec(1)), ("b", Rec(2)), ("c", Rec(3))])
    assert (r.names(), [r.origin(n) for n in r.names()], r.revision) == before


def test_an_invalid_eager_overwrite_keeps_the_old_entry():
    """Mutation: write before validate."""

    def validate(name, entry, proposed):
        if entry.value == "bad":
            raise ValueError("rejected")

    r = _reg(validate=validate)
    r.register("w", Rec(1))
    with pytest.raises(ValueError, match="rejected"):
        r.register("w", Rec("bad"), overwrite=True)
    assert r.get("w") == Rec(1)


def test_a_ref_record_commits_with_its_ref_and_a_failed_get_caches_nothing(monkeypatch, tmp_path):
    """Mutation: cache the failed resolution."""
    monkeypatch.delitem(sys.modules, "missing_mod", raising=False)
    r = _lazy()
    r.register("w", LazyRec(Ref("missing_mod:X")))
    with pytest.raises(ModuleNotFoundError):
        r.get("w")
    assert isinstance(r.peek("w").value, Ref)
    _module(monkeypatch, "missing_mod", "X = 'now here'\n", tmp_path)
    assert r.get("w").value == "now here"


# --- reads -----------------------------------------------------------------


def test_listing_membership_origin_peek_raw_items_and_snapshot_never_import(monkeypatch):
    """Mutation: make peek call get."""
    monkeypatch.delitem(sys.modules, "never_imported_mod", raising=False)
    r = _lazy()
    r.register("w", LazyRec(Ref("never_imported_mod:X")))
    assert r.names() == ["w"]
    assert "w" in r and len(r) == 1  # noqa: PT018
    assert r.origin("w") == __name__ and r.repo("w") is None  # noqa: PT018
    assert isinstance(r.peek("w").value, Ref)
    assert [name for name, _ in r.raw_items()] == ["w"]
    r._snapshot()
    with pytest.raises(DuplicateRegistration):
        r.register("w", LazyRec(1))
    assert "never_imported_mod" not in sys.modules


def test_a_refs_import_credits_its_own_module(monkeypatch, tmp_path):
    """Mutation: attribute to the get caller."""
    other = _reg()
    _home(monkeypatch, "_crediting_home", OTHER=other, Rec=Rec)
    _module(
        monkeypatch,
        "crediting_mod",
        "import _crediting_home\n"
        "_crediting_home.OTHER.register('side', _crediting_home.Rec(1))\n"
        "VALUE = 1\n",
        tmp_path,
    )
    r = _lazy()
    r.register("w", LazyRec(Ref("crediting_mod:VALUE")))
    _from_module("the_reader", r.get, "w")
    assert other.origin("side") == "crediting_mod"
