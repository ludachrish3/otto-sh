"""The refusals :mod:`otto.session` raises — typed, structured, never CLI-spelled.

Each carries the facts its message was built from as attributes. A renderer
(the CLI) formats its own text from those attributes and never rewrites the
message: a message can quote a project name or a path, and word-by-word flag
spelling would corrupt it.
"""

from typing import TYPE_CHECKING, Literal

from ..errors import FieldError, OttoError

if TYPE_CHECKING:
    from ..config.scope import BootstrapError
    from ..env.preflight import Unsatisfied
    from .projects import DemotedProject, RequiredProject


class ProjectSelectionError(FieldError, ValueError):
    """``include_projects`` / ``exclude_projects`` cannot select what they name.

    ``kind`` is ``"unknown"`` (``names`` holds the one name no discovered project
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


class ProjectLoadError(OttoError):
    """An active project failed to load, so the run cannot start.

    ``errors`` are the fatal bootstrap errors: an active project's, one forced by
    ``include_projects``, one of a project an active project requires, or one
    with no project to attribute it to (an unparsable ``settings.toml``).
    ``demoted`` are the errors that would not have stopped the run on their
    own, so a renderer can still report them. ``required`` explains each fatal
    error whose project is switched off or outside the labs but that an active
    project requires: which active projects require it.
    """

    def __init__(
        self,
        errors: "list[BootstrapError]",
        demoted: "list[DemotedProject] | None" = None,
        *,
        required: "list[RequiredProject] | None" = None,
    ) -> None:
        """Frame every fatal error, one per line, then why any inactive project still counts."""
        lines = [f"  {error}" for error in errors]
        lines += [f"  {entry.message}" for entry in required or []]
        joined = "\n".join(lines)
        super().__init__(f"cannot run while a project fails to load:\n{joined}")
        self.errors = list(errors)
        self.demoted = list(demoted or [])
        self.required = list(required or [])


class LabBuildError(FieldError):
    """The lab could not be built from the projects' configuration.

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
    """An active project declares a Python requirement this environment does not meet.

    ``unsatisfied`` are the blocking requirements. ``warnings`` are what the
    preflight would have reported had it passed: checks it could not make,
    and the unmet requirements of inactive projects.
    """

    def __init__(
        self, unsatisfied: "list[Unsatisfied]", warnings: "list[str] | None" = None
    ) -> None:
        """One line per blocking requirement, the requirement quoted as written."""
        # An empty list still says something: an exception with an empty
        # message renders as a bare class name.
        super().__init__(
            "\n".join(
                f"project {bad.project!r} requires {bad.requirement!r} — not satisfied in "
                f"this environment (found: {bad.found})"
                for bad in unsatisfied
            )
            or "no unsatisfied requirements were given"
        )
        self.unsatisfied = list(unsatisfied)
        self.warnings = list(warnings or [])


_ACTIVATE = "add {project} to include_projects"


class InstructionInactiveError(OttoError):
    """A project-owned instruction was asked for while its project is inactive.

    ``reason``: ``"excluded"`` (switched off by ``exclude_projects``),
    ``"out_of_lab_scope"`` (no loaded lab matches the project's ``lab_patterns``)
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
        """Say whose instruction it is, why that project is inactive, and what would activate it."""
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
        super().__init__(
            f"{instruction!r} belongs to project {owner!r}, {why}; to activate it, {fix}"
        )


class LoggingLevelsConflictError(OttoError, ValueError):
    """Two projects disagree about one logger's ``[logging.levels]`` entry.

    A settings error, so it subclasses ``ValueError`` like the model-level
    ones — but it cannot live in the model, which validates one project's
    ``settings.toml`` and has never seen the others.
    """
