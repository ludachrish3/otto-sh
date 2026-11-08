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
from scripts.api_manifest import Symbol  # noqa: E402 -- path set up above

_SLOT_KINDS = ("PO", "PK")
_PROPERTY_PARTS = ("getter", "setter", "deleter")
_TUPLE_MRO = api_records.builtin_mro("tuple")


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


def _implementer_additions(
    where: str, old: "list[api_records.Param]", new: "list[api_records.Param]", *, implementer: bool
) -> "list[str]":
    if not implementer:
        return []
    every_old = {p.name for p in old}
    return [
        f"{where}: new parameter {np.name} on an implementer-facing member"
        for np in new
        if np.kind != "VK" and np.name not in every_old
    ]


def compare_params(
    where: str,
    old: "list[api_records.Param]",
    new: "list[api_records.Param]",
    *,
    implementer: bool = False,
) -> "list[str]":
    """Return the §4.2 call findings between two parameter lists.

    An *implementer* member is one otto calls and the user overrides: any new
    parameter but ``**kwargs`` breaks an override that lacks it.
    """
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
    out += _implementer_additions(where, old, new, implementer=implementer)
    return list(dict.fromkeys(out))


def _compare_call(
    where: str, old: api_records.Call, new: api_records.Call, *, implementer: bool = False
) -> "list[str]":
    if old.withdrawn or new.withdrawn:
        return [f"{where}: constructor withdrawn"] if not old.withdrawn else []
    out = []
    if old.callkind != new.callkind:
        out.append(f"{where}: call kind {old.callkind} -> {new.callkind}")
    if old.builtin is not None or new.builtin is not None:
        if old.builtin != new.builtin:
            out.append(
                f"{where}: constructor {old.builtin or 'signature'} -> {new.builtin or 'signature'}"
            )
        return out
    return out + compare_params(where, old.params or [], new.params or [], implementer=implementer)


def _mro(key: str, old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    old_rec, new_rec = old.get("mro", key), new.get("mro", key)
    if old_rec is None or new_rec is None:
        return []
    new_entries = api_records.split_list(new_rec.fields[0])
    new_sets = [s for s in map(api_records.mro_set, new_entries) if s is not None]
    out = []
    for entry in api_records.split_list(old_rec.fields[0]):
        bases = api_records.mro_set(entry)
        found = any(bases & s for s in new_sets) if bases is not None else entry in new_entries
        if not found:
            out.append(f"{key}: no longer a subclass of {entry}")
    return out


def _members(
    key: str, old: api_records.Dump, new: api_records.Dump, implementer: "frozenset[str]"
) -> "list[str]":
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
                orec.key,
                api_records.parse_call(orec),
                api_records.parse_call(nrec),
                implementer=orec.key in implementer,
            )
    return out


def _input_site(key: str, sub: str) -> str:
    """Return the ``<where>`` of the input *sub* of the binding *key*."""
    return f"{key} input {sub}"


def _format_site(name: str) -> str:
    """Return the ``<where>`` of the versioned format *name*."""
    return f"format {name}"


