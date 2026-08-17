"""Project-level lifecycle: per-repo actions over the whole lab.

The layer between a repo's products and a one-line ``install`` command.
:class:`~otto.project.actions.ProjectActions` is what one repo does to the
fleet -- otto's owner-scoped defaults, or the subclass that repo registered
with :func:`~otto.project.actions.register_project_actions` -- and
:mod:`otto.project.state` is the vocabulary its answers are given in.

Composition ACROSS repos (dependency-ordered walks, the single host-level debug
sweep, the ``ensure_*`` converge layer) is :mod:`otto.project.orchestrator`'s,
and its module-level functions are re-exported here: ``otto.project.install()``
and friends are the lab-level verbs that instructions, suites, and fixtures
call with no arguments at all.
"""

from .actions import PROJECT_ACTIONS as PROJECT_ACTIONS
from .actions import ProjectActions as ProjectActions
from .actions import actions_for as actions_for
from .actions import register_project_actions as register_project_actions
from .orchestrator import cleanup as cleanup
from .orchestrator import ensure_clean as ensure_clean
from .orchestrator import ensure_installed as ensure_installed
from .orchestrator import ensure_uninstalled as ensure_uninstalled
from .orchestrator import get_logs as get_logs
from .orchestrator import install as install
from .orchestrator import install_tools as install_tools
from .orchestrator import is_clean as is_clean
from .orchestrator import status as status
from .orchestrator import uninstall as uninstall
from .state import InstallState as InstallState
from .state import ProjectStatus as ProjectStatus
