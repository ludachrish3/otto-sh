import importlib.util

from tests._fixtures.paths import PROJECT_ROOT

_MODULE_PATH = PROJECT_ROOT / "scripts" / "lint_docs_versions.py"


def _load_linter():
    spec = importlib.util.spec_from_file_location("lint_docs_versions", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lint_text(text: str, tmp_path):
    mod = _load_linter()
    f = tmp_path / "page.md"
    f.write_text(text)
    return mod.lint_file(f)


def test_hardcoded_pin_is_flagged(tmp_path):
    offenses = _lint_text("Install with `pip install otto-sh==0.5.4`.\n", tmp_path)
    assert len(offenses) == 1
    assert offenses[0][0] == 1


def test_hardcoded_wheel_filename_is_flagged(tmp_path):
    offenses = _lint_text("pip install otto_sh-0.3.0-py3-none-any.whl\n", tmp_path)
    assert len(offenses) == 1


def test_hardcoded_release_url_is_flagged(tmp_path):
    text = "curl -LO https://github.com/x/y/releases/download/v0.3.0/otto.whl\n"
    assert len(_lint_text(text, tmp_path)) == 1


def test_hardcoded_version_assignment_is_flagged(tmp_path):
    assert len(_lint_text("VERSION=0.3.0\n", tmp_path)) == 1


def test_token_spellings_are_clean(tmp_path):
    text = (
        "pip install otto-sh==%OTTO_VERSION%\n"
        "VERSION=%OTTO_VERSION%\n"
        "otto_sh-%OTTO_VERSION%-py3-none-any.whl\n"
        "releases/download/v%OTTO_VERSION%/x.whl\n"
    )
    assert _lint_text(text, tmp_path) == []


def test_token_typo_is_flagged(tmp_path):
    offenses = _lint_text("pip install otto-sh==%OTTO_VERSN%\n", tmp_path)
    assert len(offenses) == 1


def test_historical_prose_version_is_clean(tmp_path):
    # e.g. guide/coverage.md's "v1 (shipped in v0.8.1)" must NOT be flagged.
    assert _lint_text("This field was added in v0.8.1 and pins shape.\n", tmp_path) == []


def test_prefixed_version_assignment_is_clean(tmp_path):
    # release_process.md's `make release NEW_VERSION=0.4.0rc1` documents the
    # release interface with a deliberate example version — not otto-version rot.
    assert _lint_text("make release NEW_VERSION=0.4.0rc1\n", tmp_path) == []


def test_live_docs_tree_is_clean():
    mod = _load_linter()
    assert mod.main([str(PROJECT_ROOT / "docs")]) == 0


def test_main_flags_offense_in_scanned_tree(tmp_path):
    mod = _load_linter()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "page.md").write_text("pin: otto-sh==0.5.4\n")
    assert mod.main([str(docs)]) == 1


def test_main_skips_offense_under_skip_parts(tmp_path):
    mod = _load_linter()
    docs = tmp_path / "docs"
    (docs / "superpowers").mkdir(parents=True)
    (docs / "superpowers" / "plan.md").write_text("pin: otto-sh==0.5.4\n")
    (docs / "clean.md").write_text("all good\n")
    assert mod.main([str(docs)]) == 0
