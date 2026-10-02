"""Resolve the dashboard's ``[monitor]`` TLS declaration across repos."""

import ssl
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .errors import MonitorTlsError

if TYPE_CHECKING:
    from ..config.repo import MonitorSettings, Repo


def resolve_monitor_tls(repos: "Sequence[Repo]") -> "MonitorSettings | None":
    """Resolve the ``[monitor]`` TLS declaration across ``repos``.

    Fail-loud rules: more than one repo declaring *different* values is a
    configuration error; a declared cert/key path whose file is missing, or
    that cannot be loaded as a certificate/key pair, is an error (never a
    silent fall-back to HTTP, which would be a quiet security downgrade); no
    declaration at all means plain HTTP, which is simply "not configured".
    The caller frames the refusal.

    Args:
        repos: The configured repos; only ``name`` and ``monitor_settings``
            are read.

    Returns:
        The agreed settings, or ``None`` when no repo declares TLS.

    Raises:
        MonitorTlsError: Repos disagree, a declared file is missing, or the
            pair cannot be loaded.
    """
    declaring = [
        (r.name, r.monitor_settings) for r in repos if r.monitor_settings.tls_cert is not None
    ]
    if not declaring:
        return None
    names = sorted(name for name, _ in declaring)
    if len({(ms.tls_cert, ms.tls_key) for _, ms in declaring}) > 1:
        raise MonitorTlsError(
            f"[monitor] TLS settings disagree across repos ({', '.join(names)}); "
            "make them identical or declare TLS in only one settings.toml.",
            setting=None,
            repos=names,
        )
    settings = declaring[0][1]
    for field_name, path in (("tls_cert", settings.tls_cert), ("tls_key", settings.tls_key)):
        if path is not None and not path.is_file():
            raise MonitorTlsError(
                f"[monitor] {field_name} {path} does not exist or is not a file — "
                "fix .otto/settings.toml or create the certificate "
                "(see the monitor guide's 'Securing the dashboard' section).",
                setting=field_name,
                repos=names,
            )

    # Both files exist, but existence doesn't mean valid: a corrupted/
    # truncated/wrong-format PEM passes is_file() cleanly and then kills
    # uvicorn's serve task deep inside MonitorServer.serve() (ssl.SSLError
    # out of Config.load()) — which used to hang the startup poll loop
    # forever rather than surface anything (see the task-death check in
    # server.py's _uvicorn_signalled_started). Load the pair here, at the
    # point we can still refuse cleanly, instead of ever falling back to HTTP.
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
            str(settings.tls_cert),
            str(settings.tls_key) if settings.tls_key is not None else None,
        )
    except (ssl.SSLError, OSError) as err:
        key_desc = (
            str(settings.tls_key) if settings.tls_key is not None else "(none; bundled in tls_cert)"
        )
        raise MonitorTlsError(
            f"[monitor] tls_cert {settings.tls_cert} / tls_key {key_desc} "
            f"could not be loaded as a TLS certificate/key pair: {err}",
            setting="tls_cert",
            repos=names,
        ) from err
    return settings