def _inputs(key: str, old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    old_inputs, new_inputs = old.inputs_of(key), new.inputs_of(key)
    old_gone = any(api_records.is_withdrawn(r) for r in old_inputs.values())
    new_gone = any(api_records.is_withdrawn(r) for r in new_inputs.values())
    if old_gone or new_gone:
        return [f"{key}: constructor withdrawn"] if not old_gone else []
    out = []
    for sub, orec in old_inputs.items():
        where = _input_site(key, sub)
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
        f"{_input_site(key, sub)}: new required input"
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
        # The parsed record, never the raw field: ``@final`` is invisible to the
        # judge (design §4.4); the manifest token alone withdraws obligations.
        was = api_records.parse_obligations(kind, old_rec.fields[0])
        now = api_records.parse_obligations(kind, new_rec.fields[0])
        if was.withdrawn or now.withdrawn:
            if not was.withdrawn:
                out.append(f"{key}: obligations withdrawn")
            continue
        gained = sorted(set(now.names) - set(was.names))
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
            out.append(f"{_format_site(name)}: removed")
            continue
        for label, ofield, nfield in zip(
            ("reads", "writes"), orec.fields, nrec.fields, strict=True
        ):
            kept = set(api_records.split_list(nfield))
            out += [
                f"{_format_site(name)}: {label} no longer has {v}"
                for v in api_records.split_list(ofield)
                if v not in kept
            ]
    return out


def implementer_members(dump: api_records.Dump, symbols: "dict[str, Symbol]") -> "frozenset[str]":
    """Return the implementer-facing member keys of *dump* under *symbols* (spec §6.3).

    A member is implementer-facing when a non-withdrawn ``requires`` or
    ``abstract`` record names it, or its manifest entry says ``implementer``.
    """
    out = {key for key, sym in symbols.items() if sym.implementer}
    for key in dump.bindings:
        for kind in ("abstract", "requires"):
            rec = dump.get(kind, key)
            if rec is not None:
                parsed = api_records.parse_obligations(kind, rec.fields[0])
                out.update(f"{key}.{name}" for name in parsed.names)
    return frozenset(out)


def implementer_withdrawals(
    old_syms: "dict[str, Symbol]", new_syms: "dict[str, Symbol]"
) -> "list[str]":
    """Return a finding for every key whose ``implementer = true`` the newer manifest drops."""
    return [
        f"{key}: implementer status withdrawn"
        for key, sym in sorted(old_syms.items())
        if sym.implementer and not (key in new_syms and new_syms[key].implementer)
    ]


def _is_tuple_record(key: str, dump: api_records.Dump) -> bool:
    rec = dump.get("mro", key)
    return rec is not None and rec.fields[0] == _TUPLE_MRO


def _call(key: str, old: api_records.Dump, new: api_records.Dump) -> "list[str]":
    old_rec, new_rec = old.get("call", key), new.get("call", key)
    if old_rec is None or new_rec is None:
        return []
    old_call, new_call = api_records.parse_call(old_rec), api_records.parse_call(new_rec)
    # A tuple record's fields are its length and its unpacking order (spec §6.1);
    # a withdrawal on either side is _compare_call's to judge, tuple or not.
    withdrawn = old_call.withdrawn or new_call.withdrawn
    if not withdrawn and _is_tuple_record(key, old) and _is_tuple_record(key, new):
        return [] if old_call == new_call else [f"{key}: tuple record changed shape"]
    return _compare_call(key, old_call, new_call)


def compare_dumps(
    old: api_records.Dump, new: api_records.Dump, *, implementer: "frozenset[str]" = frozenset()
) -> "list[str]":
    """Return every breaking finding from *old* to *new* (dump spec §4.2), each once.

    *implementer* is the implementer-facing member set (:func:`implementer_members`);
    the caller passes the union over both commits.

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
        out += _call(key, old, new)
        out += _inputs(key, old, new)
        out += _members(key, old, new, implementer)
        out += _obligations(key, old, new)
        out += _enums(key, old, new)
    out += _formats(old, new)
    return list(dict.fromkeys(out))


FORMATS_NAMESPACE = "(formats)"


def sites(dump: api_records.Dump) -> "dict[str, str]":
    """Return every site of *dump*, each with the binding that owns it.

    A site is a binding, a member, an input or a format, spelled as
    :func:`compare_dumps` spells the ``<where>`` of a finding about it. A
    binding owns itself, its members and its inputs; a format owns itself.
    A finding names a site of the older dump, except "new required input",
    which names an input only the newer dump has.
    """
    out = {}
    for rec in dump.records.values():
        if rec.kind == "name":
            out[rec.key] = rec.key
        elif rec.kind == "member":
            out[rec.key] = api_records.binding_of(rec)
        elif rec.kind == "input" and not api_records.is_withdrawn(rec):
            out[_input_site(rec.key, rec.fields[0])] = rec.key
        elif rec.kind == api_records.FORMAT:
            out[_format_site(rec.key)] = _format_site(rec.key)
    return out


def site_of(finding: str) -> str:
    """Return the ``<where>`` of *finding*: the site it is about.

    A finding about a binding's bases, obligations or enum members names the
    binding, so those roll up to it.
    """
    return finding.partition(": ")[0]


_WHERE = re.compile(r"^(otto(?:\.\w+)*):(\S+)(.*)$")


def namespace_of(site: str) -> str:
    """Return the declared namespace *site* is under, or :data:`FORMATS_NAMESPACE` for a format."""
    match = _WHERE.match(site)
    return FORMATS_NAMESPACE if match is None else match.group(1)


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
