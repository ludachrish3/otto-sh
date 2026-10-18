"""Every registry capability otto declares, with its reason.

SHRINK-ONLY: entries are only removed. An
addition is a visible diff that review must accept; whether a reason is a good
reason is review's call too. test_approved_capabilities.py fails on any
difference from the live declarations, and on a blank reason.

Maps a registry key (``"module:ATTR"``) to its ``(capability type name, reason)``
pairs.
"""

APPROVED: dict[str, list[tuple[str, str]]] = {
    "otto.docker.adapter:COMPOSE_ADAPTERS": [
        ("RequireProject", "one adapter per (project, use case); the project is half the key")
    ],
    "otto.instruction_catalog:PROJECT_ACTIONS": [
        ("RequireProject", "one actions class per project; the project is the key")
    ],
}
