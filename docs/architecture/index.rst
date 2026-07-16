Architecture
============

These pages describe how otto is put together and why it is shaped the way
it is — written for contributors. The :doc:`User Guide <../guide/index>`
explains how to *use* each functional area; each page here explains the
moving parts behind one, and the two link across rather than repeat each
other.

Start with the overview and the shared command lifecycle, then jump to the
area you are changing. The extensibility pages describe the registry
machinery every seam shares; the utilities are the cross-cutting spines;
the principles are the recurring design rules.

.. toctree::
   :caption: Overview
   :maxdepth: 1

   overview
   lifecycle

.. toctree::
   :caption: Design by area
   :maxdepth: 1

   subsystems/hosts
   subsystems/docker-hosts
   subsystems/execution
   subsystems/network
   subsystems/monitoring
   subsystems/coverage
   subsystems/reservations
   subsystems/bootstrap
   subsystems/data-boundary

.. toctree::
   :caption: Extensibility
   :maxdepth: 1

   subsystems/registries
   subsystems/extension-points

.. toctree::
   :caption: Utilities
   :maxdepth: 1

   utilities/logging
   utilities/results

.. toctree::
   :caption: Principles
   :maxdepth: 1

   principles
