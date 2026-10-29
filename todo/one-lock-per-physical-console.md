# One lock identity per physical console

test2's serial console is guarded by two locks that do not exclude each other:

- `tests/e2e/console/conftest.py` (`test2_lease`) takes the `test2` HOST lease
  (`tests/_fixtures/_host_pool.lease_unix_host`, file `unix_pool.test2`);
- the conformance `console` cells on test2 take the Zephyr console lock
  (`tests/_fixtures/_console_lock.console_access`, files `zephyr_console.*`)
  through `tests/conformance/_console_safety.serialized_console`.

Both live under `bed_lock_root()`, so they serialize across sessions, but
only against holders of the same file. A console e2e test and a conformance
console cell can still drive test2's one serial line at once.

`make chaos-embedded` (`tests/e2e/chaos/test_console_chaos.py`) opens
`zephyr37_fat`'s console with no console lock at all.

To do: decide, in one place, which lock file guards a given physical console
(keyed by the console endpoint, not by which tree the test lives in). Then
move all three holders onto it. The gaps are listed in
docs/architecture/testing.md ("What the bed venue does not cover").
