"""``[coverage.tickets]`` runtime resolution — commit-message ticket ids.

Mirrors :mod:`otto.coverage.report_config`: the pydantic spec
(:class:`otto.models.settings.CoverageTicketsSpec`) validates the block at
settings-parse time; this module re-reads the raw dict at report time and
compiles the pattern.
"""

import re
import string
from dataclasses import dataclass
from typing import Any


class TicketConfigError(ValueError):
    """``[coverage.tickets]`` is malformed — raised loud, never rendered."""


@dataclass(frozen=True)
class TicketSpec:
    """A compiled ticket-id pattern plus an optional tracker URL template."""

    pattern: str
    url: str | None
    _regex: re.Pattern[str]

    def extract(self, message: str) -> list[str]:
        """Return every ticket id in *message*, deduped, in first-seen order.

        The id is the **whole match**, so the gutter shows what the commit
        actually wrote (``#1204``, not ``1204``).
        """
        seen: list[str] = []
        for match in self._regex.finditer(message):
            if match.group(0) not in seen:
                seen.append(match.group(0))
        return seen

    def url_for(self, ticket_id: str) -> str | None:
        """Render the tracker URL for *ticket_id*, or None when unconfigured.

        The template formats over the pattern's **named groups** plus ``{0}``
        for the whole match, so a URL can consume only part of the id.
        """
        if self.url is None:
            return None
        match = self._regex.fullmatch(ticket_id)
        if match is None:
            return None
        # ``{0}`` in str.format is a *positional* field, so the whole match
        # must be passed positionally; ``str.format`` happily ignores the
        # unused-by-name positional slot when the template only uses the
        # named groups.
        fields = {k: v or "" for k, v in match.groupdict().items()}
        return self.url.format(match.group(0), **fields)


def _url_field_names(url: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(url) if name]


def build_ticket_spec(pattern: str, url: str | None) -> TicketSpec:
    """Compile *pattern* and validate *url*'s field names against it."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise TicketConfigError(
            f"[coverage.tickets] pattern is not a valid regular expression: {exc}"
        ) from exc
    if url is not None:
        known = set(regex.groupindex) | {"0"}
        for name in _url_field_names(url):
            if name not in known:
                raise TicketConfigError(
                    f"[coverage.tickets] url references unknown group {name!r}; "
                    f"pattern defines {sorted(regex.groupindex)}"
                )
    return TicketSpec(pattern=pattern, url=url, _regex=regex)


def load_ticket_spec(cov_config: dict[str, Any]) -> TicketSpec | None:
    """Build a :class:`TicketSpec` from a raw ``[coverage]`` dict, or None.

    Returning None is the "feature absent" signal: no git log walk runs, no
    ticket data is emitted (no tickets page, no gutter column, no
    ``store.tickets``/``tickets.json``), and the coverage numbers
    themselves are unchanged.
    """
    tickets = cov_config.get("tickets")
    if not tickets:
        return None
    pattern = tickets.get("pattern")
    if not pattern:
        raise TicketConfigError("[coverage.tickets] requires a 'pattern' key")
    return build_ticket_spec(pattern, tickets.get("url"))
