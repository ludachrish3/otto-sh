"""Every otto log call whose message carries Rich markup opts in with ``extra={"markup": True}``.

A log record is rendered as Rich markup only when it asks for it (see
``docs/architecture/utilities/logging.md``). A call that writes ``[bold]`` into
its message and forgets the opt-in prints the brackets on every sink, which no
other test would notice. AST-based: the scan reads the literal text of the
message argument -- string constants, the constant parts of an f-string, and
the literals inside any expression that builds the message (a
``log_relayed(...)`` call, a concatenation) -- and asks Rich
whether a bracketed word in it is a style tag. A message assembled only from
variables escapes the scan; those sites are the author's to opt in.
"""

import ast
import re

from rich.errors import StyleSyntaxError
from rich.style import Style

from tests._fixtures.paths import CHECKOUT_ROOT

SRC = CHECKOUT_ROOT / "src" / "otto"

_LEVEL_METHODS = {"debug", "info", "warning", "error", "critical", "exception"}

# Rich's own tag syntax (``rich.markup.RE_TAGS``), with the escape run captured.
_TAG = re.compile(r"(\\*)\[([a-z#/@][^[]*?)]")


def _is_style_tag(body: str) -> bool:
    """True when ``[body]`` is a tag Rich renders as style rather than prints."""
    if body == "/":
        return True
    body = body.removeprefix("/")
    if body.startswith(("link=", "link ")) or body == "link":
        return True
    if body.startswith("@"):
        return False  # a handler tag; otto writes none
    try:
        Style.parse(body)
    except (StyleSyntaxError, ValueError):
        return False
    return True


def _has_style_tag(text: str) -> bool:
    for match in _TAG.finditer(text):
        escapes, body = match.groups()
        if len(escapes) % 2:  # an odd backslash run escapes the bracket
            continue
        if _is_style_tag(body):
            return True
    return False


def _literal_parts(node: ast.AST) -> list[str]:
    """Every string constant inside *node*, f-string constant parts included."""
    return [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _message_arg(call: ast.Call) -> ast.expr | None:
    assert isinstance(call.func, ast.Attribute)
    position = 1 if call.func.attr == "log" else 0
    return call.args[position] if len(call.args) > position else None


def _extra_flag(call: ast.Call, name: str, expected: bool) -> bool:
    for keyword in call.keywords:
        if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
            continue
        for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == name
                and isinstance(value, ast.Constant)
                and value.value is expected
            ):
                return True
    return False


def _opts_in(call: ast.Call) -> bool:
    return _extra_flag(call, "markup", True)


def _is_log_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr in _LEVEL_METHODS:
        return True
    # ``.log`` is a common name; only a receiver that reads as a logger counts.
    return node.func.attr == "log" and "log" in ast.unparse(node.func.value).lower()


def markup_without_opt_in(source: str) -> list[int]:
    """Line numbers of log calls in *source* whose message has a style tag but no opt-in."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not _is_log_call(node):
            continue
        assert isinstance(node, ast.Call)
        message = _message_arg(node)
        if message is None or _opts_in(node):
            continue
        if any(_has_style_tag(part) for part in _literal_parts(message)):
            offenders.append(node.lineno)
    return offenders


_ESCAPERS = {"escape", "escape_markup", "escape_relayed"}


def _calls_escaper(call: ast.Call) -> bool:
    """True when an argument of *call* calls ``escape`` / ``escape_markup`` / ``escape_relayed``."""
    return any(
        isinstance(inner, ast.Call)
        and (
            (isinstance(inner.func, ast.Name) and inner.func.id in _ESCAPERS)
            or (isinstance(inner.func, ast.Attribute) and inner.func.attr in _ESCAPERS)
        )
        for arg in call.args
        for inner in ast.walk(arg)
    )


def escaped_text_keeps_emoji(source: str) -> list[int]:
    """Line numbers of opted-in log calls that escape foreign text but leave emoji on.

    Escaping makes the brackets of text otto did not write print; the same text
    must not have its ``:word:`` runs turned into emoji either, so the record
    also passes ``"emoji": False``.
    """
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not _is_log_call(node):
            continue
        assert isinstance(node, ast.Call)
        if _opts_in(node) and _calls_escaper(node) and not _extra_flag(node, "emoji", False):
            offenders.append(node.lineno)
    return offenders


def escapes_without_opt_in(source: str) -> list[int]:
    """Line numbers of log calls in *source* that escape markup for a record that renders none.

    A record that does not opt in is printed as written, so a ``\\[`` in its
    literal text, or a value passed through ``rich.markup.escape`` /
    ``escape_relayed``, reaches the reader with its backslash.
    """
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not _is_log_call(node):
            continue
        assert isinstance(node, ast.Call)
        if _opts_in(node):
            continue
        escaped_literal = any("\\[" in part for arg in node.args for part in _literal_parts(arg))
        escaper_call = _calls_escaper(node)
        if escaped_literal or escaper_call:
            offenders.append(node.lineno)
    return offenders


def relay_flags_passed_by_caller(source: str) -> list[int]:
    """Line numbers of ``log_relayed(...)`` calls that pass ``markup`` or ``emoji`` themselves."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "log_relayed")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "log_relayed")
            )
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                keys = {k.value for k in keyword.value.keys if isinstance(k, ast.Constant)}
                if keys & {"markup", "emoji"}:
                    offenders.append(node.lineno)
    return offenders


