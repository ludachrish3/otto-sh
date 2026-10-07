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
