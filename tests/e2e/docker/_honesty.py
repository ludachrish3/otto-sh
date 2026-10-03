"""Parsers for the honesty differentials: what build prints, what the daemon lists.

Both read otto's real console output, so a hostless unit test pins each one
against output the product itself rendered
(``tests/unit/docker/test_honesty_parsers.py``). Imported by name; the leading
underscore keeps pytest from collecting this.
"""

import re
from dataclasses import dataclass

MARK = "OTTO-HONESTY"
"""Starts every row the passthrough asks the daemon for, so the rows are findable
in the console output that also carries otto's own command echo."""

DAEMON_LIST_COMMAND = (
    f"docker images --format '{MARK} {{{{.Repository}}}}:{{{{.Tag}}}} {{{{.ID}}}}'"
)
"""The daemon query, through ``otto host <id> exec``, which shares no code with the docker verbs."""

_BUILT = re.compile(
    r"(?P<image>\S+): built (?P<refs>\S+(?:, \S+)*)\s+(?P<id>[0-9a-f]{12})\s+\((?P<host>[^)]+)\)"
)
_SHORT_ID = re.compile(r"[0-9a-f]{12}")


@dataclass(frozen=True)
class BuiltLine:
    """One ``<repo>/<image>: built <refs>  <id>  (<host>)`` report line."""

    image: str
    """``<repo>/<image>``."""
    refs: "list[str]"
    """The references the line names."""
    image_id: str
    """The id the line names."""
    host: str
    """The host the line names."""


def parse_built_line(text: str, image: str) -> "BuiltLine | None":
    """Return the report line for *image* (``<repo>/<image>``) in *text*, or ``None``.

    Whitespace between fields is matched as any run, so the raw console text and
    the same text with its wrapping collapsed both parse.
    """
    for found in _BUILT.finditer(text):
        if found["image"] == image:
            return BuiltLine(image, found["refs"].split(", "), found["id"], found["host"])
    return None


def parse_daemon_rows(text: str) -> "dict[str, str]":
    """Return ``reference -> short id`` from the rows :data:`DAEMON_LIST_COMMAND` printed in *text*.

    The passthrough relays the daemon's output through the console logger, so a
    row arrives as ``INFO     @test3 > | OTTO-HONESTY repo1-api:latest cbd8571d4b6e``
    (and as ``         @test3 > | OTTO-HONESTY ...`` for the lines after the
    first), and the echo of the command itself carries the mark too. A row is
    what follows the mark and has docker's id shape; the echo's unexpanded
    ``{{.ID}}`` template does not.
    """
    rows: dict[str, str] = {}
    for line in text.splitlines():
        _, sep, rest = line.partition(f"{MARK} ")
        if not sep:
            continue
        fields = rest.split()
        if len(fields) >= 2 and _SHORT_ID.fullmatch(fields[1]):
            rows[fields[0]] = fields[1]
    return rows


def project_image_ids_command(compose_project: str) -> str:
    """The daemon query for the image id every container of *compose_project* runs.

    Asked through ``otto host <id> exec`` like :data:`DAEMON_LIST_COMMAND`, and
    marked the same way. Stopped containers count (``-a``): a stack that came up
    and died is still the stack that was deployed.
    """
    return (
        f"docker inspect -f '{MARK} {{{{.Image}}}}' "
        f"$(docker ps -aq --filter label=com.docker.compose.project={compose_project})"
    )


_IMAGE_ID = re.compile(r"sha256:(?P<id>[0-9a-f]{64})")


def parse_project_image_ids(text: str) -> "list[str]":
    """Return the full image ids the rows :func:`project_image_ids_command` printed in *text*.

    The daemon prints ``sha256:<64 hex>``; the daemon's own ``docker images``
    listing shows only the first 12 hex digits, so a caller compares by prefix.
    The echo of the command carries the mark too, followed by the unexpanded
    ``{{.Image}}`` template, which has no id shape and is skipped.
    """
    ids: list[str] = []
    for line in text.splitlines():
        _, sep, rest = line.partition(f"{MARK} ")
        if not sep:
            continue
        found = _IMAGE_ID.fullmatch(rest.strip())
        if found:
            ids.append(found["id"])
    return ids