def test_no_relay_call_sets_the_record_flags_itself():
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if (lines := relay_flags_passed_by_caller(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        f"log_relayed calls that pass markup/emoji: {offenders} -- the helper owns both flags"
    )


def test_the_relay_flag_scan():
    assert relay_flags_passed_by_caller('log_relayed(l, 20, "p", t, extra={"markup": True})') == [1]
    assert relay_flags_passed_by_caller('log_relayed(l, 20, "p", t, extra={"emoji": False})') == [1]
    assert relay_flags_passed_by_caller('log_relayed(l, 20, "p", t, extra={"host": h})') == []


def test_every_markup_log_call_opts_in():
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if (lines := markup_without_opt_in(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        f"log calls whose message carries Rich markup but do not opt in: {offenders} -- "
        'pass extra={"markup": True}, or drop the markup'
    )


def test_the_scan_sees_markup_and_its_opt_in():
    """The scanner flags a styled message and clears it once it opts in."""
    assert markup_without_opt_in('logger.info("[bold]done[/bold]")') == [1]
    assert markup_without_opt_in('logger.info(f"[green]{host}[/] up")') == [1]
    assert markup_without_opt_in('log.log(20, "[red]x")') == [1]
    assert markup_without_opt_in('logger.warning(f"[yellow]@{h} | " + out)') == [1]
    assert markup_without_opt_in('logger.info("[bold]done", extra={"markup": True})') == []


def test_the_scan_leaves_plain_brackets_alone():
    """Brackets Rich would print as text are not markup, escaped tags neither."""
    assert markup_without_opt_in('logger.info("[conn=0, chan=1] root:x:0:0")') == []
    assert markup_without_opt_in('logger.info("[docker] up")') == []
    assert markup_without_opt_in(r'logger.info(r"\[bold] literally")') == []
    assert markup_without_opt_in('logger.info("%s [%d]", a, b)') == []


def test_no_plain_log_call_escapes_markup():
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if (lines := escapes_without_opt_in(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        f"log calls that escape Rich markup but do not opt in to it: {offenders} -- "
        "the record is printed as written, so the escape shows; drop it"
    )


def test_the_escape_scan():
    """An escape is flagged on a plain record and allowed on an opted-in one."""
    assert escapes_without_opt_in(r'logger.info(r"\[docker] up")') == [1]
    assert escapes_without_opt_in('logger.error("%s", escape_markup(str(e)))') == [1]
    assert escapes_without_opt_in("logger.info(escape_relayed(out))") == [1]
    assert escapes_without_opt_in('logger.error("%s", rich.markup.escape(str(e)))') == [1]
    assert escapes_without_opt_in('logger.error("%s", markup.escape(str(e)))') == [1]
    assert escapes_without_opt_in('logger.info("[docker] up")') == []
    assert escapes_without_opt_in('logger.info(escape_relayed(o), extra={"markup": True})') == []


def test_every_escaping_markup_call_turns_emoji_off():
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in sorted(SRC.rglob("*.py"))
        if (lines := escaped_text_keeps_emoji(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        f"opted-in log calls that escape text but keep emoji on: {offenders} -- "
        'pass extra={"markup": True, "emoji": False}'
    )


def test_the_escape_emoji_scan():
    """An opted-in call that escapes must also turn emoji off."""
    assert escaped_text_keeps_emoji('logger.warning(escape(m), extra={"markup": True})') == [1]
    assert escaped_text_keeps_emoji(
        'logger.info(f"[b]{rich.markup.escape(n)}", extra={"markup": True})'
    ) == [1]
    assert (
        escaped_text_keeps_emoji('logger.info(escape(m), extra={"markup": True, "emoji": False})')
        == []
    )
    assert escaped_text_keeps_emoji('logger.info("[b]x", extra={"markup": True})') == []
    assert escaped_text_keeps_emoji("logger.info(escape(m))") == []
