"""The click tree, serialised for the shim: every site classified, nothing left silently live."""

import typer

from otto.config.completion_tree import serialize_tree


def _cli():
    from otto.cli.main import app

    return typer.main.get_command(app)


def test_every_completer_in_the_cli_is_registered():
    """Spec §3.3: an unregistered completer would go live without anyone noticing."""
    result = serialize_tree(_cli())
    assert result.unregistered == []


def test_root_options_and_commands_are_serialised_in_menu_order():
    cli = _cli()
    tree = serialize_tree(cli).tree
    ctx = type(cli).context_class(cli, info_name="otto", resilient_parsing=True)
    assert list(tree["commands"]) == [
        n for n in cli.list_commands(ctx) if not getattr(cli.get_command(ctx, n), "hidden", False)
    ]
    lab = next(p for p in tree["params"] if "--lab" in p["flags"])
    assert lab == {
        "flags": ["--lab", "-l"],
        "name": "labs",
        "takes_value": True,
        "multiple": True,
        "nargs": 1,
        "sep": "+",
        "source": {"kind": "payload", "key": "labs", "sort": True, "sep": "+"},
    }
    pair = next(p for p in tree["params"] if "--field" in p["flags"])
    assert pair["flags"] == ["--field", "--debug"]
    assert pair["takes_value"] is False
    xdir = next(p for p in tree["params"] if "--xdir" in p["flags"])
    # a TyperPath: Typer offers nothing for a path fragment
    assert xdir["source"] == {"kind": "none"}
    assert "--help" in {f for p in tree["params"] for f in p["flags"]}
    # Stub-proof: a `--pending-subcmd-args` mishap resolves every child as a
    # STUB (help-only, `--help` alone) instead of the real command — the
    # tree would still look plausible and `unregistered` would stay empty
    # (a stub has no completer sites to miss), so this pins that `run`'s own
    # option actually made it into the tree, not just its ever-present `--help`.
    run_flags = {f for p in tree["commands"]["run"]["params"] for f in p["flags"]}
    assert "--list-instructions" in run_flags


def test_host_node_carries_per_class_verb_views():
    from otto.cli.expose import exposed_cli_names
    from otto.host.embedded_host import ZephyrHost
    from otto.host.unix_host import UnixHost

    tree = serialize_tree(_cli()).tree
    host = tree["commands"]["host"]
    assert host["scoped_by"] == "host_id"
    host_id = host["params"][0]
    assert host_id["flags"] == []
    assert host_id["nargs"] == 1
    assert host_id["source"]["kind"] == "payload"
    assert host_id["source"]["always"] == ["local"]
    views = tree["host_classes"]
    assert set(views) >= {"unix", "embedded", "zephyr"}
    assert set(views["zephyr"]) <= set(host["commands"])
    unix_only = exposed_cli_names(UnixHost) - exposed_cli_names(ZephyrHost)
    assert unix_only <= set(host["commands"]) - set(views["zephyr"])
    put = views["unix"]["put"]
    files, dest = [p for p in put["params"] if not p["flags"]]
    assert files["nargs"] == -1
    assert files["source"] == {"kind": "none"}  # Paths, not Files
    assert dest["source"] == {"kind": "live"}


def test_the_serialiser_sees_typers_vendored_click():
    """Typer 0.27 vendors click; a serialiser written against upstream `click` classifies NOTHING."""  # noqa: E501
    from typer.core import TyperGroup, TyperOption

    cli = _cli()
    assert isinstance(cli, TyperGroup)
    ctx = type(cli).context_class(cli, info_name="otto", resilient_parsing=True)
    assert type(ctx).__module__ == "typer._click.core"
    assert all(isinstance(p, TyperOption) for p in cli.get_params(ctx))


def test_tests_and_markers_sites_are_classified():
    tree = serialize_tree(_cli()).tree
    test_node = tree["commands"]["test"]
    by_flag = {f: p for p in test_node["params"] for f in p["flags"]}
    assert by_flag["--tests"]["source"] == {"kind": "tests", "sep": ","}
    assert by_flag["--tests"]["sep"] == ","
    assert by_flag["-m"]["source"] == {"kind": "markers"}
