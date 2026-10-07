"""CI must judge a v2-era PR's own commits, and leave a v1 PR's checkout exactly as it was.

``actions/checkout`` on a pull_request checks out ``refs/pull/N/merge``, a
merge commit GitHub creates whose message carries no breaking mark. Under v2,
merges are checked against their first parent (spec 2026-10-04 §6), so that
commit would be judged as an unmarked change carrying the whole PR: the
marking step must detach to the PR head first.

The v2 rules apply when EITHER end carries the v2 header (spec §6), so the
detach is gated on the golden at the PR's base OR its head carrying the
``# api-snapshot v2`` line. A PR whose head deletes a v2 golden is v2-era
through its base alone. A golden missing at a sha counts as "not v2" there.

A v1 PR (neither end v2) keeps today's live decisions byte-for-byte: v1
skips merges, and its docs-only exemption resolves paths against the
checked-out tree, which must stay the synthetic merged tree, so it runs no
checkout.

Whatever the event, push or pull_request, a v2 golden at ``HEAD`` must open
with exactly its two header lines, ``# api-snapshot v2`` then ``# producer-schema
<n>`` (dump spec §2.1), or the step fails with an ``::error`` naming the golden
before the checker runs.

The behavioural tests run the step's real shell script under ``bash -e`` with
``git`` and ``uv`` replaced by stubs on ``PATH`` that log their argv -- no
real git runs.
"""

import os
import subprocess

import pytest
import yaml

from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

GOLDEN = "tests/unit/api_snapshot/public_api.txt"
HEAD_SHA = "h" * 40
BASE_SHA = "b" * 40
V2_TEXT = "# api-snapshot v2\n# producer-schema 1\nname\totto:Alpha\tfunction\n"
# api_lines.schema_of strips each line, so whitespace around the header is still v2.
V2_SPACED_TEXT = "  # api-snapshot v2 \t\n# header\nname otto:Alpha\n"
V1_TEXT = "# a v1 golden header\notto:Alpha\n"
INLINE_TEXT = "# a header that merely mentions # api-snapshot v2 inline\notto:Alpha\n"
CHECK = f"uv run python scripts/check_breaking_marks.py {BASE_SHA}..HEAD"


