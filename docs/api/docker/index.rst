docker
======

The docker package provides image building, Compose orchestration, use-case
resolution and deployment, and file staging for workflows that run containers
on a remote parent host.

The user-facing model these modules implement -- fragments, provider
competition, placement and the env channels -- is documented in
:doc:`/guide/cli/docker/use-cases`.

.. automodule:: otto.docker

.. toctree::

   adapter
   build
   compose
   deployment
   resolve
   staging
