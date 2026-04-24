"""Shared Typer option callbacks used across multiple CLI subapps."""

from ..configmodule import getConfigModule


def list_hosts_callback(value: bool) -> None:
    """Print all host IDs from the current lab and exit."""
    if not value:
        return
    lab = getConfigModule().lab
    print()
    for host in lab.hosts:
        print(f'\u2022 {host}')
    print()
