"""The executable names a netcat listener can wear, in one place.

The bed carries several netcat implementations (Debian's `nc.openbsd` and
`nc.traditional`, nmap's `ncat`, plus the `nc` / `netcat` alternatives that
point at one of them). The leftover-listener probe
(`tests/_fixtures/bed_hygiene.py`), the local orphan finder
(`scripts/find_test_orphans.py`) and the conformance table all need the same
answer to "which names are netcat", so they import this tuple rather than
each keeping a list that drifts.

Keep this module free of imports: `scripts/find_test_orphans.py` loads it on
machines where nothing else of the test tree is importable.
"""

NETCAT_EXEC_NAMES: tuple[str, ...] = ("nc", "nc.openbsd", "nc.traditional", "ncat", "netcat")
