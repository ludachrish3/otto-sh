"""Refuse a positional parameter of a public callable that ``api/public.toml`` does not list.

Design ``docs/superpowers/specs/2026-11-07-keyword-only-public-interfaces-design.md`` §5.
Runs in the parent process over the dump :func:`scripts.api_regen.generate_worktree`
returns, joined with the manifest's ``[symbols]`` table. The dump child already
refused a listed name that is not a leading positional prefix; this rule judges
what the child does not: the positional shape of every ``call`` and method
``member`` must EQUAL its effective entry (own, inherited through the dump's
``mro``, or reached through a structurally conforming protocol), the sources
must agree, and a positional-only or variadic parameter the entry covers needs
a ``why``. A withdrawn or builtin-inherited constructor has nothing to judge;
the interpreter's own slots of a supported dunder are skipped. A class or
model whose subclass right is withdrawn must be ``@final`` in the dump (or
exempt with ``final = false``), and a ``@final`` one must have it withdrawn.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import api_records  # noqa: E402 -- path set up above
from scripts.api_manifest import Symbol  # noqa: E402 -- path set up above
from scripts.api_records import Dump  # noqa: E402 -- path set up above

_POSITIONAL = ("PO", "PK")
_WHY_KINDS = {"PO": "positional-only", "VP": "variadic"}


def positional_shape(call: api_records.Call) -> "list[str]":
    """Return *call*'s ``PO``/``PK`` names in order, then its ``VP`` name if any."""
    params = call.params or []
    names = [p.name for p in params if p.kind in _POSITIONAL]
    return names + [p.name for p in params if p.kind == "VP"]


def _callables(dump: Dump) -> "list[api_records.Record]":
    return [rec for rec in dump.records.values() if api_records.carries_call(rec)]


def _conforming_protocols(dump: Dump) -> "dict[str, list[str]]":
    """Map each binding to the protocols whose ``requires`` its public member names cover."""
    protocols = {}
    for key, kind in dump.bindings.items():
        rec = dump.get("requires", key)
        if kind == "protocol" and rec is not None:
            parsed = api_records.parse_obligations("requires", rec.fields[0])
            if not parsed.withdrawn:
                protocols[key] = set(parsed.names)
    out: dict[str, list[str]] = {}
    for key in dump.bindings:
        names = set(dump.members_of(key))
        out[key] = [p for p, requires in sorted(protocols.items()) if requires <= names]
    return out


def effective_entries(
    dump: Dump, symbols: "dict[str, Symbol]"
) -> "dict[str, tuple[Symbol, list[str]]]":
    """Map each callable key with an entry to (the entry that applies, its sources).

    A member's sources are its own entry, ``Base.method`` for every ``Base`` in its
    binding's dump ``mro``, and ``Protocol.method`` for every protocol whose
    ``requires`` is a subset of the binding's public member names (design §5.1).
    A ``call`` record has its own entry only. The own entry applies when present,
    else the first source; :func:`findings` reports sources that disagree.
    """
    conforming = _conforming_protocols(dump)
    out: dict[str, tuple[Symbol, list[str]]] = {}
    for rec in _callables(dump):
        candidates = [rec.key]
        if rec.kind == "member":
            owner, _, method = rec.key.rpartition(".")
            mro = dump.get("mro", owner)
            for entry in api_records.split_list(mro.fields[0]) if mro is not None else []:
                bases = api_records.mro_set(entry)
                if bases is not None:
                    candidates += [f"{base}.{method}" for base in sorted(bases)]
            candidates += [f"{proto}.{method}" for proto in conforming.get(owner, [])]
        sources = [k for k in dict.fromkeys(candidates) if k in symbols]
        if sources:
            out[rec.key] = (symbols[sources[0]], sources)
    return out


