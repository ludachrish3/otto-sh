"""``otto --list-products`` / ``--list-tools``: the root flags over the listing library.

The library's rows and reasons are pinned in ``tests/unit/host/test_listing.py``;
this file pins what only the CLI decides: the flags exist, ``--lab`` selects the
view (and is not demanded), the table renders, user cells survive Rich markup,
and no host is contacted. The lab is the real one ``real_main_mocks`` builds from
``lab.json``; the entries are put on its real ``Repo`` before the lab loads.
"""

import re
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from otto.cli.main import app
from otto.config.scope import ProjectScopeConfig
from otto.declared import DeclaredEntry
from otto.host import dev_tool as dev_tool_mod
from otto.host import product as product_mod
from otto.host.product import Product, register_product_provider
from otto.registry import registering_repo
from otto.result import Result
from otto.utils import Status

runner = CliRunner()

# Wide enough that Rich never folds a table row or a cell in the output under test.
WIDE = {"COLUMNS": "240", "OTTO_LAB": ""}


def _entry(repo, name, kind="shell", *, seam="products", match=None, **params):
    return DeclaredEntry(
        name=name,
        kind=kind,
        seam=seam,
        owner=repo.name,
        base_dir=repo.sut_dir,
        match=match or {},
        params=params,
    )


class ProbeProduct(Product):
    def __init__(self, name: str) -> None:
        self.name = name

    async def stage(self, host):
        return Result(Status.Success)

    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return True


@pytest.fixture
def listing_world(real_main_mocks):
    """The real lab with declared entries on its repo; ``ordered_repos`` pinned to it."""
    repo = real_main_mocks["repo"]
    saved_p = list(product_mod._PRODUCT_PROVIDERS)
    saved_t = list(dev_tool_mod._DEV_TOOL_PROVIDERS)
    with patch("otto.config.bootstrapped.get_ordered_repos", return_value=[repo]):
        yield repo
    product_mod._PRODUCT_PROVIDERS[:] = saved_p
    dev_tool_mod._DEV_TOOL_PROVIDERS[:] = saved_t


def _invoke(*args, env=None):
    return runner.invoke(app, list(args), env={**WIDE, **(env or {})})


def _declare_two(repo):
    repo.declared_products = [
        _entry(repo, "agent", artifact="build/agent.tar.gz", stage_dir="/opt/stage"),
        _entry(repo, "kcov", artifact="build/kcov.ko", match={"id": "host2"}),
    ]
    repo.declared_dev_tools = [
        _entry(repo, "trace", seam="dev_tools", artifact="tools/trace.sh"),
    ]


# ── Without --lab: what the repo declares ─────────────────────────────────────


def test_list_products_without_lab_exits_zero_and_prints_the_declared_table(listing_world):
    _declare_two(listing_world)
    result = _invoke("--list-products")
    assert result.exit_code == 0, result.output
    header = next(line for line in result.output.splitlines() if "name" in line)
    assert [c.strip() for c in header.strip("│").split("│")] == [
        "name",
        "kind",
        "variant",
        "instrumented",
        "repo",
        "match",
        "artifact",
    ]
    assert "products" in result.output.splitlines()[0]
    assert "╭" in result.output  # box.ROUNDED
    agent = next(line for line in result.output.splitlines() if "agent" in line)
    assert [c.strip() for c in agent.strip("│").split("│")] == [
        "agent",
        "shell",
        "any",
        "missing",
        "test_repo",
        "any host",
        "build/agent.tar.gz",
    ]
    assert "id=host2" in result.output


def test_list_tools_without_lab_lists_the_dev_tool_entries(listing_world):
    _declare_two(listing_world)
    result = _invoke("--list-tools")
    assert result.exit_code == 0, result.output
    assert "dev tools" in result.output.splitlines()[0]
    assert "trace" in result.output
    assert "agent" not in result.output


def test_both_flags_print_both_tables(listing_world):
    _declare_two(listing_world)
    result = _invoke("--list-products", "--list-tools")
    assert result.exit_code == 0, result.output
    assert "agent" in result.output
    assert "trace" in result.output


