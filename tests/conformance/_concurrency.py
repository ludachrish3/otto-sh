"""The concurrency contract's one judgement: which answers came back to the wrong caller.

Shared by the contract and its positive control
(``tests/conformance/test_concurrency_contract.py``), so the control proves
the check the contract makes, not a copy of it.
"""

from collections.abc import Sequence

from otto.result import CommandResult


def crossed(results: "Sequence[CommandResult]", tokens: "Sequence[str]") -> "list[str]":
    """One sentence per result whose value lacks its own token or carries another call's.

    *results* and *tokens* are in call order: the i-th result answers the
    call that printed the i-th token. An empty list is the contract's pass.
    """
    assert len(results) == len(tokens), (
        f"{len(results)} results for {len(tokens)} calls: a call's answer is missing"
    )
    problems: list[str] = []
    for i, (result, token) in enumerate(zip(results, tokens, strict=True)):
        others = [other for other in tokens if other != token and other in result.value]
        if token not in result.value or others:
            problems.append(
                f"call {i} printed {token!r} but its value is {result.value!r}"
                + (f", which carries {others}" if others else "")
            )
    return problems
