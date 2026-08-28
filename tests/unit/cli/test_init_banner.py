"""The `otto init` "Next steps" banner lists BOTH shell-completion steps.

`otto --install-completion` writes the completion script; nothing sources it
for the current shell, so a user who runs only the first step sees no
completion and concludes it is broken (spec 2026-08-27 lab-definition-v2 §12).
The init e2e test runs the banner's command list, so the pair is exercised
end to end there too.
"""

from pathlib import Path

# Reuse the validate suite's harness: same DispatchRunner wiring, same
# COLUMNS pinning, and the same "scaffold a whole repo first" helper.
from tests.unit.cli.test_init_validate import _invoke, _scaffold_all


def test_next_steps_lists_both_completion_steps(tmp_path: Path) -> None:
    _scaffold_all(tmp_path)
    out = _invoke(["--all", "--path", str(tmp_path)]).output
    assert "otto --install-completion" in out
    assert "source ~/.bash_completions/otto.sh" in out
    assert out.index("otto --install-completion") < out.index("source ~/.bash_completions/otto.sh")
