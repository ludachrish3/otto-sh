"""Answer a bash TAB from the completion cache with the standard library alone.

Spec: docs/superpowers/specs/2026-09-04-shim-completion-design.md, section 4. This
module is imported by ``otto._shim`` BEFORE anything else in otto, on every TAB,
so it imports ``hashlib``, ``json``, ``os``, ``re``, ``shlex``, ``sys``, ``time``
and ``typing`` (already loaded by ``otto/__init__``, so free) and nothing else: not
``dataclasses`` (which drags in ``inspect``, ``dis`` and ``ast``; measured +74 file
syscalls per TAB on an NFS home) and not ``pathlib`` (``os.path`` by design;
``.ruff.toml`` exempts this file from ``PTH``). ``subprocess`` is imported only by
:func:`spawn_refresh`, after the answer is written. ``tests/unit/test_shim.py`` pins
the warm module set; the ``completion_repo_warm`` budget surface denies typer, click,
rich and pydantic. Every function that mirrors product code names what it mirrors;
change both or neither: ``tests/unit/shim/test_differential.py`` is the net.

A test-name or ``-m`` TAB (a *tests site*) is answered from each repo's per-file
table (``otto.config.collected_tests``), which pytest's collections write, with
the names pytest last recorded, and without a ``stat`` of anything the table
tracks: its file operations do not grow with the corpus. Once
:data:`CHECK_WINDOW_SECONDS` have passed since the tables were last checked, the
collect child is started behind the answer; it stats what they track and
re-reads what moved (design 2026-09-27 §9.2, as Chris revised it on
2026-09-28). A repo with no table, a table whose ``env`` moved or one past its
TTL hands over: only a pytest collection can seed it.

The parser mirrored here is Typer's vendored click (``typer._click``); each rule
is cited by the function it lives in there.

Any case this module cannot answer raises :class:`Handover`; the caller then runs
today's path in-process, which is always right.
"""

import hashlib
import json
import os
import re
import shlex
import sys
import time
from typing import Any

CACHE_FILENAME = "completion_cache.json"
SCHEMA = 24
"""Must equal ``otto.config.completion_cache.SCHEMA_VERSION`` (pinned by tests/unit/shim)."""
WINDOW_SECONDS = 60
"""How long the ``names`` marker vouches for the ``names`` key set."""
CHECK_WINDOW_SECONDS = 10 * 60
"""How long after the test tables were last checked a TAB starts the next check (Chris,
2026-09-28). The ``tests`` marker records the check: the collect child's start, and every
write of a table (each writer classified the whole table first)."""
MARKER_FILENAMES = {"names": "completion_cache.names.ok", "tests": "completion_cache.tests.ok"}
TABLES_KEY = "__collected_tests__"
TABLE_SCHEMA = 5
"""``otto.config.collected_tests.RECORDS_SCHEMA_VERSION``."""
TABLE_TTL_SECONDS = 24 * 60 * 60
"""``otto.config.completion_cache.CACHE_TTL_SECONDS``, which a table's ``generated_at`` obeys."""
SETTINGS_RELPATH = os.path.join(".otto", "settings.toml")
"""``otto.config.repo.TOML_SETTINGS_PATH``: the ``settings`` stat of a table's ``env``."""
COLLECT_LOCK_FILENAME = ".completion_collect.lock"
COLLECT_LOCK_STALE_SECONDS = 15 + 30
COLLECT_COOLDOWN_FILENAME = ".completion_collect.failed"
COLLECT_COOLDOWN_SECONDS = 60
DUMP_TESTS_ENV_VAR = "_OTTO_DUMP_TEST_NAMES"
COLLECT_OWNER_ENV_VAR = "_OTTO_COLLECT_OWNER"
_CHILD_PATH_VARS = ("OTTO_SUT_DIRS", "OTTO_XDIR", "OTTO_HOME")
_NORMALIZE_RE = re.compile(r"[-_.]+")
_PATH_LIST_SEP = re.compile(rf"[,{re.escape(os.pathsep)}]")
_MARKER_KEYWORDS = frozenset({"and", "or", "not"})


