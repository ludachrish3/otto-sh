"""No doc or example counts GCOV_PREFIX_STRIP by hand (#788).

Otto computes the strip from a product's ``build_root``
(:func:`otto.host.product.gcov_prefix_strip`): a page or an example teaches
``{gcov_prefix_strip}`` or ``product.gcov_prefix_strip``, never a number, and
no ``docker_image`` entry sets either gcov variable that otto now sets on the
``docker run`` it owns. The generated ``captures/`` are output, not teaching,
and are regenerated from the examples.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from otto.config.project import Project
from otto.host.docker_image_kind import _docker_image_kind
from tests._fixtures.paths import CHECKOUT_ROOT

DOCS = CHECKOUT_ROOT / "docs"
EXAMPLES = DOCS / "examples"
_HAND_COUNT = re.compile(r"GCOV_PREFIX_STRIP=(?!\{[A-Za-z_.]*gcov_prefix_strip\}|<n>|…)\S")
"""A ``GCOV_PREFIX_STRIP=`` followed by anything but otto's number: the
``{gcov_prefix_strip}`` placeholder (bare or off a product), or the docs'
``<n>``/``…`` notation for a value otto fills in."""


def _teaching_files() -> list[Path]:
    pages = [
        p
        for p in DOCS.rglob("*.md")
        if p.relative_to(DOCS).parts[0] not in ("superpowers", "_build")
    ]
    examples = [
        p
        for p in EXAMPLES.rglob("*")
        if p.is_file()
        and (p.suffix in (".py", ".toml", ".sh") or p.name == "Dockerfile")
        and "captures" not in p.relative_to(EXAMPLES).parts
    ]
    # The fixture projects' own build files: project5's build.sh and
    # Dockerfile counted it by hand before #788. Their tests' .py files stay
    # out of scope, since an expectation there holds the literal number.
    projects = [
        p
        for p in (CHECKOUT_ROOT / "tests").glob("project*/**/*")
        if p.is_file() and (p.suffix in (".sh", ".toml") or p.name == "Dockerfile")
    ]
    return sorted(pages + examples + projects)


# Every hand count this change removed, one per spelling, and the spellings
# that teach otto's number instead.
_HAND_COUNTED = [
    'f"GCOV_PREFIX={product.cov_dir} GCOV_PREFIX_STRIP={STRIP} /tmp/quickstart/{name} {args}"',
    'docker build --build-arg GCOV_PREFIX_STRIP="$strip_count" -f Dockerfile build',
    'f"GCOV_PREFIX_STRIP={len(PRODUCT_DIR.parts) - 1} "',
    "exec 'GCOV_PREFIX_STRIP=$(python3 -c \"print(3)\") ./tool'",
    "ENV GCOV_PREFIX_STRIP=$GCOV_PREFIX_STRIP",
    'install = "GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP=3 ./app"',
]
_OTTOS_NUMBER = [
    'install = "GCOV_PREFIX={cov_dir} GCOV_PREFIX_STRIP={gcov_prefix_strip} ./app"',
    'env = f"GCOV_PREFIX={product.cov_dir} GCOV_PREFIX_STRIP={product.gcov_prefix_strip}"',
    'f"GCOV_PREFIX_STRIP={self.gcov_prefix_strip}"',
    "`docker run … -e GCOV_PREFIX=<cov_dir> -e GCOV_PREFIX_STRIP=<n> <image>`",
    '"""``GCOV_PREFIX=… GCOV_PREFIX_STRIP=…`` for *host*\'s ``product``."""',
    "no `GCOV_PREFIX_STRIP` is needed",
]


@pytest.mark.parametrize("line", _HAND_COUNTED)
def test_the_guard_flags_every_hand_counted_spelling(line):
    assert _HAND_COUNT.search(line)


@pytest.mark.parametrize("line", _OTTOS_NUMBER)
def test_the_guard_passes_otto_s_number(line):
    assert not _HAND_COUNT.search(line)


def test_no_page_or_example_counts_the_strip_by_hand():
    found = [
        f"{path.relative_to(CHECKOUT_ROOT)}:{n}: {line.strip()}"
        for path in _teaching_files()
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if _HAND_COUNT.search(line)
    ]
    assert found == [], "\n".join(found)


def _docker_image_entries():
    roots = [p.parent.parent for p in EXAMPLES.glob("**/.otto/settings.toml")]
    roots += [
        p.parent.parent for p in (CHECKOUT_ROOT / "tests").glob("project*/.otto/settings.toml")
    ]
    for root in sorted(roots):
        if 'kind = "docker_image"' not in (root / ".otto" / "settings.toml").read_text():
            continue
        for entry in Project(root=root).declared_products:
            if entry.kind == "docker_image":
                yield pytest.param(entry, id=f"{root.relative_to(CHECKOUT_ROOT)}:{entry.name}")


@pytest.mark.parametrize("entry", list(_docker_image_entries()))
def test_every_docker_image_entry_leaves_the_gcov_env_to_otto(entry):
    # The factory expands run_args and runs refuse_gcov_env over the result.
    _docker_image_kind(entry, SimpleNamespace(id="h1"))
