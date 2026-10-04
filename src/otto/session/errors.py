"""The refusals :mod:`otto.session` raises — typed, structured, never CLI-spelled.

Each carries the facts its message was built from as attributes. A renderer
(the CLI) formats its own text from those attributes and never rewrites the
message: a message can quote a repo name or a path, and word-by-word flag
spelling would corrupt it.
"""

from typing import TYPE_CHECKING, Literal

from ..errors import FieldError, OttoError

if TYPE_CHECKING:
    from ..bootstrap import BootstrapError
    from ..env.preflight import Unsatisfied
    from .projects import DemotedRepo


class ProjectSelectionError(FieldError, ValueError):
    """``include_projects`` / ``exclude_projects`` cannot select what they name.

    ``kind`` is ``"unknown"`` (``names`` holds the one name no discovered repo
    carries; ``suggestion`` is the closest known name, or ``None``) or
    ``"overlap"`` (``names`` appear in both lists; ``field`` is
    ``include_projects``).
    """

    def __init__(
        self,
        message: str,
        *,
        field: str,
        kind: 'Literal["unknown", "overlap"]',
        names: list[str],
        suggestion: "str | None" = None,
    ) -> None:
        """Store the refusal's facts beside its message."""
        super().__init__(message, field=field)
        self.kind = kind
        self.names = list(names)
        self.suggestion = suggestion


class RepoLoadError(OttoError):
    """An active repo failed to load, so the run cannot start.

    ``errors`` are the fatal bootstrap errors: an active repo's, one forced by
    ``include_projects``, or one with no repo to attribute it to (an
    unparsable ``settings.toml``). ``demoted`` are the errors that would not
    have stopped the run on their own, so a renderer can still report them.
    """

    def __init__(
        self, errors: "list[BootstrapError]", demoted: "list[DemotedRepo] | None" = None
    ) -> None:
        """Frame every fatal error, one per line."""
        lines = "\n".join(f"  {error}" for error in errors)
        super().__init__(f"cannot run while a repo fails to load:\n{lines}")
        self.errors = list(errors)
        self.demoted = list(demoted or [])


class LabBuildError(FieldError):
    """The lab could not be built from the repos' configuration.

    ``kind``: ``"no_labs"`` (nothing selected), ``"unknown_lab"`` (the
    message IS the source's own not-found text), ``"sources"`` (a bad
    ``[[lab.sources]]`` entry or an unknown backend), ``"inventory"`` (a broken
    ``[inventory]`` declaration). ``detail`` is the underlying error's text.
    ``field`` is ``"labs"`` for the first two and ``None`` otherwise.
    """

    def __init__(
        self,
        message: str,
        *,
        field: "str | None",
        kind: 'Literal["no_labs", "unknown_lab", "sources", "inventory"]',
        detail: "str | None" = None,
    ) -> None:
        """Store the refusal's facts beside its message."""
        super().__init__(message, field=field)
        self.kind = kind
        self.detail = detail


class DependencyRefusedError(OttoError):
    """An active repo declares a Python requirement this environment does not meet.

    ``unsatisfied`` are the blocking requirements. ``warnings`` are what the
    preflight would have reported had it passed: checks it could not make,
    and the unmet requirements of inactive repos.
    """

    def __init__(
        self, unsatisfied: "list[Unsatisfied]", warnings: "list[str] | None" = None
    ) -> None:
        """One line per blocking requirement, the requirement quoted as written."""
        # An empty list still says something: an exception with an empty
        # message renders as a bare class name.
        super().__init__(
            "\n".join(
                f"repo {bad.repo!r} requires {bad.requirement!r} — not satisfied in "
                f"this environment (found: {bad.found})"
                for bad in unsatisfied
            )
            or "no unsatisfied requirements were given"
        )
        self.unsatisfied = list(unsatisfied)
        self.warnings = list(warnings or [])


_ACTIVATE = "add {project} to include_projects"


class InstructionInactiveError(OttoError):
    """A repo-owned instruction was asked for while its repo is inactive.

    ``reason``: ``"excluded"`` (switched off by ``exclude_projects``),
    ``"out_of_lab_scope"`` (no loaded lab matches the repo's ``lab_patterns``)
    or ``"host_starved"`` (the labs match, but its ``host_patterns`` select no
    host). The scope facts are empty for ``"excluded"``.
    """

    def __init__(
        self,
        instruction: str,
        owner: str,
        *,
        project: str,
        reason: 'Literal["excluded", "out_of_lab_scope", "host_starved"]',
        loaded_labs: "list[str] | None" = None,
        lab_patterns: "list[str] | None" = None,
        host_patterns: "list[str] | None" = None,
    ) -> None:
        """Say whose instruction it is, why that repo is inactive, and what would activate it."""
        self.instruction = instruction
        self.owner = owner
        self.project = project
        self.reason = reason
        self.loaded_labs = list(loaded_labs or [])
        self.lab_patterns = list(lab_patterns or [])
        self.host_patterns = list(host_patterns or [])
        loaded = ", ".join(self.loaded_labs)
        activate = _ACTIVATE.format(project=project)
        if reason == "excluded":
            why = f"which was switched off for this run (exclude_projects {project})"
            fix = f"remove {project} from exclude_projects, or {activate}"
        elif reason == "out_of_lab_scope":
            patterns = ", ".join(self.lab_patterns) or "(none)"
            why = f"which is inactive for the loaded lab(s) [{loaded}] (lab_patterns: {patterns})"
            fix = f"load a lab its lab_patterns match, or {activate}"
        else:
            patterns = ", ".join(self.host_patterns) or "(none)"
            why = (
                f"which is inactive: its [project] host_patterns ({patterns}) "
                f"match no host in the loaded lab(s) [{loaded}]"
            )
            fix = f"widen host_patterns, or {activate}"
        super().__init__(f"{instruction!r} belongs to repo {owner!r}, {why}; to activate it, {fix}")


class LoggingLevelsConflictError(OttoError, ValueError):
    """Two repos disagree about one logger's ``[logging.levels]`` entry.

    A settings error, so it subclasses ``ValueError`` like the model-level
    ones — but it cannot live in the model, which validates one repo's
    ``settings.toml`` and has never seen the others.
    """
