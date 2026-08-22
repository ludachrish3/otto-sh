project
=======

The project package is the layer between a repo's products and a one-line
``otto run install``. :class:`otto.project.actions.ProjectActions` is what ONE
repo does to the lab — otto's owner-scoped defaults, or the subclass that repo
registered with :func:`otto.project.actions.register_project_actions` — and
:mod:`otto.project.orchestrator` composes those actions across every configured
repo in dependency order, performing the host-global steps that belong to no
repo. :doc:`../guide/cli/run/defaults` is the guide-level treatment.

.. automodule:: otto.project

.. automodule:: otto.project.actions

.. automodule:: otto.project.orchestrator

.. automodule:: otto.project.state
