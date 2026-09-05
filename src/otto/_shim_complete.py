"""Answer a bash TAB from the completion cache with the standard library alone.

Spec: docs/superpowers/specs/2026-09-04-shim-completion-design.md, section 4. This
module is imported by ``otto._shim`` BEFORE anything else in otto, on every TAB,
so it imports ``hashlib``, ``json``, ``os``, ``re``, ``shlex``, ``time`` and
``typing`` (already loaded by ``otto/__init__``, so free) and nothing else: not
``dataclasses`` (which drags in ``inspect``, ``dis`` and ``ast``; measured +74 file
syscalls per TAB on an NFS home) and not ``pathlib`` (``os.path`` by design;
``.ruff.toml`` exempts this file from ``PTH``). ``tests/unit/test_shim.py`` pins the
warm module set; the ``completion_repo_warm`` budget surface denies typer, click,
rich and pydantic. Every function that mirrors product code names what it mirrors;
change both or neither: ``tests/unit/shim/test_differential.py`` is the net.

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
import time
from typing import Any

CACHE_FILENAME = "completion_cache.json"
SCHEMA = 18
"""Must equal ``otto.config.completion_cache.SCHEMA_VERSION`` (pinned by tests/unit/shim)."""
WINDOW_SECONDS = 60
MARKER_FILENAMES = {"names": "completion_cache.names.ok", "tests": "completion_cache.tests.ok"}
COLLECTED_KEY = "__collected_tests__"
COLLECTED_SCHEMA = 2
COLLECTED_TTL_SECONDS = 24 * 60 * 60
_NORMALIZE_RE = re.compile(r"[-_.]+")
_PATH_LIST_SEP = re.compile(rf"[,{re.escape(os.pathsep)}]")
_MARKER_KEYWORDS = frozenset({"and", "or", "not"})


# A control-flow signal the caller catches, not an error: no "Error" suffix.
class Handover(Exception):  # noqa: N818
    """The shim cannot answer this TAB; the reason is for `otto cache info` and tests."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    )

    def __init__(
        self,
        node: dict[str, Any],
        *,
        host_id: str | None = None,
        root_lab_values: list[str] | None = None,
    ) -> None:
        self.node = node
        self.given: set[str] = set()  # options given a value on THIS command (COMMANDLINE source)
        self.positionals = 0  # positionals consumed on this command
        self.dashdash = False  # THIS command's parser saw `--`: its option parsing is over
        self.seen_dashdash = False  # `"--" in args`, textual: set once by resolve()
        self.host_id = host_id
        self.root_lab_values = root_lab_values
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
        # "commands". A payload without it (one written before the key existed —
        # SCHEMA stays 18) must raise and hand over, not silently parse every group
        # with leaf semantics. `.get` is for the genuinely optional keys below
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
    return Resolution(child, host_id=res.host_id, root_lab_values=res.root_lab_values)


def _note_value(res: Resolution, param: dict[str, Any], value: str) -> None:
    """Record a value-taking option's value: NOW it is given (click: COMMANDLINE source)."""
    res.given.add(param["name"])
    if param["name"] == "labs" and res.node["name"] == "otto":
        res.root_lab_values = [*(res.root_lab_values or []), value]


# --- the answer (spec sections 3.4 and 4.3, fragment rules) ----------------------


class Payloads:
    """Payloads an answer reads: ``names`` always; ``tests`` and collected on a tests site."""

    __slots__ = ("collected", "names", "tests")

    def __init__(
        self,
        names: dict[str, Any],
        tests: dict[str, Any] | None = None,
        collected: dict[str, Any] | None = None,
    ) -> None:
        self.names = names
        self.tests = tests
        self.collected = collected


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


def _payload_values(source: dict[str, Any], names: dict[str, Any], labs: list[str]) -> list[str]:
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


