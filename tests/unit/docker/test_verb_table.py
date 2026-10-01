"""The docker verb table is the single source of truth: every registered command has a
row, every row's leaf carries the policy markers its row declares, and every row has a
doc page."""

import re

from otto.cli.docker import _GROUPS, _VERBS, compose_app, docker_app
from tests._fixtures.paths import PROJECT_ROOT


def _registered(app):
    return {c.name for c in app.registered_commands}


def test_every_registered_command_has_a_row_and_vice_versa():
    rows = {(v.group, v.name) for v in _VERBS}
    registered = {("docker", n) for n in _registered(docker_app)} | {
        ("compose", n) for n in _registered(compose_app)
    }
    assert registered == rows


def test_compose_is_mounted_on_docker():
    assert any(
        g.typer_instance is compose_app and g.name == "compose"
        for g in docker_app.registered_groups
    )


def test_every_row_stamps_the_markers_it_declares():
    for verb in _VERBS:
        assert getattr(verb.leaf, "__cli_output_dir__", True) is verb.output_dir, verb
        assert getattr(verb.leaf, "__cli_dry_run_preview__", False) is verb.dry_run_preview, verb


def test_every_rows_markers_survive_resolution_to_the_command_callback():
    """The markers must reach the RESOLVED commands' callbacks through typer's shim.

    ``verb.leaf`` alone (the test above) cannot notice typer ceasing to carry the
    markers through its own callback shim -- the leaf-invoke preamble never reads
    ``verb.leaf`` directly, it reads ``ctx.command.callback`` off whatever typer
    resolved for the dispatched path. A typer upgrade that stopped
    ``update_wrapper``-ing registered callbacks would silently give every read-only
    leaf an output dir and delete every shipped dry-run preview, exit 0, with
    nothing else noticing.
    """
    import typer

    group = typer.main.get_group(docker_app)
    for verb in _VERBS:
        cmd = (
            group.commands[verb.name]
            if verb.group == "docker"
            else group.commands["compose"].commands[verb.name]
        )
        assert getattr(cmd.callback, "__cli_output_dir__", True) is verb.output_dir, verb
        assert getattr(cmd.callback, "__cli_dry_run_preview__", False) is verb.dry_run_preview, verb


def test_every_row_has_a_doc_page():
    for verb in _VERBS:
        sub = "" if verb.group == "docker" else "compose/"
        page = PROJECT_ROOT / "docs" / "cli" / "docker" / f"{sub}{verb.name}.md"
        assert page.exists(), page


def test_the_groups_map_names_both_apps():
    assert {"docker": docker_app, "compose": compose_app} == _GROUPS


_OLD = re.compile(r"otto docker (?:(?:up|down)\b|build\s+(?!-)(?!\[)(?:<TAB>|\w+))")


def test_no_source_string_names_the_old_top_level_verbs():
    offenders = []
    for path in (PROJECT_ROOT / "src" / "otto").rglob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if _OLD.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
