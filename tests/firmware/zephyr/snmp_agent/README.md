# otto test-bed SNMP agent (Zephyr module)

A minimal, **read-only SNMP v2c** responder built into the test-bed `shell_module`
firmware. It gives otto a performance-monitoring channel that does **not** contend
with the single Zephyr telnet shell session (which otto already uses for command
execution).

It is intentionally tiny — a fixed scalar OID table, hand-rolled BER, GET + GETNEXT
only. It is **not** a general SNMP agent and is not meant to leave the test bed.

## Wiring

- Pulled into the build via `-DEXTRA_ZEPHYR_MODULES=.../snmp_agent` (repo `Vagrantfile`).
- Gated by `CONFIG_OTTO_SNMP_AGENT` (set in `common/otto-overlay.conf`; defined in
  this module's `Kconfig`). Nothing compiles unless the symbol is on.
- Reached from otto via a per-instance `socat` UDP relay on the zephyr VM
  (`zephyr-snmp-relay-*` units in the `Vagrantfile`): `10.10.200.14:161NN` →
  `192.0.2.x:161`.

## Served OIDs

These must stay in lockstep with `_OTTO_BASE` in `src/otto/monitor/snmp.py`:

| OID | Metric | Type | Backing API |
|-----|--------|------|-------------|
| `1.3.6.1.2.1.1.3.0` | sysUpTime | TimeTicks | `k_uptime_get()` |
| `1.3.6.1.4.1.63245.1.1.0` | overall CPU (centi-%) | Gauge32 | `k_thread_runtime_stats_all_get()` |
| `1.3.6.1.4.1.63245.1.2.0` | heap used (bytes) | Gauge32 | `sys_heap_runtime_stats_get()` * |
| `1.3.6.1.4.1.63245.1.3.0` | heap free (bytes) | Gauge32 | `sys_heap_runtime_stats_get()` * |
| `1.3.6.1.4.1.63245.1.4.0` | thread count | Gauge32 | `k_thread_foreach()` |

\* Heap OIDs require `CONFIG_SYS_HEAP_RUNTIME_STATS` (3.7/4.x supplements only);
on 2.7 they report 0 and the agent still builds.

> `63245` is a **placeholder** Private Enterprise Number — apply for a real IANA
> PEN before this agent is used anywhere but the test bed.

## Version scope

The agent is built on **3.7 / 4.x only** (enabled via `common/otto-overlay-v3_7.conf`
and `-v4_4.conf`). It is **build-verified on 3.7** (compiles + links, agent object
in the image). **2.7 is excluded**: it predates the Zephyr 3.0 `zephyr/`-prefixed
include refactor and uses the older `SYS_INIT(fn(const struct device *))`
signature, so the C does not compile there. On 2.7 the module still *registers*
(so `CONFIG_OTTO_SNMP_AGENT` is a defined symbol) but stays off — the build is
clean, just agent-less. Enabling 2.7 is a follow-up: guard the includes and the
SYS_INIT signature on `KERNEL_VERSION_NUMBER`, confirm `k_thread_runtime_stats_all_get`
exists there, then add `=y` to a 2.7 supplement.

## Run-time verify checklist (NOT yet done)

The on-wire encoding has not been validated against a live instance. After
`vagrant provision zephyr`, confirm:

1. From the zephyr VM: `snmpget -v2c -c public 127.0.0.1:161 1.3.6.1.2.1.1.3.0` and
   `snmpwalk -v2c -c public 127.0.0.1:161 1.3.6.1.4.1.63245` return sane, moving values.
   (The hand-computed BER OID byte tables in `snmp_agent.c` are the likeliest bug site.)
2. From dev: `snmpget -v2c -c public 10.10.200.14:16101 1.3.6.1.2.1.1.3.0` through the relay.
