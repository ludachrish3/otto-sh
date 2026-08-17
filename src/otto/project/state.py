"""Install-state vocabulary for project-level actions (pure data, import-light).

Deliberately free of otto imports: every layer above — per-repo actions, the
orchestrator, instructions, fixtures — reads this vocabulary, and a leaf with no
edges can be imported from any of them without a cycle to hold apart.

>>> from otto.project.state import InstallState, ProjectStatus
>>> status = ProjectStatus(overall=InstallState.PARTIAL, repos={"app": InstallState.PARTIAL})
>>> status.overall
<InstallState.PARTIAL: 'partial'>
>>> ProjectStatus(overall=InstallState.UNINSTALLED).repos
{}
"""

import enum
from dataclasses import dataclass, field


class InstallState(enum.Enum):
    """Tri-state install answer for one repo or for the lab as a whole.

    THE MIDDLE MEMBER IS WHY THIS IS NOT A BOOLEAN. A half-installed lab and a
    clean one are the same answer to ``is_installed()`` -- False -- and acting
    on that answer reinstalls on top of remnants. ``PARTIAL`` is the
    error-recovery signal ``ensure_installed(recover_partial=True)`` keys on:
    tear down first, then install fresh.
    """

    INSTALLED = "installed"
    PARTIAL = "partial"
    UNINSTALLED = "uninstalled"


@dataclass
class ProjectStatus:
    """Lab-level aggregate plus the per-repo detail behind it.

    Both halves are reported because they answer different questions: the
    aggregate decides whether a fixture converges, while the per-repo map says
    WHICH repo is the odd one out. A repo that is not counted (see the
    orchestrator's counted-repo rule) is absent from :attr:`repos` entirely
    rather than present with a made-up state.
    """

    overall: InstallState
    """The lab's answer across every counted repo."""

    repos: "dict[str, InstallState]" = field(default_factory=dict)
    """Per-repo state, keyed by repo name; counted repos only."""
