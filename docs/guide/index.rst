User Guide
==========

How to use each of otto's functional areas, ordered the way a project
grows: set up the project, work with hosts, automate, shape the network,
observe the results, share the lab.

Each area corresponds to a first-party command:

.. list-table::
   :header-rows: 1

   * - Command
     - Section
   * - ``otto init`` / ``otto schema``
     - :doc:`Project setup <setup/index>`
   * - ``otto host``
     - :doc:`Hosts <hosts/index>`
   * - ``otto run``
     - :doc:`Running instructions <run/index>`
   * - ``otto test``
     - :doc:`Running test suites <test>`
   * - ``otto docker``
     - :doc:`Docker containers <docker>`
   * - ``otto link`` / ``otto tunnel``
     - :doc:`Links & tunnels <network/index>`
   * - ``otto monitor``
     - :doc:`Monitoring <monitor>`
   * - ``otto cov``
     - :doc:`Coverage <coverage>`
   * - ``otto reservation``
     - :doc:`Reservations <reservations>`

.. toctree::
   :maxdepth: 2

   setup/index
   hosts/index
   run/index
   test
   docker
   network/index
   monitor
   coverage
   reservations
   extending-cli
   cli-reference
