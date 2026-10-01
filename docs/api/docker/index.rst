docker
======

The docker package provides image building, Compose orchestration, use-case
resolution and deployment, and file staging for workflows that run containers
on a remote parent host.

The user-facing model these modules implement -- fragments, provider
competition, placement and the env channels -- is documented in
:doc:`/cli/docker/use-cases`.

The build verbs and the reports they and ``teardown`` return are imported
from the package::

    from otto.docker import (
        build_on,
        compose_build,
        DockerBuildError,
        BuildReport,
        RepoBuild,
        FailedImage,
        HostReport,
        TeardownReport,
    )

.. automodule:: otto.docker
   :exclude-members: model_config, DockerBuildError

..
   DockerBuildError is excluded above because it is documented at its
   defining module (build_verbs.rst) -- indexing it again here under
   ``otto.docker.DockerBuildError`` gave ``DockerBuildError`` two targets,
   which made any bare ``DockerBuildError`` xref elsewhere (e.g. a type
   annotation in invoke.py) ambiguous and -W-fatal.

.. toctree::

   adapter
   build
   build_verbs
   compose
   deployment
   mounts
   reports
   resolve
   staging
