"""Shared Typer option callbacks used across multiple CLI subapps."""

import typer

from ..config import get_lab


def list_hosts_callback(value: bool) -> None:
    """Print all host IDs from the current lab and exit."""
    if not value:
        return
    lab = get_lab()
    typer.echo("")
    for host in lab.hosts:
        typer.echo(f"\u2022 {host}")
    typer.echo("")
