"""``otto.home_root``: the one reader of ``OTTO_HOME`` and the one default xdir.

Every test imports the module inside its body, so a missing name fails that
test alone and never the collection of the file.
"""

from pathlib import Path

import pytest


def test_default_xdir_is_otto_xdir_under_the_home(tmp_path, monkeypatch):
    from otto.home_root import XDIR_DIRNAME, default_xdir

    assert XDIR_DIRNAME == "otto-xdir"
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home"))
    assert default_xdir() == tmp_path / "home" / "otto-xdir"
    # The env is read per call: a changed OTTO_HOME is a new answer.
    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "other"))
    assert default_xdir() == tmp_path / "other" / "otto-xdir"

    monkeypatch.delenv("OTTO_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    assert default_xdir() == Path.home() / ".otto" / "otto-xdir"
    assert default_xdir() == tmp_path / "user" / ".otto" / "otto-xdir"

    # Pure: a query creates nothing.
    assert list(tmp_path.iterdir()) == []


def test_an_empty_otto_home_is_unset(tmp_path, monkeypatch):
    from otto.home_root import home_from_env, otto_home

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("OTTO_HOME", "")
    assert home_from_env() is None
    assert otto_home() == tmp_path / "user" / ".otto"
    monkeypatch.setenv("OTTO_HOME", "~/elsewhere")
    assert home_from_env() == tmp_path / "user" / "elsewhere"
    assert otto_home() == tmp_path / "user" / "elsewhere"


def test_otto_home_is_one_function():
    import otto.config.home
    import otto.home_root

    assert otto.config.home.otto_home is otto.home_root.otto_home


def test_otto_env_settings_no_longer_reads_otto_home():
    from otto.models.settings import OttoEnvSettings

    assert "home" not in OttoEnvSettings.model_fields


@pytest.mark.parametrize(
    "roots",
    [
        [],
        [Path("/otto")],
        [Path("/xdir")],
        [Path("/srv/otto"), Path("/srv/xdir")],
        [Path("/a/otto-xdir")],
        [Path("/home/u/proj")],
    ],
    ids=["empty", "otto", "xdir", "otto+xdir", "otto-xdir", "plain"],
)
def test_no_workspace_key_can_be_the_xdir_dirname(roots):
    # A workspace key starts with 8 lowercase hex characters; `otto-xdir`
    # starts with `o`, `t`, `x`, none of them hex, so no deleter keyed on the
    # workspace pattern can ever reach the run tree.
    from otto.config.cache_maintenance import _KEY_RE
    from otto.config.home import workspace_key
    from otto.home_root import XDIR_DIRNAME

    assert workspace_key(roots) != XDIR_DIRNAME
    assert _KEY_RE.fullmatch(XDIR_DIRNAME) is None


@pytest.mark.parametrize("case", ["set", "empty", "unset", "tilde"])
def test_home_dir_is_one_answer_through_both_entry_points(tmp_path, monkeypatch, case):
    """``otto_home()`` and the completion shim's cache path derive the home one way."""
    from otto._shim_complete import locate_cache
    from otto.home_root import home_dir, otto_home

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    project = tmp_path / "proj"
    project.mkdir()
    environ = {"OTTOPATH": str(project), "HOME": str(tmp_path / "user")}
    if case == "set":
        environ["OTTO_HOME"] = str(tmp_path / "home")
    elif case == "empty":
        environ["OTTO_HOME"] = ""
    elif case == "tilde":
        environ["OTTO_HOME"] = "~/elsewhere"
    monkeypatch.delenv("OTTO_HOME", raising=False)
    if "OTTO_HOME" in environ:
        monkeypatch.setenv("OTTO_HOME", environ["OTTO_HOME"])

    assert str(otto_home()) == home_dir(environ)
    assert Path(locate_cache(environ)).parent.parent == Path(home_dir(environ))
    assert "~" not in home_dir(environ)
