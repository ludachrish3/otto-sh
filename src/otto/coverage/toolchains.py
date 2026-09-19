"""Which gcov reads each ``<cov>/<host>/<product>`` directory's counters.

Both consumers of a run's ``.gcda`` directories resolve the toolchain for
each directory here: the capture producer that ``otto test --cov`` runs at
collection's tail, and the reporter behind ``otto cov report``. One resolver
means the two cannot disagree about which gcov reads a product.
"""

from dataclasses import replace
from pathlib import Path

from ..host import toolchain_discovery
from ..host.toolchain import Toolchain


def resolve_toolchains(
    gcda_dirs: list[Path], recorded: dict[str, Toolchain]
) -> list[Toolchain | None]:
    """Resolve each gcda directory's toolchain, parallel to *gcda_dirs*.

    Per ``<cov>/<host>/<product>`` directory: the host's entry in *recorded*
    (keyed by host directory name) whose gcov is not the default's is used
    as is — otto does not second-guess a configured gcov. Otherwise the
    directory's own ``.gcda`` stamp chooses (``llvm-cov``, ``gcov-<major>``,
    or ``None`` for the system gcov) and replaces only the gcov of whatever
    record there is; a record that names an ``lcov`` keeps it. A ``None``
    entry means the merger's own defaults. The system gcov's major is probed
    once per call.

    Raises:
        otto.host.errors.CoverageToolMissingError: a tool the data needs is
            not on ``PATH``.
    """
    default_gcov = Toolchain().gcov_bin
    system_major: int | None = None
    probed = False
    result: list[Toolchain | None] = []
    for gcda_dir in gcda_dirs:
        host_record = recorded.get(gcda_dir.parent.name)
        if host_record is not None and host_record.gcov_bin != default_gcov:
            result.append(host_record)
            continue
        if not probed:
            system_major = toolchain_discovery.system_gcov_major()
            probed = True
        discovered = toolchain_discovery.discover_toolchain_from_gcda(
            gcda_dir, system_major=system_major
        )
        if discovered is None:
            result.append(host_record)
        elif host_record is None:
            result.append(discovered)
        else:
            result.append(replace(host_record, gcov=Path(discovered.gcov_bin)))
    return result
