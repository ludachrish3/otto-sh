"""The run-tree contract (spec 2026-09-16 §3): one table, both trees.

Kills: any drive-by reorganization of either tree, and any product name that
could escape its directory or collide with the host-level debug dir.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from otto import layout

BASE = Path("/run")

# (function, args, expected relative path) — the whole contract in one table.
TEMPLATES = [
    (layout.host_logs_dir, ("h1",), "logs/h1"),
    (layout.host_debug_dir, ("h1",), "logs/h1/debug"),
    (layout.product_logs_dir, ("h1", "app"), "logs/h1/app/product"),
    (layout.product_debug_dir, ("h1", "app"), "logs/h1/app/debug"),
    (layout.cov_dir, (), "cov"),
    (layout.cov_report_dir, (), "cov_report"),
    (layout.cov_host_dir, ("h1",), "cov/h1"),
    (layout.cov_product_dir, ("h1", "app"), "cov/h1/app"),
]


@pytest.mark.parametrize(("fn", "args", "rel"), TEMPLATES, ids=lambda x: getattr(x, "__name__", x))
def test_templates(fn, args, rel):
    assert fn(BASE, *args) == BASE / rel


def test_run_dir_of_is_the_inverse_of_host_logs_dir():
    # The one path walked back UP the tree, and its only caller holds nothing
    # but a host log root. Kills: a builder that changes the depth of
    # logs/<host_id> without the inverse following it — every consumer that
    # recovers the run dir would then key its subtrees off the wrong directory.
    assert layout.run_dir_of(layout.host_logs_dir(BASE, "h1")) == BASE


def test_logs_and_cov_trees_share_the_host_product_shape():
    logs = layout.product_logs_dir(BASE, "h1", "app").parent.relative_to(BASE / "logs")
    cov = layout.cov_product_dir(BASE, "h1", "app").relative_to(BASE / "cov")
    assert logs == cov == Path("h1/app")


def test_cov_product_dir_in_is_the_same_leaf_from_an_already_resolved_cov_dir():
    # The fetchers are handed the already-resolved `<run>/cov`. Kills: the two
    # spellings drifting — a leaf reached from a run dir and one reached from
    # a cov dir must be the same directory, and both must validate the name.
    assert layout.cov_product_dir_in(BASE / "cov", "h1", "app") == layout.cov_product_dir(
        BASE, "h1", "app"
    )
    with pytest.raises(ValueError, match=r"product name"):
        layout.cov_product_dir_in(BASE / "cov", "h1", "debug")


def test_no_function_creates_directories(tmp_path):
    layout.product_logs_dir(tmp_path, "h1", "app")
    layout.cov_product_dir(tmp_path, "h1", "app")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "bad",
    ["", ".", "..", "a/b", "a\\b", "nul\0", "debug"],
    ids=["empty", "dot", "dotdot", "slash", "backslash", "nul", "reserved-debug"],
)
def test_validate_product_name_rejects(bad):
    with pytest.raises(ValueError, match=r"product name"):
        layout.validate_product_name(bad)


@pytest.mark.parametrize("good", ["app", "my-app_2", "Agent.v1", "a b"])
def test_validate_product_name_accepts(good):
    layout.validate_product_name(good)


@pytest.mark.parametrize(
    "fn", [layout.product_logs_dir, layout.product_debug_dir, layout.cov_product_dir]
)
def test_product_path_builders_validate_the_name(fn):
    # The guard lives where the state lives: a bad name never reaches disk
    # even from a caller that skipped ingest.
    with pytest.raises(ValueError, match=r"product name"):
        fn(BASE, "h1", "debug")


# The invocation level above the run tree: where each run directory sits under the xdir.


def test_the_layout_has_no_output_root():
    # Kills: the old `<xdir>/otto-output/` level coming back. A run sits
    # straight under `<xdir>/<command>/` now, default xdir or explicit.
    assert not hasattr(layout, "output_root")
    assert not hasattr(layout, "OUTPUT_DIR")


@pytest.mark.parametrize(("command", "rel"), [("test", "test"), ("write-file", "write_file")])
def test_command_output_dir_sits_straight_under_the_xdir(command, rel):
    # Hyphens become underscores here, in the one builder, so a reader and the
    # writer can never spell a command's directory differently.
    assert layout.command_output_dir(Path("/x"), command) == Path("/x") / rel


_T = datetime(2026, 7, 3, 12, 0, 0, 123456, tzinfo=timezone.utc)


def test_run_dir_name_is_utc_millis_then_the_subcommand():
    from otto.layout import run_dir_name

    assert run_dir_name(_T) == "20260703_120000_123"
    assert run_dir_name(_T, "get") == "20260703_120000_123_get"
    # The subcommand is spelled like a command directory: hyphens become underscores.
    assert run_dir_name(_T, "write-file") == "20260703_120000_123_write_file"
    # A non-UTC clock reading is named in UTC, so two zones never disagree.
    plus_two = timezone(timedelta(hours=2))
    assert run_dir_name(_T.astimezone(plus_two)) == "20260703_120000_123"


def test_run_output_dir_is_xdir_command_then_run_dir():
    from otto.layout import RunEntry, run_output_dir

    got = run_output_dir(Path("/x"), RunEntry("cov", "get"), started=_T)
    assert got == Path("/x/cov/20260703_120000_123_get")
    assert "otto-output" not in got.parts
    assert run_output_dir(Path("/x"), RunEntry("test"), started=_T) == Path(
        "/x/test/20260703_120000_123"
    )


def test_run_output_dir_reads_the_clock_when_no_start_is_given():
    from otto.layout import RUN_DIR_NAME_RE, RunEntry, run_output_dir

    got = run_output_dir(Path("/x"), RunEntry("host", "get_logs"))
    assert got.parent == Path("/x/host")
    assert RUN_DIR_NAME_RE.match(got.name)
    assert got.name.endswith("_get_logs")


@pytest.mark.parametrize("sub", [None, "get", "write-file", "get_logs"])
def test_run_dir_name_re_matches_exactly_what_run_dir_name_writes(sub):
    from otto.layout import RUN_DIR_NAME_RE, run_dir_name

    assert RUN_DIR_NAME_RE.match(run_dir_name(_T, sub))


@pytest.mark.parametrize(
    "name",
    ["notes", "2026_120000_123", "20260703_120000", "20260703-120000-123", "x20260703_120000_123"],
)
def test_run_dir_name_re_refuses_other_names(name):
    from otto.layout import RUN_DIR_NAME_RE

    assert not RUN_DIR_NAME_RE.match(name)


def test_make_run_dir_creates_parents_and_the_marker(tmp_path):
    from otto.layout import RUN_MARKER, make_run_dir

    run = tmp_path / "a" / "b" / "20260703_120000_123"
    assert make_run_dir(run) == run
    assert run.is_dir()
    marker = run / RUN_MARKER
    assert RUN_MARKER == ".otto-run"
    assert marker.is_file()
    assert marker.read_bytes() == b""
    # The CLI refuses a collision ...
    with pytest.raises(FileExistsError):
        make_run_dir(run)
    # ... the library reuses its memoised dir, and re-marks it.
    marker.unlink()
    assert make_run_dir(run, exist_ok=True) == run
    assert marker.is_file()


def _dir(parent: Path, name: str, *, marker: "str | None") -> Path:
    d = parent / name
    d.mkdir()
    if marker == "file":
        (d / ".otto-run").touch()
    elif marker == "dir":
        (d / ".otto-run").mkdir()
    return d


@pytest.mark.parametrize(
    ("name", "marker", "expected"),
    [
        ("20260703_120000_123", "file", True),
        ("20260703_120000_123_get", "file", True),
        ("20260703_120000_123", None, False),
        ("notes", "file", False),
        ("notes", None, False),
        ("20260703_120000_123", "dir", False),
    ],
    ids=[
        "name+marker",
        "sub-name+marker",
        "name-no-marker",
        "bad-name+marker",
        "neither",
        "marker-is-a-dir",
    ],
)
def test_is_run_dir_truth_table(tmp_path, name, marker, expected):
    from otto.layout import is_run_dir

    assert is_run_dir(_dir(tmp_path, name, marker=marker)) is expected


def test_is_run_dir_of_a_missing_path_is_false(tmp_path):
    from otto.layout import is_run_dir

    assert is_run_dir(tmp_path / "20260703_120000_123") is False


def test_a_symlink_to_a_marked_run_is_not_a_run(tmp_path):
    """A walk that trusts is_run_dir alone never reaches through a link to delete elsewhere."""
    from otto.layout import is_run_dir

    (tmp_path / "elsewhere").mkdir()
    elsewhere = _dir(tmp_path / "elsewhere", "20260703_120000_123", marker="file")
    link = tmp_path / "20260703_120000_456"
    link.symlink_to(elsewhere, target_is_directory=True)
    assert is_run_dir(elsewhere) is True
    assert is_run_dir(link) is False


def test_the_invocation_builders_create_nothing(tmp_path):
    layout.command_output_dir(tmp_path, "test")
    assert list(tmp_path.iterdir()) == []


def test_the_run_builders_create_nothing(tmp_path):
    from otto.layout import RunEntry, run_output_dir

    run_output_dir(tmp_path, RunEntry("test"))
    assert list(tmp_path.iterdir()) == []
