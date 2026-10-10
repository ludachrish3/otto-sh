"""Every registry capability otto declares, with its reason.

SHRINK-ONLY, like the cycle ratchet's BASELINE: entries are only removed. An
addition is a visible diff that review must accept; whether a reason is a good
reason is review's call too. test_approved_capabilities.py fails on any
difference from the live declarations, and on a blank reason.

Maps a registry key (``"module:ATTR"``) to its ``(capability type name, reason)``
pairs.
"""

APPROVED: dict[str, list[tuple[str, str]]] = {
    "otto.docker.adapter:COMPOSE_ADAPTERS": [
        ("RequireRepo", "one adapter per (repo, use case); the repo is half the key")
    ],
    "otto.instructions:PROJECT_ACTIONS": [
        ("RequireRepo", "one actions class per repo; the repo is the key")
    ],
}
