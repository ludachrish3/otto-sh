"""Shared derivation of otto's first-party CLI command names from the registry.

Both the in-process unit suite (``tests/unit/cli/test_root_group.py``) and the
subprocess e2e suite (``tests/e2e/cli/test_schema_run_help_e2e.py``) need the
same answer to "what are otto's built-in top-level CLI names" — filtered to
the ones ``register_builtin_commands`` itself registered (by ORIGIN, never a
hand-typed list that silently drifts the moment a group is added, renamed, or
removed).
"""


def builtin_command_names() -> list[str]:
    """The groups ``register_builtin_commands`` itself registered, by ORIGIN.

    Not every name in the registry: a third-party plugin — or a test that
    registered one and is running in the same process — is not a first-party
    group, and counting those would make this assertion drift with whatever
    else the worker had done.
    """
    from otto.cli.builtin_commands import register_builtin_commands
    from otto.cli.registry import CLI_COMMANDS

    register_builtin_commands()  # idempotent
    names = [
        name
        for name in CLI_COMMANDS.names()
        if CLI_COMMANDS.get(name).origin == "otto.cli.builtin_commands"
    ]
    # A floor, not the exact count: this is the one place both the in-process
    # unit tree and the subprocess e2e tree derive their group set from, so a
    # collapse here (a resolve_spec_command signature change, an origin
    # string typo, a registration-timing bug) must fail LOUDLY rather than
    # silently emptying `GROUPS`/`SUBCOMMAND_HELP` coverage in the e2e file —
    # an empty `parametrize` collects zero cases without erroring, and an
    # empty-list completeness check passes vacuously (`missing = []`).
    assert len(names) >= 10, (
        f"only {len(names)} builtin CLI names derived from the registry "
        f"(origin='otto.cli.builtin_commands'): {names!r} — this is either a "
        "real regression (otto lost most of its command groups) or this "
        "derivation itself broke (resolve_spec_command, CLI_COMMANDS, or the "
        "origin string changed shape). Either way, every test consuming this "
        "list would otherwise pass vacuously on a near-empty group set."
    )
    return names


def builtin_group_names() -> list[str]:
    """Built-in names whose registered object resolves to a Typer GROUP.

    A "group" is distinguished from a single command by inspecting the
    resolved command object (``hasattr(cmd, "commands")``) — the same surface
    check ``test_builtin_specs_match_native_typer_conversion`` already applies
    — never by name. That is what correctly excludes ``init`` (a single
    function-loader command, never a Typer app) AND ``monitor`` (a Typer app
    that Typer's own converter flattens to one bare leaf, per
    ``otto.cli.registry._typer_app_flattens``) without either one being
    hand-listed as an exception.
    """
    from otto.cli.registry import CLI_COMMANDS, resolve_spec_command

    return [
        name
        for name in builtin_command_names()
        if hasattr(resolve_spec_command(CLI_COMMANDS.get(name)), "commands")
    ]