def _judge(
    rec: api_records.Record,
    call: api_records.Call,
    applied: "tuple[Symbol, list[str]] | None",
    symbols: "dict[str, Symbol]",
) -> "list[str]":
    key = rec.key
    kinds = {p.name: p.kind for p in call.params or []}
    shape = positional_shape(call)
    slots = api_records.DUNDER_SLOTS.get(key.rpartition(".")[2], 0) if rec.kind == "member" else 0
    fixed = [n for n in shape if kinds[n] != "VP"]
    interpreter = set(fixed[:slots])
    shape = fixed[slots:] + shape[len(fixed) :]
    if applied is None:
        return [_unlisted(key, name, kinds[name]) for name in shape]
    entry, sources = applied

    def judged(sym: Symbol) -> "list[str]":
        return [n for n in sym.positional if n not in interpreter]

    listed = judged(entry)
    out = [
        f"{key}: entries disagree: {sources[0]} says {listed}, {other} says "
        f"{judged(symbols[other])}"
        for other in sources[1:]
        if judged(symbols[other]) != listed
    ]
    if shape != listed:
        if key not in symbols and shape[: len(listed)] != listed:
            out.append(f"{key}: override reorders or renames {listed}")
        elif key not in symbols:
            out += [_unlisted(key, n, kinds[n]) for n in shape[len(listed) :]]
        else:
            out += [_unlisted(key, n, kinds[n]) for n in shape if n not in listed]
            out += [
                f"{key}: {n} is listed positional but keyword-only in the code"
                for n in listed
                if n not in shape
            ]
    if not entry.why:
        out += [
            f"{key}: {_WHY_KINDS[kinds[n]]} {n} needs a why"
            for n in listed
            if n in shape and kinds[n] in _WHY_KINDS
        ]
    return out


def _unlisted(key: str, name: str, kind: str) -> str:
    if kind == "VP":
        return f"{key}: *{name} accepts positionals; list it with a why"
    return (
        f"{key}: positional parameter {name} — make the record kw_only, "
        f'or list it under [symbols."{key}"].positional'
    )


def findings(dump: Dump, symbols: "dict[str, Symbol]") -> "list[str]":
    """Return one finding per positional parameter the effective entries do not justify.

    Then one per class or model whose ``@final`` and subclass right disagree.
    """
    entries = effective_entries(dump, symbols)
    out: list[str] = []
    for rec in _callables(dump):
        call = api_records.parse_call(rec)
        if call.withdrawn or call.builtin is not None:
            continue
        out += _judge(rec, call, entries.get(rec.key), symbols)
    return out + _final_findings(dump, symbols, entries)


def _final_findings(
    dump: Dump, symbols: "dict[str, Symbol]", entries: "dict[str, tuple[Symbol, list[str]]]"
) -> "list[str]":
    """Hold ``@final`` and a withdrawn subclass right together (design §5, findings 5-6).

    Classes and models only: a protocol's ``requires`` record never carries the
    token. The entry is the effective one (a class's own, at each alias key).
    """
    out = []
    for key, kind in sorted(dump.bindings.items()):
        rec = dump.get("abstract", key)
        if kind not in ("class", "model") or rec is None:
            continue
        final = api_records.parse_obligations("abstract", rec.fields[0]).final
        applied = entries.get(key)
        entry = applied[0] if applied is not None else symbols.get(key)
        closed = entry is not None and not entry.subclass
        if closed and not final and entry.final is not False:
            out.append(
                f"{key}: subclass withdrawn but the class is not @final"
                " — decorate it, or set final = false with why"
            )
        if final and not closed:
            out.append(
                f"{key}: @final on a class whose subclass right is open"
                " — withdraw it in the manifest, or drop the decorator"
            )
    return out


def main(argv: "list[str]") -> int:
    """Print every finding, one per line; exit 1 if there is any (or no dump), else 0."""
    import argparse

    from scripts import api_regen
    from scripts.api_manifest import ManifestError, load_symbols

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)

    generated = api_regen.generate_worktree(REPO_ROOT, args.manifest)
    if generated.text is None:
        for refusal in generated.refusals:
            print(f"producer refusal: {refusal}")
        return 1
    try:
        symbols = load_symbols(args.manifest)
    except ManifestError as exc:
        print(f"FAIL {exc}")
        return 1
    out = findings(api_records.parse_dump(generated.text), symbols)
    for finding in out:
        print(finding)
    if out:
        print(f"check-api-keywords: {len(out)} finding(s)")
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
