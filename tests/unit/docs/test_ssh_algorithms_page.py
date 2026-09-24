"""docs/architecture/ssh-algorithms.md IS the SSH algorithm matrix, rendered.

Modelled on test_lab_config_field_coverage.py's section-scoped style: read
the page, split on ``## `` headings, parse every ``| `name` | yes/no | note
|`` row under each into an :class:`SshAlgorithm`, and assert the parsed list
equals ``MATRIX`` — order included, since the page renders rows in matrix
(registry) order and that order is itself a pin
(test_legacy_ssh_algorithms.py's ``test_the_matrix_is_in_registry_order``).
A row present on the page but missing from, or differing from, ``MATRIX``
means the docs page fell behind the fixture the other three pins hold; a
row missing from the page entirely means a user has no way to discover it.
"""

import re

from tests._fixtures.paths import PROJECT_ROOT
from tests._fixtures.ssh_algorithm_matrix import (
    CIPHER,
    COMPRESSION,
    HOST_KEY,
    KEX,
    MAC,
    MATRIX,
    SshAlgorithm,
)

_PAGE = PROJECT_ROOT / "docs" / "architecture" / "ssh-algorithms.md"

_HEADING_TO_FAMILY = {
    "Key exchange": KEX,
    "Host key": HOST_KEY,
    "Cipher": CIPHER,
    "MAC": MAC,
    "Compression": COMPRESSION,
}

_SSH_OPTIONS_KEY = {
    KEX: "kex_algs",
    HOST_KEY: "server_host_key_algs",
    CIPHER: "encryption_algs",
    MAC: "mac_algs",
    COMPRESSION: "compression_algs",
}

_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(yes|no)\s*\|\s*(.*?)\s*\|\s*$", re.MULTILINE)


def _sections(text: str) -> dict[str, str]:
    """Split on ``## `` headings into ``{heading: section body}``."""
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    # parts[0] is the preamble before the first heading; then alternating
    # (heading, body) pairs.
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _parse_matrix() -> list[SshAlgorithm]:
    """Parse in the page's OWN heading order, not ``_HEADING_TO_FAMILY``'s: a
    reordered section must change the parsed order, or a reorder would pass
    silently against ``MATRIX``'s fixed family order."""
    text = _PAGE.read_text()
    sections = _sections(text)
    parsed: list[SshAlgorithm] = []
    for heading, body in sections.items():
        found = _ROW_RE.findall(body)
        if heading not in _HEADING_TO_FAMILY:
            assert not found, (
                f"ssh-algorithms.md: unknown section {heading!r} carries {len(found)} row(s)"
            )
            continue
        family = _HEADING_TO_FAMILY[heading]
        for name, stock, note in found:
            parsed.append(SshAlgorithm(family, name, stock == "yes", note))
    missing = set(_HEADING_TO_FAMILY) - set(sections)
    assert not missing, f"ssh-algorithms.md: missing section(s) {missing}"
    return parsed


def test_the_page_s_tables_equal_the_matrix_in_order():
    assert _parse_matrix() == MATRIX


def test_the_parser_found_every_family_and_at_least_one_row_each():
    text = _PAGE.read_text()
    sections = _sections(text)
    for heading in _HEADING_TO_FAMILY:
        assert heading in sections, f"missing {heading!r} section"
        rows = _ROW_RE.findall(sections[heading])
        assert rows, f"{heading!r} section has no parseable rows"


def test_every_section_names_its_ssh_options_key():
    text = _PAGE.read_text()
    sections = _sections(text)
    for heading, family in _HEADING_TO_FAMILY.items():
        key = _SSH_OPTIONS_KEY[family]
        assert key in sections[heading], f"{heading!r} section never mentions `{key}`"
