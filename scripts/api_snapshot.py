#!/usr/bin/env python3
"""Write and check otto's public-API golden: the API dump of the declared namespaces.

The golden, ``tests/unit/api_snapshot/public_api.txt``, is the API dump (spec
``docs/superpowers/specs/2026-10-05-api-dump-design.md``) of every namespace
``api/public.toml`` declares: one record per binding, call, constructor input,
member, obligation, enum member and versioned format, under the two header
lines ``# api-snapshot v2`` and ``# producer-schema <n>``.
``scripts/api_dump_child.py`` produces it in a fresh interpreter and
``scripts/api_regen.py`` runs that child; this script writes the golden and
checks it. The docs do not produce lines: ``scripts/api_teaching.py``
validates what they teach against the same declaration.

``scripts/check_breaking_marks.py`` regenerates the dump of every commit it
checks and compares it with the committed golden byte for byte, so a commit
that changes the surface regenerates the golden too (``make api-snapshot``).

Usage:
    python scripts/api_snapshot.py --manifest api/public.toml [--golden G]
                                               # print the dump
    python scripts/api_snapshot.py --manifest api/public.toml --update
                                               # regenerate the golden
    python scripts/api_snapshot.py --manifest api/public.toml --check
                                               # exit 1 unless the golden is the dump
    python scripts/api_snapshot.py --manifest api/public.toml --report [--assume-dir]
                                               # print producer refusals; always exit 0
    --hash-seed N                              # the dump child's PYTHONHASHSEED
                                               # (the version-invariance lane, §6)
"""

import argparse
import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import api_lines  # noqa: E402 -- path set up above
from scripts.api_agreement import NamespaceReport  # noqa: E402 -- path set up above

GOLDEN_PATH = REPO_ROOT / "tests" / "unit" / "api_snapshot" / "public_api.txt"
SCHEMA_V2 = 2
MAX_HASH_SEED = 4_294_967_295
"""The largest ``PYTHONHASHSEED`` Python accepts."""


def describe_dump_drift(golden_text: str, current_text: str) -> str:
    """Say how a stale dump differs: a breaking change, growth, or a re-sort.

    For a person to read and decide on, never acted on: ``--check`` writes
    nothing. The findings are the checker's own (``scripts/api_compat.py``), so
    what this says is what ``check-breaking`` will say.
    """
    from scripts import api_compat, api_records

    try:
        golden = api_records.parse_dump(golden_text)
    except api_records.DumpError as exc:
        return f"the golden does not parse ({exc}); regenerate it with `make api-snapshot`."
    findings = api_compat.compare_dumps(golden, api_records.parse_dump(current_text))
    if findings:
        shown = "\n".join(f"  - {f}" for f in api_compat.group_findings(findings))
        return (
            f"public API CHANGED: {len(findings)} breaking finding(s):\n{shown}\n"
            "If the change is intended, record it with `make api-snapshot`, commit the "
            "golden, and mark the commit breaking (`!` or `BREAKING CHANGE:`)."
        )
    if sorted(golden_text.splitlines()) == sorted(current_text.splitlines()):
        return "the golden is not in canonical order; regenerate it with `make api-snapshot`."
    return (
        "public API GREW (or changed without breaking anything): record it with "
        "`make api-snapshot` and commit the golden."
    )


def undumped_names(text: str, reports: "dict[str, NamespaceReport]") -> "list[str]":
    """Return every declared ``ns:name`` (a namespace's ``__all__`` entry) *text* does not record.

    The dump's coverage is exact, so this is its size check: a producer that
    dropped a declared name is caught here, before ``--update`` writes it. A
    binding-count floor would refuse a small, correct manifest and still pass a
    dump that dropped a few names.
    """
    from scripts import api_records

    bound = set(api_records.parse_dump(text).bindings)
    return [
        f"{ns}:{name}"
        for ns in sorted(reports)
        for name in reports[ns].all or []
        if f"{ns}:{name}" not in bound
    ]