_MISSING_IMAGE = re.compile(
    r"pull access denied"
    r"|failed to resolve reference"
    r"|repository does not exist"
    r"|manifest unknown"
    r"|manifest for \S+ not found"
    r"|[\w./-]+:[\w.-]+: not found"
    r"|no such image"
    # The registry cannot be reached at all (classic image store: `Get
    # "https://registry-1.docker.io/v2/": dial tcp ...: i/o timeout`, or `...:
    # lookup registry-1.docker.io ...`). Docker's own error for the image it
    # could not pull, so it is the same answer: nothing built it. The
    # containerd store wraps this in "failed to resolve reference", above.
    r"|dial tcp"
    r"|lookup registry"
)


def names_a_missing_image(text: str) -> bool:
    """Whether *text* carries docker's own words for an image it cannot find.

    ``compose up`` of an image that is not on the daemon tries to pull it, and
    docker says so in one of a few forms depending on the registry's answer and
    the compose version. Wrapping is collapsed first, so a phrase Rich broke
    across two console lines still reads as one. A ``not found`` counts only when
    it follows an image reference (``<name>:<tag>: not found``), never bare: a
    missing *command* (``sh: 1: docker: not found``) says it too.
    """
    return _MISSING_IMAGE.search(" ".join(text.lower().split())) is not None


_PS_ID = re.compile(r"^(?P<id>[0-9a-f]{12})\s")
_IMAGE_ROW = re.compile(r"^(?P<repo>\S+)\s+(?P<tag>\S+)\s+(?P<id>[0-9a-f]{12})\s")
_IMAGE_REF_ROW = re.compile(r"^(?P<ref>\S*:\S*)\s+(?P<id>[0-9a-f]{12})\s")


def parse_ps_ids(text: str) -> "dict[str, list[str]]":
    """``host id -> container ids`` from an ``otto docker ps`` fan-out: a row is
    a line starting with docker's 12-hex id under the last ``== host ==`` header."""
    rows: dict[str, list[str]] = {}
    host = ""
    for line in text.splitlines():
        if line.startswith("== ") and line.endswith(" =="):
            host = line[3:-3]
            rows.setdefault(host, [])
            continue
        found = _PS_ID.match(line)
        if host and found:
            rows[host].append(found["id"])
    return rows


def parse_images_rows(text: str) -> "dict[str, dict[str, str]]":
    """``host id -> {reference: short id}`` from an ``otto docker images`` fan-out.

    Reads both layouts docker prints under a ``== host ==`` header. The classic
    one is ``REPOSITORY  TAG  IMAGE ID  ...``, a row being ``<repo> <tag> <12-hex id>``,
    keyed ``repo:tag``. The newer one is ``IMAGE  ID  DISK USAGE  ...`` (preceded by a
    ``WARNING:`` line), a row being ``<repo:tag> <12-hex id>``, keyed by its first field.
    Anything matching neither (the warning, the header rows, blank lines) is ignored,
    and a dangling ``<none>:<none>`` row is skipped in both layouts.
    """
    rows: dict[str, dict[str, str]] = {}
    host = ""
    for line in text.splitlines():
        if line.startswith("== ") and line.endswith(" =="):
            host = line[3:-3]
            rows.setdefault(host, {})
            continue
        classic = _IMAGE_ROW.match(line)
        if classic:
            key, short_id = f"{classic['repo']}:{classic['tag']}", classic["id"]
        else:
            by_ref = _IMAGE_REF_ROW.match(line)
            if not by_ref:
                continue
            key, short_id = by_ref["ref"], by_ref["id"]
        if host and key != "<none>:<none>":  # dangling images share one key; skip, never collapse
            rows[host][key] = short_id
    return rows


def parse_compose_ps_names(text: str) -> "dict[str, list[str]]":
    """``host id -> container names`` from an ``otto docker compose ps`` fan-out.

    Compose prints ``NAME  IMAGE  COMMAND  SERVICE  CREATED  STATUS  PORTS``; under the
    last ``== host ==`` header, every line with at least two fields whose first
    field is not ``NAME`` is a container, and its first field is its name.
    """
    rows: dict[str, list[str]] = {}
    host = ""
    for line in text.splitlines():
        if line.startswith("== ") and line.endswith(" =="):
            host = line[3:-3]
            rows.setdefault(host, [])
            continue
        fields = line.split()
        if host and len(fields) >= 2 and fields[0] != "NAME":
            rows[host].append(fields[0])
    return rows
