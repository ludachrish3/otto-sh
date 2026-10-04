"""Read-only checks on how ``docs/conf.py`` runs the renderers of git-ignored pages.

A rendered page reaches a reader only because ``docs/conf.py`` runs its renderer
at ``builder-inited``. Every renderer goes through the one hook,
``_generate_rendered_pages``, over the ``_RENDERED_PAGES`` table, so each page's
tests ask the same three questions of that table and that hook.
"""

from tests._fixtures.paths import PROJECT_ROOT


def assert_conf_renders(module: str, page: str) -> None:
    """Assert ``docs/conf.py`` runs *module* for *page* on every builder, and raises on failure.

    Every builder, not html-only: a toctree names the page, so the doctest
    builder ``make docs`` also runs has to find it on disk. A non-zero exit
    raises, so a failed render is a build failure and not a warning.
    """
    conf = (PROJECT_ROOT / "docs" / "conf.py").read_text(encoding="utf-8")
    table = conf[conf.index("_RENDERED_PAGES = {") :]
    table = table[: table.index("\n}")]
    assert f'"{module}": "{page}"' in table, f"{module} is not in _RENDERED_PAGES"
    assert 'app.connect("builder-inited", _generate_rendered_pages)' in conf
    hook = conf[conf.index("def _generate_rendered_pages") :]
    hook = hook[: hook.index("\ndef ")]
    assert "for module, page in _RENDERED_PAGES.items():" in hook
    assert '"-m", module]' in hook
    assert "raise RuntimeError" in hook, "a failed render would only warn"
    assert "app.builder.name" not in hook, "the page is a source file; every builder needs it"
