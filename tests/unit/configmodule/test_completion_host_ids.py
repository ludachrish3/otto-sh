"""collect_host_ids surfaces the built-in `local` host for tab completion."""

from otto.configmodule.completion_cache import (
    collect_docker_capable_host_ids,
    collect_host_ids,
)
from otto.host.builtin_hosts import BUILTIN_LOCAL_HOST_ID


def test_collect_host_ids_includes_builtin_local() -> None:
    # No repos → no hosts.json hosts, but the built-in local must still appear.
    ids = collect_host_ids([])
    assert BUILTIN_LOCAL_HOST_ID in ids


def test_docker_capable_excludes_builtin_local() -> None:
    ids = collect_docker_capable_host_ids([])
    assert BUILTIN_LOCAL_HOST_ID not in ids
