Architecture
============

These pages describe how otto is put together and why it is shaped the way it
is — written for contributors and for anyone extending otto beyond what the
:doc:`user guide <../guide/index>` covers. The guide explains how to *use*
each feature; this section explains the moving parts behind them.

The section reads like a story, in order:

1. **The big picture** — what otto is and its pillars: the first-party
   commands.
2. **Command lifecycles** — the shared path every invocation walks (entry,
   bootstrap, dispatch, preamble, teardown), then what each pillar does
   differently once it takes over.
3. **Subsystems** — the machinery underneath: hosts, registries, the data
   boundary, and the extension seams.
4. **Utilities** — the cross-cutting spines every subsystem leans on.
5. **Principles** — the recurring design rules that keep the codebase
   coherent, and where each is enforced.

.. toctree::
   :caption: The big picture
   :maxdepth: 1

   overview

.. toctree::
   :caption: Command lifecycles
   :maxdepth: 2

   lifecycles/index

.. toctree::
   :caption: Subsystems
   :maxdepth: 1

   subsystems/hosts
   subsystems/registries
   subsystems/data-boundary
   subsystems/extension-points
   subsystems/docker-hosts

.. toctree::
   :caption: Utilities
   :maxdepth: 1

   utilities/logging
   utilities/results

.. toctree::
   :caption: Principles
   :maxdepth: 1

   principles
