docker
======

The docker package provides image building, Compose orchestration, use-case
resolution and deployment, and file staging for workflows that run containers
on a remote parent host.

The user-facing model these modules implement -- fragments, provider
competition, placement and the env channels -- is documented in
:doc:`/cli/docker/use-cases`.

The build verbs, the observe verbs and the reports they and ``teardown``
return are imported from the package::

    from otto.docker import (
        build_on,
        compose_build,
        list_containers,
        container_logs,
        DockerVerbError,
        BuildReport,
        RepoBuild,
        FailedImage,
        HostReport,
        TeardownReport,
    )

.. automodule:: otto.docker
   :exclude-members: model_config, DockerVerbError

..
   DockerVerbError is excluded above because it is documented at its
   defining module (observe.rst) -- indexing it again here under
   ``otto.docker.DockerVerbError`` gave ``DockerVerbError`` two targets,
   which made any bare ``DockerVerbError`` xref elsewhere (e.g. a type
   annotation in invoke.py) ambiguous and -W-fatal.

.. toctree::

   adapter
   build
   build_verbs
   compose
   deployment
   mounts
   observe
   reports
   resolve
   staging