def test_a_registered_provider_gets_a_note_naming_module_and_qualname(listing_world):
    def supply(host):
        return None

    with registering_repo(listing_world.name):
        register_product_provider(supply)
    result = _invoke("--list-products")
    assert result.exit_code == 0, result.output
    assert (
        f"test_repo also registers a product provider ({supply.__module__}.{supply.__qualname__}); "
        "what it supplies depends on the host, so it is listed only with --lab."
    ) in result.output


def test_user_cells_survive_rich_markup(listing_world):
    listing_world.declared_products = [_entry(listing_world, "fw[a]", artifact="out/[x].bin")]
    result = _invoke("--list-products")
    assert result.exit_code == 0, result.output
    assert "fw[a]" in result.output
    assert "out/[x].bin" in result.output


# ── With --lab: what each host gets ───────────────────────────────────────────


def test_list_products_with_lab_shows_hosts_stage_dir_and_not_used(listing_world):
    repo = listing_world
    repo.declared_products = [
        _entry(repo, "agent", artifact="build/agent.tar.gz", stage_dir="/opt/stage"),
        _entry(repo, "kcov", "kmod", artifact="build/kcov.ko", match={"id": "host2"}),
        _entry(repo, "fw", "embedded", artifact="out/fw.o", match={"id": "nope"}),
    ]
    repo.project_scope = ProjectScopeConfig([re.compile("test_lab")], [re.compile("host.*")])
    result = _invoke("--lab", "test_lab", "--list-products")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "products in lab test_lab" in lines[0]
    header = next(line for line in lines if "stage dir" in line)
    assert [c.strip() for c in header.strip("│").split("│")] == [
        "name",
        "kind",
        "variant",
        "instrumented",
        "repo",
        "hosts",
        "artifact",
        "stage dir",
    ]
    agent = next(line for line in lines if "agent" in line)
    assert [c.strip() for c in agent.strip("│").split("│")] == [
        "agent",
        "shell",
        "any",
        "missing",
        "test_repo",
        "host1, host2",
        "build/agent.tar.gz",
        "/opt/stage",
    ]
    kcov = next(line for line in lines if "kcov" in line)
    assert kcov.strip("│").split("│")[-1].strip() == "login home"
    assert "not used in this lab:" in lines
    assert "  fw (embedded, test_repo): no host matches" in lines


def test_the_not_used_section_is_absent_when_everything_landed(listing_world):
    _declare_two(listing_world)
    result = _invoke("--lab", "test_lab", "--list-products")
    assert result.exit_code == 0, result.output
    assert "not used in this lab" not in result.output


def test_outside_scope_reason_prints_its_brackets_literally(listing_world):
    repo = listing_world
    repo.declared_products = [_entry(repo, "agent", artifact="a")]
    repo.project_scope = ProjectScopeConfig([re.compile("other")], [re.compile(".*")])
    result = _invoke("--lab", "test_lab", "--list-products")
    assert result.exit_code == 0, result.output
    assert "  agent (shell, test_repo): outside test_repo's [project] scope" in result.output


def test_a_provider_product_row_reads_code_and_its_class(listing_world):
    with registering_repo(listing_world.name):
        register_product_provider(
            lambda host: [ProbeProduct("probe")] if host.id == "host1" else None
        )
    listing_world.project_scope = ProjectScopeConfig([re.compile(".*")], [re.compile(".*")])
    result = _invoke("--lab", "test_lab", "--list-products")
    assert result.exit_code == 0, result.output
    probe = next(line for line in result.output.splitlines() if "probe" in line)
    assert [c.strip() for c in probe.strip("│").split("│")][:6] == [
        "probe",
        "code (ProbeProduct)",
        "any",
        "unknown",
        "test_repo",
        "host1",
    ]


def test_list_tools_with_lab_lists_dev_tools(listing_world):
    _declare_two(listing_world)
    result = _invoke("--lab", "test_lab", "--list-tools")
    assert result.exit_code == 0, result.output
    assert "dev tools in lab test_lab" in result.output
    assert "trace" in result.output


def test_neither_view_contacts_a_host(listing_world):
    _declare_two(listing_world)

    def refuse(*_a, **_k):
        raise AssertionError("the listing contacted a host")

    with (
        patch("otto.host.unix_host.UnixHost.login_home", refuse),
        patch("otto.host.unix_host.UnixHost.exec", refuse),
        patch("otto.host.unix_host.UnixHost.run", refuse),
    ):
        for args in (["--list-products"], ["--lab", "test_lab", "--list-products"]):
            result = _invoke(*args)
            assert result.exit_code == 0, result.output
            assert "agent" in result.output


