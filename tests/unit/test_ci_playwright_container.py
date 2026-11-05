"""The browser-test CI jobs run in Playwright's image, tagged from uv.lock (#855).

scripts/playwright_version.py alone reads the locked version and spells the image;
a `playwright-version` job in each workflow publishes it and the browser jobs
consume `needs.playwright-version.outputs.image`.
"""

from typing import Any

import pytest
import yaml

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_WORKFLOWS = CHECKOUT_ROOT / ".github" / "workflows"
_IMAGE_EXPR = "${{ needs.playwright-version.outputs.image }}"


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOWS / name).read_text())


CI = _load("ci.yml")
NIGHTLY = _load("nightly.yml")

# (workflow file, parsed workflow, job id, commands the job must keep)
JOBS = [
    ("ci.yml", CI, "dashboard", ["uv python install 3.10", "OTTO_BROWSER_SHARD", "uv run nox -k"]),
    (
        "nightly.yml",
        NIGHTLY,
        "dashboard",
        ["uv python install 3.10", "OTTO_BROWSER_SHARD", "uv run nox -k", "--repeat-scope=session"],
    ),
    (
        "nightly.yml",
        NIGHTLY,
        "dashboard-soak",
        ["uv python install 3.10", "uv sync --all-extras --dev", "make dashboard-soak"],
    ),
]
IDS = [f"{f}:{j}" for f, _, j, _ in JOBS]


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _job_text(job: dict[str, Any]) -> str:
    return "\n".join(yaml.safe_dump(s) for s in job["steps"])


@pytest.mark.parametrize(("wf", "doc", "job_id", "retained"), JOBS, ids=IDS)
def test_the_job_runs_in_the_helper_derived_image(wf, doc, job_id, retained) -> None:
    job = doc["jobs"][job_id]
    assert job["container"]["image"] == _IMAGE_EXPR
    options = job["container"]["options"]
    assert "--ipc=host" in options
    assert "--init" in options
    assert job["env"]["HOME"] == "/root"
    needs = _needs(job)
    assert "playwright-version" in needs
    if wf == "ci.yml":
        assert "gate-admission" in needs


@pytest.mark.parametrize(("wf", "doc", "job_id", "retained"), JOBS, ids=IDS)
def test_the_job_no_longer_apt_installs_browser_dependencies(wf, doc, job_id, retained) -> None:
    text = _job_text(doc["jobs"][job_id])
    assert "--with-deps" not in text
    assert "ci_drop_unused_apt_sources" not in text


# The image ships neither make nor jq (scripts/typecheck_web.sh needs jq, and
# `make web` runs it). Only the missing tools are installed, so the whole
# command is pinned.
PROVISION_MAKE = (
    'missing=""; for tool in make jq; do command -v "$tool" >/dev/null 2>&1 || '
    'missing="$missing $tool"; done; [ -z "$missing" ] || '
    "{ apt-get update && apt-get install -y --no-install-recommends $missing; }"
)


@pytest.mark.parametrize(("wf", "doc", "job_id", "retained"), JOBS, ids=IDS)
def test_make_is_provisioned_before_the_first_make(wf, doc, job_id, retained) -> None:
    steps = doc["jobs"][job_id]["steps"]
    provision = [i for i, s in enumerate(steps) if s.get("run", "").strip() == PROVISION_MAKE]
    uses_make = [
        i
        for i, s in enumerate(steps)
        if any(line.strip().startswith("make ") for line in s.get("run", "").splitlines())
    ]
    assert len(provision) == 1
    assert uses_make
    assert provision[0] < uses_make[0]


# actions/checkout records the workspace as a git safe.directory only in a
# throwaway HOME. The container runs as root in a workspace the runner's user
# owns, so without this step every later git command refuses the checkout,
# and `make web`'s `git diff --exit-code a b` silently becomes a --no-index
# diff of the two files against each other (#855, first CI run).
TRUST_CHECKOUT = 'git config --global --add safe.directory "$GITHUB_WORKSPACE"'


@pytest.mark.parametrize(("wf", "doc", "job_id", "retained"), JOBS, ids=IDS)
def test_git_trusts_the_checkout_before_the_first_make(wf, doc, job_id, retained) -> None:
    steps = doc["jobs"][job_id]["steps"]
    checkout = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/checkout@")]
    trust = [i for i, s in enumerate(steps) if s.get("run", "").strip() == TRUST_CHECKOUT]
    first_make = next(
        i
        for i, s in enumerate(steps)
        if any(line.strip().startswith("make ") for line in s.get("run", "").splitlines())
    )
    assert len(checkout) == 1
    assert len(trust) == 1
    assert checkout[0] < trust[0] < first_make


@pytest.mark.parametrize(("wf", "doc", "job_id", "retained"), JOBS, ids=IDS)
def test_the_job_keeps_its_own_commands(wf, doc, job_id, retained) -> None:
    text = _job_text(doc["jobs"][job_id])
    for needle in ["node-version-file: .nvmrc", "make web", *retained]:
        assert needle in text, needle


@pytest.mark.parametrize(("wf", "doc"), [("ci.yml", CI), ("nightly.yml", NIGHTLY)])
def test_the_prep_job_publishes_the_helper_outputs(wf, doc) -> None:
    job = doc["jobs"]["playwright-version"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"] == {"contents": "read"}
    if wf == "ci.yml":
        assert _needs(job) == ["gate-admission"]
    steps = job["steps"]
    lock = [i for i, s in enumerate(steps) if s.get("id") == "lock"]
    assert len(lock) == 1
    # tomli comes from `--with`, and `--no-project` keeps the project unsynced.
    assert steps[lock[0]]["run"].strip() == (
        "uv run --no-project --with tomli python scripts/playwright_version.py uv.lock"
    )
    before = [s.get("uses", "") for s in steps[: lock[0]]]
    assert any(u.startswith("actions/checkout@") for u in before)
    assert any(u.startswith("astral-sh/setup-uv@") for u in before)
    assert job["outputs"] == {
        "version": "${{ steps.lock.outputs.version }}",
        "image": "${{ steps.lock.outputs.image }}",
    }


@pytest.mark.parametrize("name", ["ci.yml", "nightly.yml"])
def test_only_the_helper_spells_the_image_or_reads_the_lock(name) -> None:
    text = (_WORKFLOWS / name).read_text()
    for banned in (
        "mcr.microsoft.com/playwright",
        "import tomllib",
        "import tomli\n",
        "tomli.loads",
    ):
        assert banned not in text, banned
    # The only mention of the lock file is the prep job handing it to the helper.
    assert text.count("playwright_version.py uv.lock") == 1


@pytest.mark.parametrize(("wf", "doc"), [("ci.yml", CI), ("nightly.yml", NIGHTLY)])
def test_the_failure_reporter_needs_the_prep_job(wf, doc) -> None:
    assert "playwright-version" in _needs(doc["jobs"]["report-failure"])


def test_dependabot_keeps_direct_updates_and_allows_playwright() -> None:
    cfg = yaml.safe_load((CHECKOUT_ROOT / ".github" / "dependabot.yml").read_text())
    uv = next(u for u in cfg["updates"] if u["package-ecosystem"] == "uv")
    assert {"dependency-type": "direct"} in uv["allow"]
    assert {"dependency-name": "playwright", "dependency-type": "all"} in uv["allow"]
