Architecture
============

These pages describe how otto is put together and why it is shaped the way it
is. They are written for contributors and for anyone extending otto beyond
what the :doc:`user guide <../guide/index>` covers — the guide explains how to
*use* each feature; this section explains the moving parts behind them, the
boundaries between subsystems, and the design rules that keep the codebase
coherent as it grows.

Start with the :doc:`overview` for the layer map and the life of an
invocation, then dive into the subsystem that concerns you.

.. toctree::
   :maxdepth: 1

   overview
   lifecycle
   registries
   hosts
   data-boundary
   results-and-logging
   test-pipeline
   monitoring-and-coverage
   extension-points
   principles
   docker-hosts
