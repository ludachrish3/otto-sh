"""CI's web gate must INVOKE the gate, not re-enumerate it.

The `web-quality` job used to hand-list `web-check`'s sub-targets
(``web-lint`` + ``web-format-check`` + ``web-typecheck`` + ``web-coverage``)
instead of calling the umbrella. That list is a second copy of the gate, and it
drifted: ``biome lint`` and ``biome format`` do **not** report Biome's *assist*
actions (organize-imports), so unsorted imports passed both while failing
``biome check`` — the command developers actually run via ``npm run check``. Ten
such errors sat on main with the job green.

The failure is structural, not incidental: any gate a CI job re-lists can lose a
step without anything noticing. So these tests pin the two halves of the repair —
CI calls the umbrella, and the umbrella runs the authoritative Biome command.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_MAKEFILE = _ROOT / "Makefile"
_PACKAGE_JSON = _ROOT / "web" / "package.json"

# The narrower Biome lanes. Useful at a dev's fingertips, but neither reports
# assist actions, so CI must never gate on them *instead of* `biome check`.
_WEAKER_THAN_BIOME_CHECK = ("web-lint", "web-format-check")


def _web_quality_run_lines() -> list[str]:
    """Every `run:` command inside ci.yml's `web-quality` job."""
    text = _CI.read_text(encoding="utf-8")
    start = text.index("\n  web-quality:")
    # The next top-level job (two-space indent, a name, a colon) ends this one.
    rest = text[start + 1 :]
    end = re.search(r"\n  [a-z][a-z0-9-]*:\n", rest)
    body = rest[: end.start()] if end else rest
    return [m.group(1).strip() for m in re.finditer(r"^\s*run:\s*(.+)$", body, re.MULTILINE)]


def test_ci_web_quality_invokes_the_umbrella_target():
    runs = _web_quality_run_lines()
    assert runs, "web-quality job has no `run:` steps — did the job get renamed?"
    assert runs == ["make web-check"], (
        "CI's web-quality job must invoke the `web-check` UMBRELLA, not re-list its "
        f"sub-targets. Found: {runs}. A job that re-enumerates a gate is a second copy "
        "of it, and it will drift — that is exactly how `biome check`'s assist actions "
        "(organize-imports) stopped being checked on main."
    )


@pytest.mark.parametrize("target", _WEAKER_THAN_BIOME_CHECK)
def test_ci_does_not_gate_on_the_narrower_biome_lanes(target: str):
    """`biome lint` / `biome format` each miss assist actions. Neither is the gate."""
    assert target not in " ".join(_web_quality_run_lines()), (
        f"CI's web-quality job gates on `{target}`, which does NOT report Biome's "
        "assist actions (organize-imports). Gate on `make web-check`, which runs "
        "`biome check`."
    )


def test_web_check_runs_the_authoritative_biome_command():
    """The umbrella is only worth invoking if it runs `biome check`.

    Pins the whole chain: `web-check` -> `web-biome` -> `npm run check` ->
    `biome check`. Break any link and unsorted imports go unreported again.
    """
    makefile = _MAKEFILE.read_text(encoding="utf-8")
    web_check = re.search(r"^web-check:([^\n#]*)", makefile, re.MULTILINE)
    assert web_check, "no `web-check` target in the Makefile"
    assert "web-biome" in web_check.group(1), (
        "`web-check` no longer depends on `web-biome`, so CI is not running "
        f"`biome check`. Prerequisites are: {web_check.group(1).strip()!r}"
    )

    web_biome = re.search(r"^web-biome:.*\n\t(.+)$", makefile, re.MULTILINE)
    assert web_biome, "no `web-biome` target in the Makefile"
    assert "npm run check" in web_biome.group(1)

    scripts = _PACKAGE_JSON.read_text(encoding="utf-8")
    assert re.search(r'"check"\s*:\s*"biome check', scripts), (
        'web/package.json\'s "check" script must run `biome check` — it is the only '
        "Biome invocation that reports lint rules, formatting AND assist actions."
    )
