project
=======

The project package is the layer between a repo's products and a one-line
``otto run install``. :class:`otto.project.actions.ProjectActions` is what ONE
repo does to the lab — otto's owner-scoped defaults, or the subclass that repo
registered with :func:`otto.project.actions.register_project_actions` — and
:mod:`otto.project.orchestrator` composes those actions across every configured
repo in dependency order, performing the host-global steps that belong to no
repo. The first-party options classes a repo's override
inherits (``InstallOptions`` and its five siblings) are exported from
``otto.project``. :doc:`../guide/cli/run/defaults` is the guide-level
treatment, flag tables and all.

.. automodule:: otto.project

.. automodule:: otto.project.actions

.. Each field's rendered annotation is ``Annotated[bool, <OptionInfo object
   at 0x...>]`` -- an address-bearing repr no cross-reference can
   resolve, so the fields are excluded here; every flag's documentation home
   is the guide page's table. THE LIST MUST GROW WITH THE CLASSES: a new
   first-party options field that is not named here renders its OptionInfo
   repr and fails the docs gate under -W, in a build whose error names an
   address rather than the field that was added.

.. automodule:: otto.project.options
   :exclude-members: ensure, recover_partial, product_logs, debug_logs,
                     reset_impairments, remove_tunnels, require_product_logs,
                     dev, toolchain, full

.. automodule:: otto.project.orchestrator

.. automodule:: otto.project.commands

.. automodule:: otto.project.render

.. automodule:: otto.project.state
