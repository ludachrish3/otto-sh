"""Negation-parity matching over C preprocessor conditions.

This is deliberately NOT an evaluator. otto never needs to know which arm
the preprocessor selected, because a dead arm produces no code and
therefore no gcov records — deleting it would be a no-op. All that is
needed is whether a flagged macro could enable the arm, which is a token
scan plus a parenthesis stack.
"""

import re

_SYMBOL_RE = re.compile(r"[A-Za-z_]\w*|&&|\|\||[!()]|\S")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def references_positively(keyword: str, condition: str, macros: "list[str]") -> bool:
    """Report whether any name in *macros* appears at even negation parity.

    ``#ifdef X`` is parity 0 on X; ``#ifndef X`` is parity 1. ``#else`` and
    ``#endif`` carry no condition and therefore never match.
    """
    if keyword in ("else", "endif"):
        return False
    wanted = set(macros)
    if keyword == "ifdef":
        names = condition.split()
        return bool(names) and names[0] in wanted
    if keyword == "ifndef":
        return False

    stack = [0]
    pending = 0
    for symbol in _SYMBOL_RE.findall(condition):
        if symbol == "!":
            pending ^= 1
        elif symbol == "(":
            stack.append(stack[-1] ^ pending)
            pending = 0
        elif symbol == ")":
            if len(stack) > 1:
                stack.pop()
            pending = 0
        elif symbol == "defined":
            # The operator itself is not a reference, and must not consume the
            # pending negation — `!defined(X)` has to carry it inside.
            continue
        elif _IDENT_RE.fullmatch(symbol):
            if symbol in wanted and (stack[-1] ^ pending) == 0:
                return True
            pending = 0
        else:
            pending = 0
    return False
