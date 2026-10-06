"""Judge two API dumps by the dump spec's compatibility rules.

Spec: ``docs/superpowers/specs/2026-10-05-api-dump-design.md`` §4. A finding is
``"<where>: <reason>"`` and is BREAKING: it needs ``!`` or ``BREAKING CHANGE:``.
Everything §4.3 lists as safe produces no finding. Shape rules apply only to
bindings present in both dumps; a new binding is an addition.

The call invariant (§4.1): every call the old signature accepted is still
accepted, and every positional argument lands in the same slot. A positional-only
parameter is identified by its position, never its name. Keyword interception
(``(**kw)`` -> ``(x=0, **kw)``) is allowed (D-6).
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import api_records  # noqa: E402 -- path set up above

_SLOT_KINDS = ("PO", "PK")
_PROPERTY_PARTS = ("getter", "setter", "deleter")


def _defaults(
    where: str, label: str, old: api_records.Param, new: api_records.Param
) -> "list[str]":
    if old.default is None:
        return []
    if new.default is None:
        return [f"{where}: {label} lost its default"]
    if not api_records.values_equal(old.default, new.default):
        return [f"{where}: {label} default {old.default} -> {new.default}"]
    return []


def compare_params(
    where: str, old: "list[api_records.Param]", new: "list[api_records.Param]"
) -> "list[str]":
    """Return the §4.2 call findings between two parameter lists."""
    out: list[str] = []
    old_slots = [p for p in old if p.kind in _SLOT_KINDS]
    new_slots = [p for p in new if p.kind in _SLOT_KINDS]
    new_named = {p.name: p for p in new if p.kind in ("PO", "PK", "KO")}
    old_keyword_only = {p.name for p in old if p.kind == "KO"}
    old_kinds = {p.kind for p in old}
    new_kinds = {p.kind for p in new}
    for kind, label in (("VP", "*args"), ("VK", "**kwargs")):
        if kind in old_kinds and kind not in new_kinds:
            out.append(f"{where}: {label} removed")
    for i, op in enumerate(old_slots):
        slot = f"positional slot {i + 1}"
        np = new_slots[i] if i < len(new_slots) else None
        if np is None:
            moved = new_named.get(op.name)
            if moved is not None and moved.kind == "KO":
                was = "positional-or-keyword" if op.kind == "PK" else "positional-only"
                out.append(f"{where}: {op.name} narrowed from {was} to keyword-only")
            else:
                out.append(f"{where}: {slot} ({op.name}) removed")
            continue
        if op.kind == "PK":
            if np.name != op.name:
                out.append(f"{where}: {slot} is now {np.name}, was {op.name}")
                continue
            if np.kind == "PO":
                out.append(f"{where}: {op.name} narrowed to positional-only")
                continue
            out += _defaults(where, op.name, op, np)
        else:
            # A positional-only slot that turns positional-or-keyword collides with
            # any old call that filled it positionally and also passed its new name
            # by keyword: through **kwargs, or as a keyword-only parameter.
            if np.kind == "PK" and np.name in old_keyword_only:
                out.append(
                    f"{where}: keyword-only {np.name} moved into {slot}, which old calls "
                    f"fill beside {np.name}= (keyword collision)"
                )
            elif np.kind == "PK" and "VK" in old_kinds:
                out.append(
                    f"{where}: {np.name} became positional-or-keyword beside **kwargs "
                    "(keyword collision)"
                )
            out += _defaults(where, slot, op, np)
    if "VP" in old_kinds and len(new_slots) > len(old_slots):
        names = ", ".join(p.name for p in new_slots[len(old_slots) :])
        out.append(f"{where}: positional parameter(s) {names} capture arguments *args used to take")
    for op in old:
        if op.kind != "KO":
            continue
        np = new_named.get(op.name)
        if np is None:
            out.append(f"{where}: keyword {op.name} removed")
        elif np.kind == "PO":
            out.append(f"{where}: {op.name} narrowed from keyword-only to positional-only")
        else:
            out += _defaults(where, op.name, op, np)
    old_names = {p.name for p in old if p.kind in ("PK", "KO")}
    for i, np in enumerate(new_slots):
        if np.default is None and i >= len(old_slots) and np.name not in old_names:
            out.append(f"{where}: new required parameter {np.name}")
    for np in new:
        if np.kind == "KO" and np.default is None and np.name not in old_names:
            out.append(f"{where}: new required parameter {np.name}")
    return list(dict.fromkeys(out))


def _compare_call(where: str, old: api_records.Call, new: api_records.Call) -> "list[str]":
    out = []
    if old.callkind != new.callkind:
        out.append(f"{where}: call kind {old.callkind} -> {new.callkind}")
    if old.builtin is not None or new.builtin is not None:
        if old.builtin != new.builtin:
            out.append(
                f"{where}: constructor {old.builtin or 'signature'} -> {new.builtin or 'signature'}"
            )
        return out
    return out + compare_params(where, old.params or [], new.params or [])


def _mro(key: str, old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    old_rec, new_rec = old.get("mro", key), new.get("mro", key)
    if old_rec is None or new_rec is None:
        return []
    new_entries = api_records.split_list(new_rec.fields[0])
    new_sets = [set(e[1:-1].split(",")) for e in new_entries if e.startswith("{")]
    out = []
    for entry in api_records.split_list(old_rec.fields[0]):
        if entry.startswith("{"):
            found = any(set(entry[1:-1].split(",")) & s for s in new_sets)
        else:
            found = entry in new_entries
        if not found:
            out.append(f"{key}: no longer a subclass of {entry}")
    return out


def _members(key: str, old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    out = []
    new_members = new.members_of(key)
    for name, orec in sorted(old.members_of(key).items()):
        nrec = new_members.get(name)
        if nrec is None:
            out.append(f"{orec.key}: removed")
            continue
        okind, nkind = orec.fields[0], nrec.fields[0]
        if okind.startswith("property:") and nkind.startswith("property:"):
            for part, was, now in zip(_PROPERTY_PARTS, okind[9:], nkind[9:], strict=True):
                if was != "-" and now == "-":
                    out.append(f"{orec.key}: lost its {part}")
            continue
        if okind != nkind:
            out.append(f"{orec.key}: kind changed {okind} -> {nkind}")
            continue
        if okind in ("method", "classmethod", "staticmethod"):
            out += _compare_call(
                orec.key, api_records.parse_call(orec), api_records.parse_call(nrec)
            )
    return out


def _inputs(key: str, old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    out = []
    old_inputs, new_inputs = old.inputs_of(key), new.inputs_of(key)
    for sub, orec in old_inputs.items():
        where = f"{key} input {sub}"
        nrec = new_inputs.get(sub)
        if nrec is None:
            out.append(f"{where}: removed")
            continue
        _, oreq, odef, oroutes = orec.fields
        _, nreq, ndef, nroutes = nrec.fields
        if oreq == "optional" and nreq == "required":
            out.append(f"{where}: became required")
        elif api_records.EMPTY not in (odef, ndef) and not api_records.values_equal(odef, ndef):
            out.append(f"{where}: default {odef} -> {ndef}")
        out += [
            f"{where}: route {r} removed"
            for r in api_records.split_list(oroutes)
            if r not in api_records.split_list(nroutes)
        ]
    out += [
        f"{key} input {sub}: new required input"
        for sub, nrec in new_inputs.items()
        if sub not in old_inputs and nrec.fields[1] == "required"
    ]
    return out


def _obligations(key: str, old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    out = []
    for kind in ("abstract", "requires"):
        old_rec, new_rec = old.get(kind, key), new.get(kind, key)
        if old_rec is None or new_rec is None:
            continue
        gained = sorted(
            set(api_records.split_list(new_rec.fields[0]))
            - set(api_records.split_list(old_rec.fields[0]))
        )
        out += [f"{key}: new obligation {name} for implementers" for name in gained]
    return out


def _enums(key: str, old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    out = []
    new_enums = new.enums_of(key)
    for name, orec in old.enums_of(key).items():
        nrec = new_enums.get(name)
        if nrec is None:
            out.append(f"{key}: enum member {name} removed")
        elif not api_records.values_equal(orec.fields[1], nrec.fields[1]):
            out.append(f"{key}: enum member {name} value {orec.fields[1]} -> {nrec.fields[1]}")
    return out


def _formats(old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    """Return a format removed, or a version dropped from reads or writes (dump spec §13.3)."""
    out = []
    for name, orec in sorted(old.formats.items()):
        nrec = new.formats.get(name)
        if nrec is None:
            out.append(f"format {name}: removed")
            continue
        for label, ofield, nfield in zip(
            ("reads", "writes"), orec.fields, nrec.fields, strict=True
        ):
            kept = set(api_records.split_list(nfield))
            out += [
                f"format {name}: {label} no longer has {v}"
                for v in api_records.split_list(ofield)
                if v not in kept
            ]
    return out


def compare_dumps(old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    """Return every breaking finding from *old* to *new* (dump spec §4.2), each once.

    One fact can surface through two record families: a nested class is both a
    binding and a member of its outer class, so removing it reads ``removed``
    from each. A finding is its text, so an identical one is dropped here, at
    the source, and every consumer (the count, the grouped review aid) agrees.
    """
    out: list[str] = []
    for key, kind in sorted(old.bindings.items()):
        if key not in new.bindings:
            out.append(f"{key}: removed")
            continue
        if new.bindings[key] != kind:
            out.append(f"{key}: kind changed {kind} -> {new.bindings[key]}")
            continue
        out += _mro(key, old, new)
        old_call, new_call = old.get("call", key), new.get("call", key)
        if old_call is not None and new_call is not None:
            out += _compare_call(
                key, api_records.parse_call(old_call), api_records.parse_call(new_call)
            )
        out += _inputs(key, old, new)
        out += _members(key, old, new)
        out += _obligations(key, old, new)
        out += _enums(key, old, new)
    out += _formats(old, new)
    return list(dict.fromkeys(out))


_WHERE = re.compile(r"^(otto(?:\.\w+)*):(\S+)(.*)$")


def group_findings(findings: "list[str]") -> "list[str]":
    """Merge findings that differ only in namespace, naming every namespace (dump spec §10)."""
    groups: dict[str, list[str]] = {}
    for finding in findings:
        match = _WHERE.match(finding)
        if match is None:
            groups.setdefault(finding, [])
            continue
        namespace, rest, tail = match.groups()
        groups.setdefault(rest + tail, []).append(namespace)
    return [
        f"{text} [{', '.join(sorted(spaces))}]" if spaces else text
        for text, spaces in groups.items()
    ]
