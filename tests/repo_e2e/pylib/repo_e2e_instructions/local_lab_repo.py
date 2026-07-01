"""Custom LabRepository that wraps the JSON backend and adds a LocalHost to every lab.

Used by the e2e fixture repo (tests/repo_e2e) so that
``otto -l veggies host local run "echo hi"`` resolves the ``local`` host ID
without an SSH/telnet remote host. The LocalHost is injected on every
``load_lab`` call so ``otto host local run/put/get/login`` tests can run
without a live bed (``pytest.mark.hostless``).
"""

from pathlib import Path
from typing import Any

from otto.configmodule.lab import Lab
from otto.storage import JsonFileLabRepository, register_lab_repository
from otto.storage.errors import LabNotFoundError


class LocalHostLabRepository:
    """Wraps JsonFileLabRepository; injects a LocalHost into every returned lab.

    Constructor kwargs (passed from ``[lab.local_json]`` in settings.toml):
        hosts_dir: Path
            Directory that contains ``hosts.json``.  Forwarded to
            ``JsonFileLabRepository`` as the sole search path.
    """

    def __init__(self, repo_dir: Path, *, hosts_dir: str) -> None:
        self._json_repo = JsonFileLabRepository(search_paths=[Path(hosts_dir)])

    def load_lab(
        self,
        name: str,
        preferences: dict[str, Any] | None = None,
    ) -> Lab:
        """Load a lab from JSON and inject a LocalHost under the ``local`` key."""
        try:
            lab = self._json_repo.load_lab(name, preferences=preferences)
        except LabNotFoundError:
            # Still inject local even when no JSON hosts match — callers using
            # a lab that only needs the local host (e.g. tests/e2e hostless)
            # should not fail on a LabNotFoundError.
            lab = Lab(name=name)

        from otto.host.local_host import LocalHost

        local_host = LocalHost()
        if local_host.id not in lab.hosts:
            lab.add_host(local_host)

        return lab

    def list_labs(self) -> list[str]:
        """Return the JSON-backend labs.

        The ``local`` host is injected into each lab by :meth:`load_lab`, not as a lab of its own.
        """
        try:
            return self._json_repo.list_labs()
        except Exception:  # noqa: BLE001 — best-effort listing
            return []


register_lab_repository("local_json", LocalHostLabRepository)
