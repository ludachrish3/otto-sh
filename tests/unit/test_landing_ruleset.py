"""Pin the live GitHub ruleset and merge settings that make every landing a pull request.

The live ruleset is not in the tree, so this reads it with ``gh api``. It runs
only under ``make release-preflight`` (which sets ``OTTO_RULESET_CHECK=1``):
every other lane lacks the network and the credentials.
"""

import functools
import json
import subprocess

import pytest

from scripts.gate_stamp_common import remote_slug
from tests._ambient_env import ambient
from tests._fixtures.paths import CHECKOUT_ROOT

RULESET_ID = 15843311

pytestmark = [
    pytest.mark.interpreter_agnostic,
    pytest.mark.skipif(
        ambient("OTTO_RULESET_CHECK") != "1",
        reason="reads the live ruleset; make release-preflight sets OTTO_RULESET_CHECK=1",
    ),
]


def _gh_api(endpoint: str) -> dict:
    done = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"gh api {endpoint} exited {done.returncode}: {done.stderr}"
    return json.loads(done.stdout)


@functools.cache
def _slug() -> str:
    return remote_slug(CHECKOUT_ROOT, "origin")


@functools.cache
def _ruleset() -> dict:
    return _gh_api(f"repos/{_slug()}/rulesets/{RULESET_ID}")


@functools.cache
def _repository() -> dict:
    return _gh_api(f"repos/{_slug()}")


def _rule(rule_type: str) -> dict:
    rules = _ruleset()["rules"]
    matches = [r for r in rules if r["type"] == rule_type]
    assert len(matches) == 1, rule_type
    return matches[0]


def test_the_required_contexts_are_the_committed_list():
    rule = _rule("required_status_checks")
    committed = json.loads((CHECKOUT_ROOT / ".github/required_contexts.json").read_text())
    live = [c["context"] for c in rule["parameters"]["required_status_checks"]]
    assert sorted(live) == sorted(committed)
    assert rule["parameters"]["strict_required_status_checks_policy"] is False


def test_the_pull_request_rule_is_what_closes_main_to_direct_pushes():
    """Checks, linear history and an empty bypass list still admit a green fast-forward push.

    That is how #820 landed; the pull_request rule is what refuses a push.
    """
    rule = _rule("pull_request")
    # The keys this repository decides; GitHub adds defaults of its own beside them
    # (`required_reviewers`, `require_extra_approval_for_unattributed_changes`).
    want = {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": False,
        "required_review_thread_resolution": False,
        "allowed_merge_methods": ["rebase"],
    }
    assert {k: rule["parameters"].get(k) for k in want} == want


def test_the_ruleset_is_active_on_main():
    ruleset = _ruleset()
    assert ruleset["enforcement"] == "active"
    assert ruleset["target"] == "branch"
    assert "refs/heads/main" in ruleset["conditions"]["ref_name"]["include"]
    assert ruleset["conditions"]["ref_name"]["exclude"] == []


def test_linear_history_and_no_force_push_stay():
    kinds = {r["type"] for r in _ruleset()["rules"]}
    assert kinds >= {
        "required_linear_history",
        "non_fast_forward",
        "pull_request",
        "required_status_checks",
    }


def test_the_one_bypass_actor_is_the_admin_role_for_pull_requests_only():
    assert _ruleset()["bypass_actors"] == [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
    ]


def test_the_repository_merges_by_rebase_only():
    live = _repository()
    assert (live["allow_squash_merge"], live["allow_merge_commit"], live["allow_rebase_merge"]) == (
        False,
        False,
        True,
    )