def _collected(payloads: Payloads, what: str) -> list[str]:
    entry = payloads.collected
    if not isinstance(entry, dict):
        raise Handover("collected set cold")
    values = entry.get(what)
    if not isinstance(values, list):
        raise Handover("collected set cold")
    return [str(v) for v in values]


def _source_values(
    param: dict[str, Any], frag: str, labs: list[str], payloads: Payloads
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
        if source.get("case_sensitive", True):
            return [v for v in source["values"] if v.startswith(frag)]
        low = frag.lower()
        return [v for v in source["values"] if v.lower().startswith(low)]
    if kind == "tests":
        tests = payloads.tests or {}
        names = {str(t) for t in tests.get("tests", [])} | set(_collected(payloads, "names"))
        return complete_separated_list(sorted(names), frag, source.get("sep", ","))
    if kind == "markers":
        tests = payloads.tests or {}
        names = {str(m) for m in tests.get("markers", [])} | set(_collected(payloads, "markers"))
        return complete_marker_expression(sorted(names), frag)
    if kind == "payload":
        sep = source.get("sep")
        if sep and source.get("live_past_sep") and sep in frag:
            raise Handover("list fragment past its first separator")
        values = _payload_values(source, payloads.names, labs)
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
        return _source_values(param, frag, labs, payloads)
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
        raise Handover("schema mismatch")
    sections = data.get("sections")
    if not isinstance(sections, dict):
        raise Handover("no sections")
    shim = sections.get("shim")
    if not isinstance(shim, dict) or not isinstance(shim.get("payload"), dict):
        raise Handover("no shim section")
    if shim.get("tainted"):
        raise Handover("tainted")
    at = shim.get("generated_at")
    payload = shim["payload"]
    ttl = payload.get("ttl_seconds")
    if not isinstance(at, (int, float)) or not isinstance(ttl, (int, float)) or now - at > ttl:
        raise Handover("expired")
    names = sections.get("names")
    if not isinstance(names, dict) or not isinstance(names.get("payload"), dict):
        raise Handover("no names section")
    return payload


def _stat_pass(triples: list[Any]) -> None:
    for path, mtime_ns, size in triples:
        try:
            st = os.stat(path)
        except OSError:
            if mtime_ns is None:
                continue
            raise Handover(f"stale: {path} is gone") from None
        if mtime_ns is None:
            raise Handover(f"stale: {path} appeared")
        if st.st_mtime_ns != mtime_ns or st.st_size != size:
            raise Handover(f"stale: {path} changed")


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
    return st.st_mtime_ns >= cache_mtime_ns and now - st.st_mtime < WINDOW_SECONDS


def _touch(marker: str) -> None:
    # No contextlib.suppress: that import would be paid on every TAB.
    try:
        os.utime(marker, None)
    except FileNotFoundError:
        try:
            with open(marker, "a", encoding="utf-8"):
                pass
        except OSError:
            pass
    except OSError:
        pass


def validate_keys(cache_path: Any, data: dict[str, Any], site: str, now: float) -> str:
    """Validate the ``names`` key set, then ``tests`` on a tests site (spec 4.2 steps 2-3).

    Returns ``"stat"`` if ANY checked key set needed a full stat pass (after
    which its marker is touched), else ``"marker"``.
    """
    payload = servable_shim(data, now)
    tests = data["sections"].get("tests", {})
    if site == "tests" and not isinstance(tests.get("payload"), dict):
        raise Handover("no tests section")
    cache_path = str(cache_path)
    cache_dir = os.path.dirname(cache_path)
    # The caller READ the file before this stat, so a rewrite landing between the
    # two makes this mtime the NEW entry's while `data` is the old one, and a
    # marker written after it looks fresh: one TAB can be answered from data up to
    # the window old. That is the window's own contract -- a fresh marker vouches
    # for nothing newer than WINDOW_SECONDS anyway -- and the next TAB re-stats,
    # so closing it would cost a second read for a staleness the design accepts.
    cache_mtime_ns = os.stat(cache_path).st_mtime_ns
    how = "marker"
    for key in ("names", "tests") if site == "tests" else ("names",):
        marker = os.path.join(cache_dir, MARKER_FILENAMES[key])
        if _marker_fresh(marker, cache_mtime_ns, now):
            continue
        _stat_pass(payload["keys"][key])
        if key == "names":
            _inventory_pass(payload.get("inventory"))
        _touch(marker)
        how = "stat"
    return how


def collected_entry(data: dict[str, Any], digest: str, now: float) -> dict[str, Any] | None:
    """Apply ``read_collected_tests``' freshness rules to the raw file: the entry, or ``None``."""
    namespace = data.get(COLLECTED_KEY)
    entry = namespace.get(digest) if isinstance(namespace, dict) else None
    if not isinstance(entry, dict) or entry.get("schema_version") != COLLECTED_SCHEMA:
        return None
    at = entry.get("generated_at")
    if not isinstance(at, (int, float)) or now - at > COLLECTED_TTL_SECONDS:
        return None
    return entry


# --- entry ---------------------------------------------------------------------


def _answer_items(environ: dict[str, str], now: float) -> list[str]:
    """Compute the candidates for this TAB, or raise :class:`Handover`."""
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
        raise Handover("no cache file") from None
    payload = servable_shim(data, now)
    names = data["sections"]["names"]["payload"]
    res = resolve(payload["tree"], args, names.get("host_classes_by_id", {}))
    site = _site_for(res, frag)
    validate_keys(cache_path, data, site, now)
    tests = data["sections"].get("tests", {}).get("payload") if site == "tests" else None
    collected = collected_entry(data, payload["tests_digest"], now) if site == "tests" else None
    return complete(payload["tree"], res, frag, environ, Payloads(names, tests, collected))


class Outcome:
    """What the shim decided for one TAB: the candidates, or ``None`` and the reason."""

    __slots__ = ("items", "reason")

    def __init__(self, items: list[str] | None, reason: str = "") -> None:
        self.items = items
        self.reason = reason  # empty when answered; for `otto cache info` and tests otherwise


def answer_or_reason(environ: dict[str, str], now: float | None = None) -> Outcome:
    """Compute the candidates for this TAB, or ``None`` and why the shim hands over."""
    try:
        items = _answer_items(environ, time.time() if now is None else now)
    except Handover as e:
        return Outcome(None, e.reason)
    except Exception as e:  # noqa: BLE001
        # a TAB never tracebacks; the full path decides
        return Outcome(None, f"error: {type(e).__name__}: {e}")
    return Outcome(items)


def _site_for(res: Resolution, frag: str) -> str:
    """Name the key set the answer will read: the ONE target rule ``complete`` applies."""
    kind, param, _ = _target(res, frag)
    return site_of(param["source"]) if kind == "param" and param is not None else "names"


def answer(environ: dict[str, str]) -> str | None:
    """Render the text to print for this TAB (no trailing newline), or ``None`` to hand over."""
    items = answer_or_reason(environ).items
    return None if items is None else "\n".join(items)


def inspect_shim(cache_path: Any, now: float | None = None) -> str:
    """Describe, for ``otto cache info``, whether the NEXT names-site TAB would be served."""
    now = time.time() if now is None else now
    try:
        try:
            with open(str(cache_path), encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise Handover("no cache file") from None
        how = validate_keys(cache_path, data, "names", now)
        if how == "stat":
            return "served (validated now)"
        marker = os.path.join(os.path.dirname(str(cache_path)), MARKER_FILENAMES["names"])
        ago = int(now - os.stat(marker).st_mtime)
    except Handover as e:
        return f"handing over — {e.reason}"
    except Exception as e:  # noqa: BLE001
        return f"handing over — error: {type(e).__name__}: {e}"
    return f"served (validated {ago}s ago)"
