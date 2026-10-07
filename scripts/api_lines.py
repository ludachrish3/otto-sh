"""Tell the public-API golden's two schemas apart, and read the old one.

``tests/unit/api_snapshot/public_api.txt`` has had two schemas (spec
``docs/superpowers/specs/2026-10-04-public-api-manifest-design.md`` §6):

* **v2**, the golden since the cutover (#590): the API dump, which
  ``scripts/api_records.py`` defines and ``scripts/api_snapshot.py`` writes
  (dump spec ``docs/superpowers/specs/2026-10-05-api-dump-design.md``). This
  module only detects it: ``schema_of`` reads its ``# api-snapshot v2``
  header, which ``scripts/api_snapshot.py`` requires of the golden it checks.
* **v1**, the line format before the cutover. Nothing writes it any more.
  ``scripts/check_breaking_marks.py`` still reads it, through this module, to
  judge commits from before the cutover and to convert the last v1 golden at
  the cutover commit:

  * ``otto:<name>``: a root export;
  * ``<module>:<name>``: a deep path the docs teach;
  * ``<module>:``: a bare documented ``import``;
  * ``otto.host.host:Host.<method>(<params>)``: a Host protocol signature.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.api_records import V2_HEADER  # noqa: E402 -- path set up above

HOST_MODULE = "otto.host.host"
HOST_PREFIX = "Host."

_HOST_RE = re.compile(rf"^{re.escape(HOST_MODULE)}:({re.escape(HOST_PREFIX)}\w+)\((.*)\)$")


def schema_of(text: str) -> int:
    """Return 2 when *text* carries the ``# api-snapshot v2`` header line, else 1."""
    return 2 if any(line.strip() == V2_HEADER for line in text.splitlines()) else 1


def data_lines(text: str) -> list[str]:
    """Return *text*'s data lines: every non-empty line that does not start with ``#``."""
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def parse_host_line(line: str) -> "tuple[str, list[str]] | None":
    """Split ``otto.host.host:Host.<method>(<p1>, <p2>)`` into ``("Host.<method>", [p1, p2])``.

    Returns ``None`` for any other line.
    """
    match = _HOST_RE.match(line)
    if match is None:
        return None
    method, params = match.groups()
    return method, params.split(", ") if params else []


def host_line(method: str, params: list[str]) -> str:
    """Render a Host protocol line for *method* (no ``Host.`` prefix) taking *params*."""
    return f"{HOST_MODULE}:{HOST_PREFIX}{method}({', '.join(params)})"


def v1_kind(line: str) -> str:
    """Classify a v1 data line as ``root``, ``host``, ``bare``, ``deep`` or ``unknown``."""
    if parse_host_line(line) is not None:
        return "host"
    module, sep, name = line.partition(":")
    if not sep:
        return "unknown"
    if not name:
        return "bare"
    if module == "otto":
        return "root"
    return "deep"
