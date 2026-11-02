"""The gate is only as good as its adoption.

`AGENTS.md` is the one instruction surface that exists INSIDE a worktree: a
worktree checkout contains exactly the tracked files, and `.claude` is
gitignored in full, so hook- or settings-based reminders are absent precisely
where the agent is working. This pins that the instruction cannot vanish in an
unrelated edit.
"""

from tests._fixtures.paths import CHECKOUT_ROOT

AGENTS_MD = CHECKOUT_ROOT / "AGENTS.md"


def test_agents_md_tells_agents_how_a_landing_is_gated_and_handed_back():
    # The raw Markdown: the button's name is bold in the bullet.
    # AGENTS.md is the only instruction surface present inside a worktree
    # (docs/superpowers/specs/2026-11-02-landing-enforcement-design.md).
    text = AGENTS_MD.read_text()
    assert "make gate-tip" in text
    assert "make gate-status" in text
    assert "make gate-fresh" not in text
    assert "HEAD:main" not in text
    assert "**Rebase and merge** button" in text


def test_agents_md_says_when_to_run_it():
    # A bare mention would let the bullet decay into a name with no trigger.
    text = AGENTS_MD.read_text().lower()
    assert "before any squash" in text or "hand back" in text
