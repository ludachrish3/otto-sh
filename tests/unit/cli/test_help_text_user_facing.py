"""Built-in ``--help`` text is written for a user, never raw reStructuredText.

Typer shows a command's docstring as its ``--help`` text, so a docstring written
for a developer (double-backtick literals, ``:func:`` roles, notes about
internals) reaches the person typing ``otto <group> --help``. This walks every
built-in command the root app can reach and fails on any help string that still
carries RST markup.

The walk reads the real command objects, not a hand-kept list of functions, so a
new built-in group, leaf, host class or instruction is covered the moment it
exists:

* the groups come from :func:`tests._fixtures.cli_registry.builtin_command_names`
  (the registry, filtered by origin) and are walked through their resolved
  command trees;
* the ``otto run`` instructions otto ships are published into the registry only
  at bootstrap, so the walk publishes them itself;
* the ``otto host`` verbs are synthesized per host class from ``@cli_exposed``
  methods, so every first-party :class:`~otto.host.host.BaseHost` subclass is
  walked, found by recursing over ``__subclasses__``.

What a user sees decides what is checked: a leaf's ``--help`` shows its whole
docstring up to a form feed, so the whole text is checked, not its first line.
"""

import re
from collections.abc import Iterator
from typing import Any, ClassVar

import click
import typer

from otto.cli.expose import collect_exposed_methods, verb_help
from otto.cli.main import app
from otto.cli.registry import CLI_COMMANDS, register_cli_command, resolve_spec_command
from otto.host.host import BaseHost
from otto.utils import cli_exposed
from tests._fixtures.cli_registry import builtin_command_names
from tests._fixtures.host_classes import host_classes_in_the_tree
from tests._fixtures.registrant import from_module

_RST_MARKUP = re.compile(
    r"``"  # double-backtick literal
    r"|:[A-Za-z_][\w.+-]*:`"  # role: :func:`x`, :attr:`x`
    r"|(?<![\w*`])\*[A-Za-z_]\w*\*(?!\w)"  # emphasis: *word* (not a glob like *.json)
    r"|^\s*:(?:param|type|returns?|rtype|raises?)\b[^:\n]*:"  # field list: :param x:
    r"|^\s*\.\. [\w-]+::"  # directive: .. note::
    r"|§",  # a pointer into the design specs ("Spec §10"), meaningless to a user
    re.MULTILINE,
)


def _help_texts(cmd: Any) -> Iterator[tuple[str, str]]:
    """Yield ``(where, text)`` for every string *cmd* shows a user about itself."""
    for attr in ("help", "short_help", "epilog"):
        text = getattr(cmd, attr, None)
        if text:
            # Typer cuts a help text at a form feed; what follows is never shown.
            yield attr, text.split("\f")[0]
    for param in cmd.params:
        text = getattr(param, "help", None)
        if text:
            yield f"parameter {param.name!r} help", text.split("\f")[0]


def _walk(cmd: Any, path: str) -> Iterator[tuple[str, str, str]]:
    """Yield ``(command path, where, text)`` for *cmd* and every command beneath it.

    Every command yields one ``"command"`` row first, so a command with no help
    text at all still shows up in the walk's paths.
    """
    yield path, "command", ""
    for where, text in _help_texts(cmd):
        yield path, where, text
    if hasattr(cmd, "list_commands"):
        ctx = click.Context(cmd)
        for name in cmd.list_commands(ctx):
            sub = cmd.get_command(ctx, name)
            if sub is not None:
                yield from _walk(sub, f"{path} {name}")


_KNOWN_VERBS = {
    "BaseHost": {"exec", "shutdown", "power"},
    "UnixHost": {"exec", "shutdown", "mkdir", "glob", "read-file", "load", "unload"},
    "EmbeddedHost": {"exec", "shutdown", "load", "unload"},
    "ZephyrHost": {"exec", "load", "unload"},
    "LocalHost": {"exec", "mkdir", "glob"},
    "DockerContainerHost": {"exec", "shutdown"},
}
"""Verbs each first-party host class is known to expose, spelled out by hand."""


def _host_classes() -> list[type]:
    """``BaseHost`` and every live first-party subclass of it."""
    return [BaseHost, *host_classes_in_the_tree()]


def _host_verb_prefix(cls: type) -> str:
    return f"otto host <{cls.__qualname__}> "


def _walk_built_in_commands() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    # The root screen: its own description and options, plus the one-line
    # entry each group shows in the command list (the registry's help).
    found.extend(_walk(typer.main.get_command(app), "otto"))
    # Each group's real, resolved command tree.
    for name in builtin_command_names():
        found.extend(_walk(resolve_spec_command(CLI_COMMANDS.get(name)), f"otto {name}"))
    # `otto host` verbs come from the host classes; a verb can carry different
    # help per class, so every first-party class is walked, not just the first
    # registration of each name.
    host_group = resolve_spec_command(CLI_COMMANDS.get("host"))
    for cls in _host_classes():
        for cli_name, attr_name in collect_exposed_methods(cls).items():
            verb = host_group._class_command(cls, cli_name, attr_name)
            found.extend(_walk(verb, f"{_host_verb_prefix(cls)}{cli_name}"))
    return found


def _offenders(texts: list[tuple[str, str, str]]) -> list[str]:
    return [
        f"{path} [{where}]: {text.strip().splitlines()[0]!r}"
        for path, where, text in texts
        if _RST_MARKUP.search(text)
    ]