def test_the_lab_view_with_an_unknown_lab_reports_like_the_other_lab_flags(listing_world):
    result = _invoke("--lab", "no_such_lab", "--list-products")
    assert result.exit_code != 0


# ── Contrast and help ─────────────────────────────────────────────────────────


def test_list_hosts_still_needs_a_lab_while_list_products_does_not(listing_world):
    assert _invoke("--list-hosts").exit_code == 2
    assert _invoke("--list-products").exit_code == 0


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _rendered_help() -> str:
    """``otto --help`` as one line of words, whatever width or colour Rich chose.

    Under ``GITHUB_ACTIONS`` Typer forces a Rich terminal, and with CI's
    ``TERM=dumb`` Rich then renders 80 columns wide and ignores ``COLUMNS``
    (``Console.size``'s dumb-terminal arm), so a sentence in the options
    panel wraps across box-drawn lines. Strip the escapes and the panel
    chrome and collapse the whitespace, so the assertions read the words.
    """
    result = _invoke("--help")
    assert result.exit_code == 0
    plain = _ANSI.sub("", result.output)
    for chrome in "│╭╮╰╯─":
        plain = plain.replace(chrome, " ")
    return " ".join(plain.split())


def test_the_new_columns_render_under_ci_width(listing_world):
    _declare_two(listing_world)
    result = _invoke(
        "--list-products", env={"GITHUB_ACTIONS": "true", "TERM": "dumb", "FORCE_COLOR": ""}
    )
    assert result.exit_code == 0, result.output
    plain = _ANSI.sub("", result.output)
    for chrome in "│╭╮╰╯─┬┼┴":
        plain = plain.replace(chrome, " ")
    words = " ".join(plain.split())
    assert "variant" in words
    assert "instrumented" in words
    assert "agent shell any missing" in words


def test_help_describes_both_flags_in_plain_language():
    text = _rendered_help()
    assert "--list-products" in text
    assert "--list-tools" in text
    assert "List the products the repos declare" in text
    assert "List the dev tools the repos declare" in text
    assert "``" not in text


def test_the_listing_exits_before_any_subcommand_runs(listing_world):
    """``otto --list-products test X`` lists and stops; ``test`` would exit 2 without a lab."""
    _declare_two(listing_world)
    result = _invoke("--list-products", "test", "test_anything")
    assert result.exit_code == 0, result.output
    assert "agent" in result.output


def test_a_listing_flag_combines_with_the_other_lab_flags(listing_world):
    _declare_two(listing_world)
    result = _invoke("--lab", "test_lab", "--list-products", "--list-hosts")
    assert result.exit_code == 0, result.output
    assert "agent" in result.output
    assert "• host1" in result.output


def test_the_lab_name_in_the_title_is_not_read_as_markup(listing_world):
    from types import SimpleNamespace

    lab = SimpleNamespace(name="lab[x]", hosts={})
    with (
        patch("otto.cli.invoke.ensure_inline_lab"),
        patch("otto.context.get_context", return_value=SimpleNamespace(lab=lab)),
    ):
        result = _invoke("--lab", "test_lab", "--list-products")
    assert result.exit_code == 0, result.output
    assert "products in lab lab[x]" in result.output


def test_a_name_defined_in_data_and_by_a_provider_fails_the_lab_load_naming_both(listing_world):
    repo = listing_world
    repo.declared_products = [_entry(repo, "agent", artifact="build/agent.tar.gz")]
    repo.project_scope = ProjectScopeConfig([re.compile("test_lab")], [re.compile("host.*")])

    def products(host):
        return [ProbeProduct("agent")]

    with registering_repo(repo.name):
        register_product_provider(products)
    result = _invoke("--lab", "test_lab", "--list-products")
    assert result.exit_code != 0
    text = result.output + str(result.exception or "")
    assert "[[products]] 'agent' (repo test_repo) is also defined by provider" in text
    assert f"{products.__module__}:{products.__qualname__}" in text