def _main_dump(args: argparse.Namespace) -> int:
    """Run ``main``: the manifest's namespaces, as the API dump records them.

    Every failure is a ``FAIL <reason>`` line and exit 1, never a traceback. With
    ``--report`` it prints the producer's refusals, counts them, and exits 0: a
    measurement, never a gate.
    """
    from scripts import api_regen
    from scripts.api_agreement import AgreementError, agreement_failures, namespace_reports
    from scripts.api_manifest import ManifestError, load_manifest

    try:
        names = sorted(load_manifest(args.manifest))
    except ManifestError as exc:
        print(f"FAIL {exc}")
        return 1
    try:
        reports = namespace_reports(names, REPO_ROOT)
    except AgreementError as exc:
        print(f"FAIL cannot report the declared namespaces: {exc}")
        return 1
    failures = agreement_failures(names, reports)
    generated = api_regen.generate_worktree(
        REPO_ROOT,
        args.manifest,
        assume_dir=args.assume_dir,
        seed=args.hash_seed or api_regen.GATE_HASH_SEED,
    )
    problems = [*failures, *generated.refusals]
    if generated.text is not None:
        problems += [
            f"the dump has no name record for the declared {key}"
            for key in undumped_names(generated.text, reports)
        ]
    if args.report:
        # Agreement failures are api_teaching.py --report's to list; only the
        # producer's own refusals are this mode's.
        for refusal in generated.refusals:
            print(f"refusal: {refusal}")
        print(f"api-dump-report: {len(generated.refusals)} producer refusal(s)")
        return 0
    for problem in problems:
        print(f"FAIL {problem}")
    golden = args.golden or GOLDEN_PATH
    if args.update:
        if problems or generated.text is None:
            print("\nnot writing the golden: resolve the failures above first.")
            return 1
        golden.write_bytes(generated.text.encode("utf-8"))
        return 0
    if args.check:
        return _check_dump(golden, generated.text, problems)
    if generated.text is not None:
        print(generated.text, end="")
    return 1 if problems else 0


def _check_dump(golden: Path, current: "str | None", problems: list[str]) -> int:
    """Compare the committed dump *golden* with *current*; return the exit code.

    The comparison is of BYTES, as ``check-breaking``'s freshness check makes it
    (dump spec §5.1): a golden whose text matches but whose bytes do not (CRLF
    line endings, say) is stale here exactly as it is there.
    """
    try:
        data = golden.read_bytes()
    except OSError as exc:
        print(f"FAIL cannot read the golden {golden}: {exc}")
        return 1
    text = data.decode("utf-8", errors="replace")
    if api_lines.schema_of(text) != SCHEMA_V2:
        print(f"FAIL {golden} is not an api-snapshot v2 golden: no {api_lines.V2_HEADER!r} line")
        return 1
    if problems or current is None:
        return 1
    if data != current.encode("utf-8"):
        if text.splitlines() == current.splitlines():
            print(
                "FAIL the golden's bytes differ from the dump's though every line matches "
                "(line endings or encoding); regenerate it with `make api-snapshot`."
            )
            return 1
        print(
            "\n".join(
                difflib.unified_diff(
                    text.splitlines(), current.splitlines(), "golden", "current", lineterm=""
                )
            )
        )
        print(describe_dump_drift(text, current))
        return 1
    print("api snapshot: OK")
    return 0


def _hash_seed(text: str) -> str:
    """Return *text* if it is a valid ``PYTHONHASHSEED`` (0 to 4294967295), else raise."""
    if not (text.isascii() and text.isdigit() and int(text) <= MAX_HASH_SEED):
        raise argparse.ArgumentTypeError(f"{text!r} is not a PYTHONHASHSEED (0 to 4294967295)")
    return text


def main(argv: list[str]) -> int:
    """Print the dump; optionally --update the golden or --check it."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--update", action="store_true", help="regenerate the golden")
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 1 unless the golden is, byte for byte, the dump of the working tree",
    )
    ap.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="the declared namespaces and formats (api/public.toml)",
    )
    ap.add_argument(
        "--assume-dir",
        action="store_true",
        help="a namespace with no __all__ exports its public globals (report only)",
    )
    ap.add_argument("--report", action="store_true", help="print refusals and exit 0")
    ap.add_argument(
        "--hash-seed",
        type=_hash_seed,
        default=None,
        help="the dump child's PYTHONHASHSEED (default: the gate's fixed seed); the "
        "version-invariance lane passes a different one per interpreter",
    )
    ap.add_argument(
        "--golden", type=Path, default=None, help="golden path (default: the committed one)"
    )
    return _main_dump(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
