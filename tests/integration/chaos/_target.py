"""Chaos target host: loopback sshd by default, a lab bed host on request.

Tier-2 chaos tests point a real ``otto`` subprocess at one SSH-reachable
host (chaos spec, Tier 2). Default: the hermetic loopback sshd from
``_sshd``. Set ``OTTO_CHAOS_BED_HOST=carrot|tomato|pepper`` on the lab to
aim at a bed host instead — the otto subprocess still runs locally and
signals are only ever delivered to that local process.
"""

import asyncio
import dataclasses
import getpass
import json
from pathlib import Path

from tests._ambient_env import ambient
from tests._fixtures.labdata import lab_data_path
from tests._fixtures.paths import TESTS_ROOT

_REPO_E2E = TESTS_ROOT / "repo_e2e"


@dataclasses.dataclass(frozen=True)
class ChaosTarget:
    sut_dir: Path
    lab: str
    host_id: str
    ssh_host: str
    ssh_port: int
    ssh_username: str
    ssh_client_key: "Path | None"
    ssh_password: "str | None"


def make_loopback_target(root: Path, *, port: int, client_key: Path) -> ChaosTarget:
    """Generate the SUT dir + lab data for the loopback host and return the target."""
    user = getpass.getuser()
    tech_dir = root / "labdata" / "chaostech"
    tech_dir.mkdir(parents=True)
    (tech_dir / "lab.json").write_text(
        json.dumps(
            {
                "hosts": [
                    {
                        "ip": "127.0.0.1",
                        "element": "loopback",
                        "os_type": "unix",
                        "valid_terms": ["ssh"],
                        "valid_transfers": ["sftp", "scp"],
                        "is_virtual": True,
                        "creds": [{"login": user, "password": "unused-pubkey-auth"}],
                        "resources": ["loopback"],
                        "labs": ["chaos"],
                        "ssh_options": {
                            "port": port,
                            "client_keys": [str(client_key)],
                        },
                    }
                ]
            },
            indent=2,
        )
    )
    sut = root / "sut"
    (sut / ".otto").mkdir(parents=True)
    (sut / ".otto" / "settings.toml").write_text(
        f"""\
name = "chaos_harness"
version = "0.1.0"
lab_data_type = "json"
labs = [
    "{tech_dir}",
]

[lab]
backend = "json"
"""
    )
    return ChaosTarget(
        sut_dir=sut,
        lab="chaos",
        host_id="loopback",
        ssh_host="127.0.0.1",
        ssh_port=port,
        ssh_username=user,
        ssh_client_key=client_key,
        ssh_password=None,
    )


def make_bed_target(element: str) -> ChaosTarget:
    """Aim at a veggies bed host via the existing repo_e2e SUT (lab leg only)."""
    lab_json_path = lab_data_path("tech1")
    lab_json = json.loads(lab_json_path.read_text())
    host = next(h for h in lab_json["hosts"] if h["element"] == element)
    cred = host["creds"][0]
    return ChaosTarget(
        sut_dir=_REPO_E2E,
        lab="veggies",
        host_id=f"{element}_{host['board']}",
        ssh_host=host["ip"],
        ssh_port=22,
        ssh_username=cred["login"],
        ssh_client_key=None,
        ssh_password=cred["password"],
    )


async def _probe(target: ChaosTarget, command: str) -> "tuple[int, str]":
    import asyncssh

    kwargs: dict = {
        "username": target.ssh_username,
        "known_hosts": None,
        "port": target.ssh_port,
    }
    if target.ssh_client_key is not None:
        kwargs["client_keys"] = [str(target.ssh_client_key)]
    else:
        kwargs["password"] = target.ssh_password
    async with asyncssh.connect(target.ssh_host, **kwargs) as conn:
        result = await conn.run(command, check=False)
        status = result.exit_status if result.exit_status is not None else -1
        return status, str(result.stdout or "")


def probe(target: ChaosTarget, command: str) -> "tuple[int, str]":
    """Run ``command`` over a fresh, independent SSH connection (remote-hygiene oracle)."""
    return asyncio.run(_probe(target, command))


def bed_host_override() -> "str | None":
    return ambient("OTTO_CHAOS_BED_HOST") or None