# A control-flow signal the caller catches, not an error: no "Error" suffix.
class Handover(Exception):  # noqa: N818
    """The shim cannot answer this TAB; the reason is for `otto cache info` and tests.

    ``stale`` is True when the CACHE is what failed (missing, expired, or a key
    path moved; on a tests site, a repo's test table missing, expired or its
    ``env`` moved), so the full path should rebuild or seed it; False when the
    cache is fine and this TAB is simply one the shim does not model.
    """

    def __init__(self, reason: str, *, stale: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.stale = stale


# --- mirrors -----------------------------------------------------------------


def split_arg_string(string: str) -> list[str]:
    """Split as ``typer._click.shell_completion.split_arg_string`` does, verbatim."""
    lex = shlex.shlex(string, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    out: list[str] = []
    try:
        # verbatim mirror: on an incomplete escape or quote the partial token is kept
        for token in lex:
            out.append(token)  # noqa: PERF402
    except ValueError:
        out.append(lex.token)
    return out


def workspace_key(sut_dirs: list[str]) -> str:
    """Mirror ``otto.config.home.workspace_key`` over raw ``OTTO_SUT_DIRS`` entries.

    The product sorts ``Path`` objects, which compare component-wise
    (``PurePath.__lt__``); a plain string sort orders by raw characters and so
    disagrees whenever a character below ``/`` (0x2F) -- notably ``-`` or ``.``
    -- appears where the shorter path ends a segment (e.g. ``a-b`` sorts before
    ``a/b`` as strings, but as components ``a`` < ``a-b`` puts ``a/b`` first).
    Splitting on ``os.sep`` before sorting reproduces the component-wise order
    without importing ``pathlib``.
    """
    resolved = sorted(
        {os.path.realpath(os.path.expanduser(d)) for d in sut_dirs}, key=lambda p: p.split(os.sep)
    )
    digest = hashlib.sha256("\n".join(resolved).encode()).hexdigest()
    names = "-".join(os.path.basename(p) for p in resolved)
    slug = _NORMALIZE_RE.sub("-", names.lower()) if resolved else "no-repos"
    return f"{digest[:8]}-{slug[:40]}"


def complete_separated_list(candidates: list[str], incomplete: str, sep: str = ",") -> list[str]:
    """Mirror ``otto.utils.complete_separated_list``, verbatim."""
    head, found, frag = incomplete.rpartition(sep)
    already = set(head.split(sep)) if found else set()
    prefix = head + found
    return [prefix + c for c in candidates if c.startswith(frag) and c not in already]


def complete_marker_expression(candidates: list[str], incomplete: str) -> list[str]:
    """Mirror ``otto.utils.complete_marker_expression``, verbatim."""
    cut = max(incomplete.rfind(" "), incomplete.rfind("\t"), incomplete.rfind("("))
    head, tail = incomplete[: cut + 1], incomplete[cut + 1 :]
    return [head + c for c in candidates if c.startswith(tail) and c not in _MARKER_KEYWORDS]


def parse_lab_values(values: list[str]) -> list[str] | None:
    """Mirror ``otto.cli.main.parse_lab_selection``: split on ``+`` and strip; empty means None."""
    if not values:
        return None
    labs: list[str] = []
    for value in values:
        for segment in value.split("+"):
            name = segment.strip()
            if not name:
                return None  # click swallows the callback's error under resilient parsing
            labs.append(name)
    return labs


def selected_labs(root_lab_values: list[str] | None, environ: dict[str, str]) -> list[str]:
    """Mirror ``otto.cli.completers.selected_lab_names``: ``ctx.params["labs"]`` as click fills it.

    The flag wins when given (a malformed value leaves the param ``None``: no env
    fallback, click already consumed the flag); else ``OTTO_LAB``, whitespace-split
    by click's ``multiple`` envvar rule, then ``+``-split by the callback. An empty
    envvar is unset.
    """
    if root_lab_values is not None:
        return parse_lab_values(root_lab_values) or []
    return parse_lab_values(environ.get("OTTO_LAB", "").split()) or []


# --- the walk (spec section 4.3) ------------------------------------------------


class Resolution:
    """Where the complete words (everything before the fragment) left the parser."""

    __slots__ = (
        "dashdash",
        "given",
        "host_id",
        "last_token",
        "node",
        "positionals",
        "root_lab_values",
        "seen_dashdash",
        "term",
    )

    def __init__(
        self,
        node: dict[str, Any],
        *,
        host_id: str | None = None,
        root_lab_values: list[str] | None = None,
        term: str | None = None,
    ) -> None:
        self.node = node
        self.given: set[str] = set()  # options given a value on THIS command (COMMANDLINE source)
        self.positionals = 0  # positionals consumed on this command
        self.dashdash = False  # THIS command's parser saw `--`: its option parsing is over
        self.seen_dashdash = False  # `"--" in args`, textual: set once by resolve()
        self.host_id = host_id
        self.root_lab_values = root_lab_values
        self.term = term
        self.last_token: str | None = None  # last complete word: click's textual pending rule


def _options(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in node["params"] if p["flags"]]


def _positionals(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in node["params"] if not p["flags"]]


def _reject_multi_value(param: dict[str, Any]) -> None:
    """Hand over for an option that takes more than one word (spec section 10).

    click pops ``nargs`` words for it (``parser._get_value_from_state``) and looks
    ``nargs`` words BACK for the pending-value rule
    (``shell_completion._is_incomplete_option``); the shim models exactly one on
    both paths. No otto option has ``nargs > 1`` today, but ``param_synth``
    synthesises options from a third-party ``@otto.options`` class, where a
    ``tuple[int, int]`` field would produce one.
    """
    if param["nargs"] > 1:
        raise Handover(f"nargs={param['nargs']} option {param['name']}")


def _match_option(node: dict[str, Any], word: str) -> tuple[dict[str, Any], str | None]:
    """Mirror ``typer._click.parser``: an exact long flag, ``--long=value``, ``-svalue``.

    click matches NO prefix of a long option (``--la`` is ``NoSuchOption``) and
    stacks short flags (``-hv``); the shim hands over on both rather than guess.
    """
    options = _options(node)
    if word.startswith("--"):
        name, has_eq, value = word.partition("=")
        for param in options:
            if name in param["flags"]:
                _reject_multi_value(param)
                return param, (value if has_eq else None)
        raise Handover(f"unknown option {word!r}")
    name, value = word[:2], word[2:]
    for param in options:
        if name in param["flags"]:
            if value and not param["takes_value"]:
                raise Handover(f"stacked short flags {word!r}")
            _reject_multi_value(param)
            return param, (value or None)
    raise Handover(f"unknown option {word!r}")


def _view(
    tree: dict[str, Any], node: dict[str, Any], res: Resolution, classes: dict[str, str]
) -> dict[str, Any]:
    """Pick *node*'s subcommand map: the host's class view when the typed id's class is known."""
    if node.get("scoped_by") and res.host_id is not None:
        view = tree.get("host_classes", {}).get(classes.get(res.host_id))
        if isinstance(view, dict):
            return view
    return node["commands"]


def resolve(tree: dict[str, Any], args: list[str], classes: dict[str, str]) -> Resolution:
    """Walk the complete words before the fragment; every unknown construct hands over.

    TWO parsers, not one. ``typer.core.TyperGroup`` sets
    ``allow_interspersed_args = False`` (typer/core.py:999) where a leaf
    ``TyperCommand`` inherits click's ``True`` (typer/_click/core.py:517), and
    ``typer._click.parser._OptionParser._process_args_for_options`` reads that flag:
    at a GROUP it stops at the FIRST word that is not an option (pushing it back),
    then ``_process_args_for_args`` fills the group's own arguments from what is left
    and ``TyperGroup.parse_args`` hands the remainder to ``resolve_command``. So an
    option-looking word AFTER a group's positional is never parsed as an option —
    ``resolve_command`` just finds no subcommand by that name and
    ``shell_completion._resolve_context`` leaves the context ON the group. That is why
    ``otto host dut1 --term <TAB>`` completes ``--term``'s values off the host GROUP
    (its textual ``_is_incomplete_option`` rule) and why ``otto host dut1 --help <TAB>``
    still offers ``--help``: the group's parser never saw it, so its parameter source
    is not ``COMMANDLINE``. At a LEAF options and positionals interleave freely.
    """
    res = Resolution(tree)
    index, total = 0, len(args)
    while index < total:
        # Indexed, never `.get`: "group" is a REQUIRED Node key, like "params" and
        # "commands". A payload without it (one written before the key existed,
        # under the same SCHEMA) must raise and hand over, not silently parse every
        # group with leaf semantics. `.get` is for the genuinely optional keys below
        # ("scoped_by", "host_classes").
        is_group = res.node["group"]
        while index < total:  # _process_args_for_options
            word = args[index]
            if not res.dashdash and word == "--":
                index += 1
                res.dashdash = True
                break
            if not res.dashdash and word.startswith("-") and len(word) > 1:
                param, value = _match_option(res.node, word)
                index += 1
                if not param["takes_value"]:
                    if value is not None:
                        # `--flag=value` (and bare `--flag=`): `_match_long_opt` raises
                        # BadOptionUsage (parser.py:360-361), and resilient parsing
                        # swallows it in `_OptionParser.parse_args` (parser.py:284-291)
                        # AFTER discarding `rargs` and without running
                        # `_process_args_for_args` — the WHOLE parse of that command is
                        # over, so every later word is dropped and even the positionals
                        # already seen are not recorded. Not modelled: hand over.
                        raise Handover(f"value given to flag {word!r}")
                    res.given.add(param["name"])
                elif value is not None:
                    _note_value(res, param, value)
                elif index < total:
                    _note_value(res, param, args[index])  # the value is the next word
                    index += 1
                # else: the value never arrived. click raises BadOptionUsage, which
                # resilient parsing swallows, and the option is NOT given.
                continue
            if is_group:
                break  # allow_interspersed_args=False: the parser stops here
            _consume_positional(res, word)
            index += 1
        if index >= total:
            break
        if not is_group:
            continue  # a leaf that just read `--`: the rest are positionals
        for param in _positionals(res.node):  # _process_args_for_args, this group's own
            if index >= total:
                break
            if param["nargs"] > 1:
                raise Handover(f"nargs={param['nargs']} positional {param['name']}")
            if res.node.get("scoped_by") == param["name"]:
                res.host_id = args[index]
            if param["nargs"] == -1:
                index = total  # the variadic absorbs the rest; no subcommand can follow
                break
            res.positionals += 1
            index += 1
        if index >= total:
            break
        child = _descend(tree, res, args[index], classes)
        if child is None:
            break  # the context stays on this group; click ignores the leftover words
        res = child
        index += 1
    res.last_token = args[-1] if args else None
    # click's fragment rule reads the WHOLE line textually (`if "--" not in args`),
    # not what a parser consumed: a `--` sitting in a group's UNRESOLVED leftover
    # (`otto host dut1 -- <TAB>`) still suppresses option-name completion.
    res.seen_dashdash = "--" in args
    return res


def _consume_positional(res: Resolution, word: str) -> None:
    """Give *word* to this command's next argument (click's ``_process_args_for_args``)."""
    positionals = _positionals(res.node)
    if res.positionals < len(positionals):
        param = positionals[res.positionals]
        if param["nargs"] > 1:
            raise Handover(f"nargs={param['nargs']} positional {param['name']}")  # not modelled
        if res.node.get("scoped_by") == param["name"]:
            res.host_id = word
        if param["nargs"] != -1:
            res.positionals += 1
        return
    if positionals and positionals[-1]["nargs"] == -1:
        return  # the variadic absorbs everything that is not an option
    raise Handover(f"unknown command {word!r}")


def _descend(
    tree: dict[str, Any], res: Resolution, word: str, classes: dict[str, str]
) -> "Resolution | None":
    """Resolve *word* as this group's subcommand (``TyperGroup.resolve_command``).

    ``None`` means the walk stops HERE with the context still on this group.
    """
    if word == "--" or (word.startswith("-") and len(word) > 1):
        # No command is named `--x`, so resolve_command returns None and (under
        # resilient parsing) the context STAYS on this group. Modelled, not unknown.
        return None
    child = _view(tree, res.node, res, classes).get(word)
    if child is None:
        raise Handover(f"unknown command {word!r}")
    return Resolution(
        child, host_id=res.host_id, root_lab_values=res.root_lab_values, term=res.term
    )


def _note_value(res: Resolution, param: dict[str, Any], value: str) -> None:
    """Record a value-taking option's value: NOW it is given (click: COMMANDLINE source)."""
    res.given.add(param["name"])
    if param["name"] == "labs" and res.node["name"] == "otto":
        res.root_lab_values = [*(res.root_lab_values or []), value]
    if param["name"] == "term" and res.node.get("scoped_by"):
        # the host group's --term: `--user` narrows by it (cli.completers.filter_logins)
        res.term = value


# --- the answer (spec sections 3.4 and 4.3, fragment rules) ----------------------


class TestNames:
    """What the test tables offer a tests site, and whether a check of them is due."""

    __slots__ = ("check_due", "markers", "names")

    def __init__(self, names: list[str], markers: list[str], check_due: bool) -> None:
        self.names = names  # sorted, each once
        self.markers = markers  # sorted, each once
        self.check_due = check_due  # the collect child should stat what the tables track


class Payloads:
    """Payloads an answer reads: ``names`` always; the test tables' answer on a tests site."""

    __slots__ = ("names", "tests")

    def __init__(self, names: dict[str, Any], tests: TestNames | None = None) -> None:
        self.names = names
        self.tests = tests


def site_of(source: dict[str, Any]) -> str:
    """Name the key set a source reads: ``tests`` for the tests/markers kinds, else ``names``."""
    return "tests" if source.get("kind") in ("tests", "markers") else "names"


def _lab_host_set(names: dict[str, Any], labs: list[str], always: list[str]) -> set[str]:
    """Mirror ``lab_scoped_host_ids`` with a lab selected: built-ins plus the labs' buckets."""
    by_lab = names.get("hosts_by_lab", {})
    hosts = set(always)
    for lab in labs:
        hosts.update(by_lab.get(lab, []))
    return hosts


def _host_logins(
    source: dict[str, Any], names: dict[str, Any], host_id: str | None, term: str | None
) -> list[str]:
    """Mirror ``otto.cli.completers.filter_logins`` for the typed host, before the prefix cut.

    A login may appear under several protocol-scoped entries — one entry per
    login in the result.
    """
    if not host_id:
        return []
    entries = names.get("logins_by_host", {}).get(host_id, [])
    flavour = source.get("flavour")
    out = []
    for e in entries:
        if flavour == "direct" and e.get("proxy"):
            continue
        protocols = e.get("protocols") or []
        if source.get("term_scoped") and term and protocols and term not in protocols:
            continue
        login = str(e.get("login", ""))
        if login:
            out.append(login)
    return list(dict.fromkeys(out))


def _payload_values(
    source: dict[str, Any],
    names: dict[str, Any],
    labs: list[str],
    host_id: str | None = None,
    term: str | None = None,
) -> list[str]:
    if source.get("host_scoped"):
        values = _host_logins(source, names, host_id, term)
        return sorted(values) if source.get("sort") else values
    key = source["key"]
    scoped = None
    if labs and source.get("lab_scoped"):
        scoped = _lab_host_set(names, labs, source.get("always", []))
    if key == "hosts":
        values = sorted(scoped) if scoped is not None else list(names.get("hosts", []))
    elif key == "docker_hosts":
        values = [str(h) for h in names.get("docker_hosts", [])]
        if scoped is not None:
            values = [h for h in values if h in scoped]
    elif key == "links":
        values = [
            str(e["id"])
            for e in names.get("links", [])
            if scoped is None or any(h in scoped for h in e.get("hosts", []))
        ]
    elif key == "transfer_backends":
        values = [
            str(e["name"])
            for e in names.get("transfer_backends", [])
            if source.get("family") in e.get("host_families", [])
        ]
    else:
        values = [str(v) for v in names.get(key, [])]
    return sorted(values) if source.get("sort") else values


def _tests(payloads: Payloads) -> TestNames:
    if payloads.tests is None:
        raise Handover("no tables read for a tests site")
    return payloads.tests


def _source_values(
    param: dict[str, Any],
    frag: str,
    labs: list[str],
    payloads: Payloads,
    res: "Resolution | None" = None,
) -> list[str]:
    """Produce a parameter's candidates; each kind filters by the fragment as a prefix."""
    source = param["source"]
    kind = source.get("kind")
    if kind == "live":
        raise Handover(f"live source for {param['name']}")
    if kind == "none":
        return []
    if kind == "echo":
        return [frag]
    if kind == "static":
        if source.get("match_case"):
            # Answer in the fragment's case (lower-case fragment -> lower-case
            # names), then Typer's own prefix filter, which drops a mixed-case
            # fragment's upper-case answer: the completer's exact contract.
            low = frag.lower()
            hits = [v for v in source["values"] if v.lower().startswith(low)]
            answered = [v.lower() for v in hits] if frag.islower() else hits
            return [v for v in answered if v.startswith(frag)]
        if source.get("case_sensitive", True):
            return [v for v in source["values"] if v.startswith(frag)]
        low = frag.lower()
        return [v for v in source["values"] if v.lower().startswith(low)]
    if kind == "tests":
        names = _tests(payloads).names
        sep = source.get("sep")
        if sep:
            return complete_separated_list(names, frag, sep)
        return [n for n in names if n.startswith(frag)]
    if kind == "markers":
        return complete_marker_expression(_tests(payloads).markers, frag)
    if kind == "payload":
        sep = source.get("sep")
        if sep and source.get("live_past_sep") and sep in frag:
            raise Handover("list fragment past its first separator")
        values = _payload_values(
            source, payloads.names, labs, res.host_id if res else None, res.term if res else None
        )
        if sep:
            return complete_separated_list(values, frag, sep)
        return [v for v in values if v.startswith(frag)]
    raise Handover(f"unknown source kind {kind!r}")


def _target(res: Resolution, frag: str) -> tuple[str, dict[str, Any] | None, str]:
    """Mirror ``typer._click.shell_completion._resolve_incomplete``: decide what *frag* completes.

    ``("param", param, frag)`` for a parameter's values; ``("command", None, frag)``
    for the command's own menu. click's rules, in click's order: a bare ``=`` is an
    empty fragment; ``--x=val`` splits into the pending option's name and the value
    part, and click APPENDS that name to ``args`` rather than skipping its remaining
    rules, so the option-name rule below still runs on the value part (``--lab=-``
    completes option NAMES) while the pending rule, which reads ``args[-1]``, is
    answered by the appended name itself and never by the previous word (that is
    what ``eq`` suppresses); a fragment starting with ``-`` is an option NAME unless
    ``--`` appeared anywhere on the line; else the option named by the LAST complete
    word, read textually (``otto --xdir --lab <TAB>`` completes labs although the
    parser took ``--lab`` as ``--xdir``'s value); else the first positional that is
    variadic or not yet given; else the command.
    """
    node = res.node
    pending = None
    eq = False
    if frag == "=":
        frag = ""
    elif "=" in frag and frag.startswith("-"):
        name, _, frag = frag.partition("=")
        param, _ = _match_option(node, name)
        pending = param if param["takes_value"] else None
        eq = True
    if frag.startswith("-") and not res.seen_dashdash:
        return "command", None, frag  # runs after the split too: `args.append(name)` is not `--`
    if not eq and res.last_token is not None and res.last_token.startswith("-"):
        for param in _options(node):  # _is_incomplete_option: args[-1] in param.opts
            if param["takes_value"] and res.last_token in param["flags"]:
                _reject_multi_value(param)
                pending = param
                break
    if pending is not None:
        return "param", pending, frag
    for index, param in enumerate(_positionals(node)):
        if param["nargs"] > 1:
            raise Handover(f"nargs={param['nargs']} positional {param['name']}")
        if param["nargs"] == -1 or index >= res.positionals:
            return "param", param, frag
    return "command", None, frag


def complete(
    tree: dict[str, Any], res: Resolution, frag: str, environ: dict[str, str], payloads: Payloads
) -> list[str]:
    """Compute the candidates for *frag* after the walk, in Typer's order."""
    labs = selected_labs(res.root_lab_values, environ)
    kind, param, frag = _target(res, frag)
    if kind == "param" and param is not None:
        return _source_values(param, frag, labs, payloads, res=res)
    # TyperGroup.shell_complete: visible subcommands by prefix, then Command.shell_complete's
    # option names for a non-alphanumeric fragment (given non-multiple options excluded).
    view = _view(tree, res.node, res, payloads.names.get("host_classes_by_id", {}))
    items = [name for name in view if name.startswith(frag)]
    if frag and not frag[0].isalnum():
        items.extend(
            flag
            for p in _options(res.node)
            if p["multiple"] or p["name"] not in res.given
            for flag in p["flags"]
            if flag.startswith(frag)
        )
    return items


# --- locate and validate (spec sections 4.1-4.2) --------------------------------


def locate_cache(environ: dict[str, str]) -> str:
    """Locate ``<OTTO_HOME or ~/.otto>/<workspace_key>/completion_cache.json`` from *environ*."""
    raw = environ.get("OTTO_SUT_DIRS", "")
    sut_dirs = [p for p in _PATH_LIST_SEP.split(raw) if p]
    if not sut_dirs:
        raise Handover("no SUT dirs")
    home = environ.get("OTTO_HOME") or os.path.join(os.path.expanduser("~"), ".otto")
    return os.path.join(os.path.expanduser(home), workspace_key(sut_dirs), CACHE_FILENAME)


def servable_shim(data: Any, now: float) -> dict[str, Any]:
    """Return the shim payload iff the file, schema, taint and TTL allow (spec 4.2 step 1)."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise Handover("schema mismatch", stale=True)
    sections = data.get("sections")
    if not isinstance(sections, dict):
        raise Handover("no sections", stale=True)
    shim = sections.get("shim")
    if not isinstance(shim, dict) or not isinstance(shim.get("payload"), dict):
        raise Handover("no shim section", stale=True)
    if shim.get("tainted"):
        raise Handover("tainted")
    at = shim.get("generated_at")
    payload = shim["payload"]
    ttl = payload.get("ttl_seconds")
    if not isinstance(at, (int, float)) or not isinstance(ttl, (int, float)) or now - at > ttl:
        raise Handover("expired", stale=True)
    names = sections.get("names")
    if not isinstance(names, dict) or not isinstance(names.get("payload"), dict):
        raise Handover("no names section", stale=True)
    return payload


def _stat_pass(triples: list[Any]) -> None:
    for path, mtime_ns, size in triples:
        try:
            st = os.stat(path)
        except OSError:
            if mtime_ns is None:
                continue
            raise Handover(f"stale: {path} is gone", stale=True) from None
        if mtime_ns is None:
            raise Handover(f"stale: {path} appeared", stale=True)
        if st.st_mtime_ns != mtime_ns or st.st_size != size:
            raise Handover(f"stale: {path} changed", stale=True)


def _inventory_pass(block: Any) -> None:
    kind = block.get("kind") if isinstance(block, dict) else None
    if kind == "none":
        return
    if kind == "stat":
        _stat_pass(block.get("files", []))
        return
    raise Handover("opaque inventory")


def _marker_fresh(marker: str, cache_mtime_ns: int, now: float) -> bool:
    try:
        st = os.stat(marker)
    except OSError:
        return False
    # A marker dated after `now` (the clock stepped back) is not "under a minute old".
    return st.st_mtime_ns >= cache_mtime_ns and 0 <= now - st.st_mtime < WINDOW_SECONDS


def _touch(marker: str) -> bool:
    """Touch *marker*, creating it if need be; ``False`` when it cannot be written."""
    # No contextlib.suppress: that import would be paid on every TAB.
    try:
        os.utime(marker, None)
    except FileNotFoundError:
        try:
            with open(marker, "a", encoding="utf-8"):
                pass
        except OSError:
            return False
    except OSError:
        return False
    return True


def _check_names(payload: dict[str, Any], cache_dir: str, cache_mtime_ns: int, now: float) -> str:
    """Check the ``names`` key set and the inventory, unless the marker window vouches for them."""
    marker = os.path.join(cache_dir, MARKER_FILENAMES["names"])
    if _marker_fresh(marker, cache_mtime_ns, now):
        return "marker"
    _stat_pass(payload["keys"])
    _inventory_pass(payload.get("inventory"))
    _touch(marker)
    return "stat"


def validate_keys(cache_path: Any, data: dict[str, Any], now: float) -> str:
    """Validate the ``names`` key set (spec 4.2 steps 2-3).

    Returns ``"stat"`` if it needed a full stat pass (after which its marker
    is touched), else ``"marker"``.
    """
    payload = servable_shim(data, now)
    cache_path = str(cache_path)
    # The caller READ the file before this stat, so a rewrite landing between the
    # two makes this mtime the NEW entry's while `data` is the old one, and a
    # marker written after it looks fresh: one TAB can be answered from data up to
    # the window old. That is the window's own contract -- a fresh marker vouches
    # for nothing newer than WINDOW_SECONDS anyway -- and the next TAB re-stats,
    # so closing it would cost a second read for a staleness the design accepts.
    cache_mtime_ns = os.stat(cache_path).st_mtime_ns
    return _check_names(payload, os.path.dirname(cache_path), cache_mtime_ns, now)


# --- the test tables (design 2026-09-27 §9.2) -------------------------------------


def _stat_pair(path: str) -> list[int] | None:
    """Mirror ``otto.config.collected_tests._stat_pair``: ``[mtime_ns, size]``, or ``None``."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def selectable_names(classes: list[str], name: str) -> list[str]:
    """Mirror ``otto.config.repo.selectable_names``, verbatim."""
    base = name.partition("[")[0]
    return [base, *classes, *([f"{classes[-1]}::{base}"] if classes else [])]


def _python_version() -> str:
    major, minor, micro = sys.version_info[:3]
    return f"{major}.{minor}.{micro}"


def _env_pass(sut_dir: str, env: dict[str, Any], stat: Any) -> None:
    """Mirror ``classify``'s ``env`` comparison (``collected_tests.current_env``), by stat.

    The interpreter's own fields are compared by value: the Python version,
    and ``sys.prefix``, the venv (worktree venvs share one Python and one
    ``OTTO_HOME``, and each keeps its own table). The pytest and otto versions
    are not read here (that needs the installed distributions' metadata):
    installing either moves the stat of the site-packages directory it lands
    in, which is.
    """
    for field, now in (("python", _python_version()), ("prefix", sys.prefix)):
        if env.get(field) != now:
            raise Handover(f"test-names cache env moved: {field}", stale=True)
    watched = [
        *env["site_packages"].items(),
        *env["configs"].items(),
        (os.path.join(sut_dir, SETTINGS_RELPATH), env["settings"]),
    ]
    for path, before in watched:
        if stat(path) != before:
            raise Handover(f"test-names cache env moved: {path}", stale=True)


def _tables(data: dict[str, Any], payload: dict[str, Any], now: float) -> list[Any]:
    """Every repo's table, in the order the payload lists the repos; a cold one hands over."""
    namespace = data.get(TABLES_KEY)
    namespace = namespace if isinstance(namespace, dict) else {}
    tables = []
    for sut_dir in payload["tables"]:
        table = namespace.get(sut_dir)
        if not isinstance(table, dict) or table.get("schema_version") != TABLE_SCHEMA:
            raise Handover(f"no test-names cache for {sut_dir}", stale=True)
        at = table.get("generated_at")
        if not isinstance(at, int) or now - at > TABLE_TTL_SECONDS:
            raise Handover(f"test-names cache expired for {sut_dir}", stale=True)
        tables.append((sut_dir, table))
    return tables


def _check_age(marker: str, now: float) -> float | None:
    """Seconds since the tables were last checked, or ``None`` with no record of it.

    A marker dated after *now* (the clock stepped back) is no record.
    """
    try:
        age = now - os.stat(marker).st_mtime
    except OSError:
        return None
    return age if age >= 0 else None


def table_view(cache_path: str, data: dict[str, Any], now: float) -> TestNames:
    """Answer a tests site from the tables, statting nothing they track.

    Mirrors ``collected_tests.completion_view`` for a warm table.
    Every record offers what pytest last recorded in it, so a file edited,
    added or deleted since shows up one check later: the O(corpus) stat pass
    is the detached collect child's (``classify``), started when
    :data:`CHECK_WINDOW_SECONDS` have passed since the tables were last
    checked (:attr:`TestNames.check_due`). Every table's ``env`` is checked
    here, a handful of ``stat`` calls whatever the corpus size, because a
    table another interpreter, venv or pytest config wrote vouches for
    nothing.
    """
    payload = data["sections"]["shim"]["payload"]
    tables = _tables(data, payload, now)
    names: set[str] = set()
    markers: set[str] = set()
    for sut_dir, table in tables:
        _env_pass(sut_dir, table["env"], _stat_pair)
        markers.update(table["registered_markers"])
        for record in table["files"].values():
            markers.update(record["markers"])
            for classes, name in record["tests"]:
                names.update(selectable_names(classes, name))
    marker = os.path.join(os.path.dirname(cache_path), MARKER_FILENAMES["tests"])
    age = _check_age(marker, now)
    return TestNames(sorted(names), sorted(markers), age is None or age >= CHECK_WINDOW_SECONDS)


# --- the check behind the answer (design 2026-09-27 §9.2, Chris 2026-09-28) ------


def _younger_than(path: str, seconds: float, now: float) -> bool:
    """Mirror ``otto.config.completion_cache._younger_than`` (one ``stat``)."""
    try:
        return now - os.stat(path).st_mtime <= seconds
    except OSError:
        return False


def _child_environ(environ: dict[str, str]) -> dict[str, str]:
    """Mirror ``otto.config.completion_cache._collect_child_command``'s environment."""
    env = dict(environ)
    for var in ("_OTTO_COMPLETE", "COMP_WORDS", "COMP_CWORD"):
        env.pop(var, None)
    for var in _CHILD_PATH_VARS:
        if env.get(var):
            paths = [os.path.abspath(p) for p in _PATH_LIST_SEP.split(env[var]) if p]
            env[var] = os.pathsep.join(paths)
    env[DUMP_TESTS_ENV_VAR] = "1"
    env[COLLECT_OWNER_ENV_VAR] = f"{os.getpid()}-{time.monotonic_ns()}"
    return env


_started: list[Any] = []
"""The detached children this process started, held so none is reported as a leak."""


def spawn_refresh(cache_dir: str, environ: dict[str, str], now: float | None = None) -> bool:
    """Start the collect child detached; ``True`` when one started. Never raises.

    Mirrors ``otto.config.completion_cache.spawn_collect_child``: skipped
    while a collect child holds a fresh lock (the lock's ``stat`` first, so a
    burst of TABs starts one child) and during the cooldown after a failure.
    The child is the venv's ``otto`` in the collect mode, in its own session,
    with ``/dev/null`` for every stream and no inherited descriptors; it
    stats what the tables track, re-reads what moved and rewrites the tables
    for the next TAB. Starting it touches the ``tests`` marker, which starts
    the next :data:`CHECK_WINDOW_SECONDS`, so the TABs of a burst start one;
    a marker that cannot be touched starts no child at all.
    Called after the answer is written, which is why ``subprocess`` is
    imported here.
    """
    now = time.time() if now is None else now
    lock = os.path.join(cache_dir, COLLECT_LOCK_FILENAME)
    if _younger_than(lock, COLLECT_LOCK_STALE_SECONDS, now):
        return False
    cooldown = os.path.join(cache_dir, COLLECT_COOLDOWN_FILENAME)
    if _younger_than(cooldown, COLLECT_COOLDOWN_SECONDS, now):
        return False
    otto_bin = os.path.join(os.path.dirname(sys.executable), "otto")
    if not os.path.exists(otto_bin):
        return False
    if not _touch(os.path.join(cache_dir, MARKER_FILENAMES["tests"])):
        # With no record of this check, every TAB of a burst would start one.
        return False
    import subprocess

    try:
        child = subprocess.Popen(  # noqa: S603 — venv otto binary, fixed argv, no shell
            [otto_bin],
            env=_child_environ(environ),
            cwd=os.path.abspath(cache_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    _started.append(child)
    return True


# --- entry ---------------------------------------------------------------------


def _answer_items(environ: dict[str, str], now: float) -> tuple[list[str], str | None]:
    """Compute the candidates for this TAB and the cache dir to refresh, or raise :class:`Handover`.

    The second item is ``None`` unless a tests site found the tables' check due.
    """
    if environ.get("_OTTO_COMPLETE") != "complete_bash":
        raise Handover("not bash")
    words = split_arg_string(environ.get("COMP_WORDS", ""))
    cword = int(environ.get("COMP_CWORD", "1"))
    args = words[1:cword]
    frag = words[cword] if cword < len(words) else ""
    cache_path = locate_cache(environ)
    try:
        with open(cache_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError:
        raise Handover("no cache file", stale=True) from None
    payload = servable_shim(data, now)
    names = data["sections"]["names"]["payload"]
    res = resolve(payload["tree"], args, names.get("host_classes_by_id", {}))
    site = _site_for(res, frag)
    cache_mtime_ns = os.stat(cache_path).st_mtime_ns  # see validate_keys for the race
    _check_names(payload, os.path.dirname(cache_path), cache_mtime_ns, now)
    tests = table_view(cache_path, data, now) if site == "tests" else None
    items = complete(payload["tree"], res, frag, environ, Payloads(names, tests))
    refresh = os.path.dirname(cache_path) if tests is not None and tests.check_due else None
    return items, refresh


class Outcome:
    """What the shim decided for one TAB: the candidates, or ``None`` and the reason."""

    __slots__ = ("items", "reason", "refresh", "stale")

    def __init__(
        self,
        items: list[str] | None,
        reason: str = "",
        stale: bool = False,
        refresh: str | None = None,
    ) -> None:
        self.items = items
        self.reason = reason  # empty when answered; for `otto cache info` and tests otherwise
        self.stale = stale  # True iff the CACHE (not just this TAB) needs a rebuild
        # The cache dir whose collect child to start once the answer is written
        # (spawn_refresh); None when nothing the answer read has moved.
        self.refresh = refresh


def answer_or_reason(environ: dict[str, str], now: float | None = None) -> Outcome:
    """Compute the candidates for this TAB, or ``None`` and why the shim hands over."""
    try:
        items, refresh = _answer_items(environ, time.time() if now is None else now)
    except Handover as e:
        return Outcome(None, e.reason, e.stale)
    except Exception as e:  # noqa: BLE001
        # a TAB never tracebacks; the full path decides
        return Outcome(None, f"error: {type(e).__name__}: {e}")
    return Outcome(items, refresh=refresh)


def _site_for(res: Resolution, frag: str) -> str:
    """Name the key set the answer will read: the ONE target rule ``complete`` applies."""
    kind, param, _ = _target(res, frag)
    return site_of(param["source"]) if kind == "param" and param is not None else "names"


def inspect_shim(cache_path: Any, now: float | None = None) -> str:
    """Describe, for ``otto cache info``, whether the NEXT names-site TAB would be served."""
    now = time.time() if now is None else now
    try:
        try:
            with open(str(cache_path), encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise Handover("no cache file", stale=True) from None
        how = validate_keys(cache_path, data, now)
        if how == "stat":
            return "served (validated now)"
        marker = os.path.join(os.path.dirname(str(cache_path)), MARKER_FILENAMES["names"])
        ago = int(now - os.stat(marker).st_mtime)
    except Handover as e:
        return f"handing over — {e.reason}"
    except Exception as e:  # noqa: BLE001
        return f"handing over — error: {type(e).__name__}: {e}"
    return f"served (validated {ago}s ago)"


def inspect_tests(cache_path: Any, now: float | None = None) -> str:
    """Describe, for ``otto cache info``, what the NEXT test-name TAB would do.

    The same checks such a TAB runs (the ``names`` key set, then the test
    tables' ``env``), so a passing stat pass of the ``names`` key set touches
    the ``names`` marker as a TAB would; the ``tests`` marker is only read.
    The answer is served, with when the tables were last checked and when
    the next check is due, or handed over, and why. Whether a due check
    starts at once is the collect child's lock and cooldown to say, reported
    on their own line.
    """
    now = time.time() if now is None else now
    try:
        try:
            with open(str(cache_path), encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise Handover("no cache file", stale=True) from None
        validate_keys(cache_path, data, now)
        found = table_view(str(cache_path), data, now)
    except Handover as e:
        return f"handing over — {e.reason}"
    except Exception as e:  # noqa: BLE001
        return f"handing over — error: {type(e).__name__}: {e}"
    count = len(found.names)
    noun = "name" if count == 1 else "names"
    age = _check_age(os.path.join(os.path.dirname(str(cache_path)), MARKER_FILENAMES["tests"]), now)
    if age is None:
        when = "no check recorded, a check is due"
    elif found.check_due:
        when = f"last checked {int(age)}s ago, a check is due"
    else:
        left = int(CHECK_WINDOW_SECONDS - age)
        when = f"checked {int(age)}s ago, the next check is due in {left}s"
    return f"served ({count} {noun}; {when})"