def _marking_step_script() -> str:
    ci = yaml.safe_load((PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    (step,) = [
        s for s in ci["jobs"]["lint-python"]["steps"] if "check-breaking-marks" in s.get("name", "")
    ]
    return step["run"]


def _run_marking_step(
    tmp_path,
    *,
    event: str,
    base_text: "str | None" = None,
    head_text: "str | None" = None,
    checked_out: "str | None" = None,
) -> "tuple[subprocess.CompletedProcess[str], list[str]]":
    """Run the marking step for *event*; return the finished process and the stubs' argv lines.

    The ``git`` stub answers ``git show <rev>:<golden>`` with the golden text
    given for that rev (exit 128, as git does, when it is None or the path is
    not the golden) and logs every call. *checked_out* is the golden at
    ``HEAD``, the commit the step checks.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    goldens = tmp_path / "goldens"
    goldens.mkdir()
    log = tmp_path / "calls.log"
    for rev, text in ((BASE_SHA, base_text), (HEAD_SHA, head_text), ("HEAD", checked_out)):
        if text is not None:
            (goldens / f"{rev}.txt").write_text(text, encoding="utf-8", newline="")
    (bin_dir / "git").write_text(
        "#!/bin/sh\n"
        'echo "git $*" >> "$STUB_LOG"\n'
        'if [ "$1" = show ]; then\n'
        '  sha="${2%%:*}"; path="${2#*:}"\n'
        '  if [ "$path" != "$STUB_GOLDEN" ] || [ ! -f "$STUB_DIR/$sha.txt" ]; then exit 128; fi\n'
        '  cat "$STUB_DIR/$sha.txt"\n'
        "fi\n"
    )
    (bin_dir / "uv").write_text('#!/bin/sh\necho "uv $*" >> "$STUB_LOG"\n')
    for stub in ("git", "uv"):
        (bin_dir / stub).chmod(0o755)
    script = (
        _marking_step_script()
        .replace("${{ github.event_name }}", event)
        .replace("${{ github.event.pull_request.head.sha }}", HEAD_SHA)
        .replace("${{ github.event.pull_request.base.sha }}", BASE_SHA)
        .replace("${{ github.event.before }}", "0" * 40)
    )
    assert "${{" not in script, script
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_DIR": str(goldens),
        "STUB_GOLDEN": GOLDEN,
        "TMPDIR": str(tmp_path),
    }
    proc = subprocess.run(
        ["bash", "-e", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def _run_pr_step(tmp_path, base_text: "str | None", head_text: "str | None") -> "list[str]":
    """Run the marking step as a pull_request; return the stubs' logged argv lines."""
    proc, calls = _run_marking_step(
        tmp_path, event="pull_request", base_text=base_text, head_text=head_text
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return calls


@pytest.mark.parametrize(
    ("base_text", "head_text"),
    [
        (V1_TEXT, V2_TEXT),
        (None, V2_TEXT),
        (V2_TEXT, V2_TEXT),
        (V2_TEXT, V1_TEXT),
        (V2_TEXT, None),
        (V1_TEXT, V2_SPACED_TEXT),
        (V2_SPACED_TEXT, None),
    ],
    ids=[
        "v1-base-v2-head",
        "no-base-v2-head",
        "v2-both",
        "v2-base-v1-head",
        "v2-base-no-head",
        "v2-head-header-with-whitespace",
        "v2-base-header-with-whitespace",
    ],
)
def test_a_v2_era_pr_is_detached_to_its_head_before_the_check(tmp_path, base_text, head_text):
    calls = _run_pr_step(tmp_path, base_text, head_text)
    checkout = f"git checkout -q --detach {HEAD_SHA}"
    assert checkout in calls, calls
    assert calls.index(checkout) < calls.index(CHECK)


@pytest.mark.parametrize(
    ("base_text", "head_text"),
    [
        (V1_TEXT, V1_TEXT),
        (INLINE_TEXT, INLINE_TEXT),
        (V1_TEXT, None),
        (None, V1_TEXT),
        (None, None),
    ],
    ids=["v1-both", "v2-marker-not-a-whole-line", "v1-base-no-head", "no-base-v1-head", "neither"],
)
def test_a_v1_pr_keeps_the_merged_tree_checkout(tmp_path, base_text, head_text):
    calls = _run_pr_step(tmp_path, base_text, head_text)
    assert not [c for c in calls if c.startswith("git checkout")], calls
    assert calls[-1] == CHECK


def test_both_endpoint_gates_precede_the_detach_in_the_script():
    script = _marking_step_script()
    checkout_at = script.index("git checkout")
    for sha in ("base.sha", "head.sha"):
        gate = f'git show "${{{{ github.event.pull_request.{sha} }}}}:{GOLDEN}"'
        assert script.index(gate) < checkout_at, gate
    assert script.index("# api-snapshot v2") < checkout_at
    assert checkout_at < script.index("check_breaking_marks.py")


GOLDEN_ERROR = f"::error file={GOLDEN}::"
CHECKER = "uv run python scripts/check_breaking_marks.py "


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize(
    "checked_out",
    [
        V2_SPACED_TEXT,
        "# api-snapshot v2\n# header\nname otto:Alpha\n",
        "# a comment\n# api-snapshot v2\n# producer-schema 1\n",
        V2_TEXT.replace("\n", "\r\n"),
        "  # api-snapshot v2\n# producer-schema 1\nname\totto:Alpha\tfunction\n",
    ],
    ids=[
        "header-not-exact",
        "line-2-not-the-schema",
        "header-not-on-line-1",
        "crlf",
        "header-not-exact-line-2-ok",
    ],
)
def test_a_v2_golden_without_its_two_header_lines_fails_before_the_check(
    tmp_path, event, checked_out
):
    proc, calls = _run_marking_step(
        tmp_path, event=event, base_text=V2_TEXT, head_text=checked_out, checked_out=checked_out
    )
    assert proc.returncode == 1
    assert GOLDEN_ERROR in proc.stdout
    assert not [c for c in calls if c.startswith(CHECKER)], calls


@pytest.mark.parametrize("event", ["pull_request", "push"])
@pytest.mark.parametrize("checked_out", [V2_TEXT, V1_TEXT, None], ids=["v2", "v1", "absent"])
def test_a_well_headed_v1_or_absent_golden_reaches_the_check(tmp_path, event, checked_out):
    proc, calls = _run_marking_step(
        tmp_path, event=event, base_text=V2_TEXT, head_text=checked_out, checked_out=checked_out
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls[-1].startswith(CHECKER), calls