def test_built_in_help_has_no_rst_markup():
    texts = _walk_built_in_commands()
    paths = {path for path, _, _ in texts}

    # Not vacuous: every built-in group, every shipped instruction and every
    # verb of every first-party host class was actually reached.
    for name in builtin_command_names():
        assert f"otto {name}" in paths, name
    from otto.instructions import PROJECT_INSTRUCTIONS

    shipped = PROJECT_INSTRUCTIONS.names()
    assert "install" in shipped, shipped
    for name in shipped:
        assert f"otto run {name}" in paths, name
    classes = _host_classes()
    for cls in classes:
        prefix = _host_verb_prefix(cls)
        verb_paths = {p for p in paths if p.startswith(prefix)}
        # One path per verb: `otto host <Class> <verb>` and nothing deeper.
        assert {p[len(prefix) :] for p in verb_paths} == set(collect_exposed_methods(cls)), cls
    # Hard anchors, written out rather than derived: the comparison above takes
    # its expected set from the very function that drives the walk, so it
    # would pass if both came back empty.
    walked = {c.__name__: {p for p in paths if p.startswith(_host_verb_prefix(c))} for c in classes}
    for class_name, verbs in _KNOWN_VERBS.items():
        assert class_name in walked, (class_name, sorted(walked))
        prefix = _host_verb_prefix(next(c for c in classes if c.__name__ == class_name))
        assert {f"{prefix}{v}" for v in verbs} <= walked[class_name], class_name

    offenders = _offenders(texts)
    assert not offenders, (
        "help text shown to users carries RST markup (write plain text with "
        "single backticks; keep developer notes in a comment under the "
        "docstring, or pass help_= to @cli_exposed for host verbs):\n" + "\n".join(offenders)
    )


def test_walk_flags_a_planted_built_in_with_rst_help():
    def planted() -> None:
        """Do a thing with the ``--lab`` flag."""

    # Registered from a frame that IS otto's built-in module, as a built-in is.
    from_module("otto.cli.builtin_commands", register_cli_command, "zz-planted", planted)
    try:
        assert "zz-planted" in builtin_command_names()
        offenders = _offenders(_walk_built_in_commands())
        assert any(o.startswith("otto zz-planted [help]") for o in offenders), offenders
    finally:
        CLI_COMMANDS.unregister("zz-planted")
    assert "zz-planted" not in CLI_COMMANDS


def test_rst_markup_pattern_flags_what_it_claims_to():
    for leaky in (
        "Run the ``--lab`` check.",
        "See :func:`~otto.cli.invoke.command_preamble` first.",
        "Fetch :attr:`debug_log_globs` matches.",
        "Call :meth:`is_installed`.",
        "Expand *pattern* on the host.",
        "Return *path*.",
        "Intro.\n\n:param x: the thing\n",
        "Intro.\n\n    :raises ValueError: bad\n",
        "Intro.\n\n.. note:: careful\n",
        "Create a tunnel. See spec §6.",
        "Remove a tunnel. Spec §10.",
    ):
        assert _RST_MARKUP.search(leaky), leaky
    for clean in (
        "Run the `--lab` check.",
        "Write *.schema.json into the directory.",
        "Match *.log and *.txt files.",
        "Matches `*log*` in the name.",
        "Copy SRC to DST (`cp`); a host:path form is accepted.",
        "Use a 2*3*4 style product.",
        "Ratio is a:b and the time is 10:30.",
    ):
        assert not _RST_MARKUP.search(clean), clean


def test_form_feed_hides_what_follows_in_a_help_text():
    class _Cmd:
        help = "Visible.\n\f\n:param x: hidden by Typer"
        short_help = None
        epilog = None
        params: ClassVar[list[Any]] = []

    assert not _offenders([("c", w, t) for w, t in _help_texts(_Cmd())])


def test_verb_help_inheritance():
    class Parent:
        @cli_exposed(help_="Parent help.")
        async def with_help(self) -> None:
            """Docstring of the parent."""

        @cli_exposed
        async def without_help(self) -> None:
            """Parent docstring line.

            More.
            """

        @cli_exposed(help_="Lent by a differently named verb.")
        async def other_name(self) -> None:
            """Docstring."""

    class Child(Parent):
        @cli_exposed
        async def with_help(self) -> None:
            """See :meth:`Parent.with_help`."""

        @cli_exposed
        async def without_help(self) -> None:
            """Child docstring line."""

        # Same attribute name, but exposed under another CLI name than the
        # parent's verb of that name: it must not borrow the parent's help.
        @cli_exposed(name="other")
        async def other_name(self) -> None:
            """Child docstring for the renamed verb."""

    class Override(Parent):
        @cli_exposed(help_="Own help.")
        async def with_help(self) -> None:
            """Anything."""

    class Untouched(Parent):
        pass

    # An override with no help_ of its own inherits the nearest one above it...
    assert verb_help(Child, "with_help") == "Parent help."
    # ...its own help_ wins...
    assert verb_help(Override, "with_help") == "Own help."
    # ...and with none anywhere, the first line of its own docstring.
    assert verb_help(Child, "without_help") == "Child docstring line."
    assert verb_help(Parent, "without_help") == "Parent docstring line."
    # A verb inherited unchanged reads the same as on its parent.
    assert verb_help(Untouched, "with_help") == verb_help(Parent, "with_help")
    assert verb_help(Untouched, "without_help") == verb_help(Parent, "without_help")
    # A base verb exposed under a different CLI name lends nothing.
    assert verb_help(Child, "other_name") == "Child docstring for the renamed verb."


def test_a_restated_verb_shows_the_help_of_the_verb_it_overrides():
    from otto.host.docker_host import DockerContainerHost

    assert verb_help(DockerContainerHost, "exec") == verb_help(BaseHost, "exec")
    assert verb_help(BaseHost, "exec")
