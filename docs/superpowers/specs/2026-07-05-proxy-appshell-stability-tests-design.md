# Proxy-user & AppShell stability tests

**Status:** design — approved (pending spec review)
**Date:** 2026-07-05
**Author:** Chris Collins (with Claude)

## 1. Motivation

The 3.13 resync flake and the AppShell in-REPL-recovery caveat show that the login-proxy and AppShell paths are flake-prone under load/repetition. otto already has a soak harness (`make stability` → `stability-unit` / `stability-unix` / `stability-embedded`, driven by `pytest-repeat --count`; nightly runs `--count=100`). This adds soak tests to that harness to surface intermittent bugs in:

1. **Proxy-user command execution** — session establishment as a proxied user, `as_user` roundtrip (exercises the resync), `oneshot`, and concurrent fan-out.
2. **Proxy-user file transfer — all Unix backends.** Reconnaissance found a real gap: of `scp` (default) / `sftp` / `ftp` / `nc`, **only `nc` has proxied-user (ownership) coverage** (`test_login_proxy_e2e.py::test_nc_put_owned_by_proxied_user`), and no test asserts *content and ownership together* under the proxy.
3. **AppShell lifecycle** — launch → cmd(s) → quit and `_recover_session`-on-exit, on the real mysql bed and hostless (`LocalHost` `python3`), plus session-lock and concurrent-session isolation.

## 2. Goals / non-goals

**Goals**
- Soak the proxied-user command path as `mysql` (establishment, `as_user`, `oneshot`, fan-out).
- Soak proxied-user transfers across **scp / sftp / ftp / nc**: assert byte-identical content **and** `owner == mysql`; sequential (repeated by `--count`) plus one concurrent case.
- Soak AppShell: bed `mysql` (parsed SELECT correctness + host uncorrupted after) and hostless `PyRepl` (launch/cmd/quit lifecycle, `AppShellActiveError` lock, recover-on-exit, concurrent independent sessions).
- Integrate cleanly with the existing harness (correct markers/tiers), excluded from `make coverage` where heavy.

**Non-goals**
- No product-code changes (tests only).
- No new pytest markers; reuse `stability` / `integration` (auto-stamped) / `concurrency`.
- Embedded transfers (`console`, `tftp`) out of scope — login proxies are Unix-only and `tftp` is an unimplemented stub.

## 3. Design

### 3.1 Files (one responsibility each)

- **`tests/integration/host/test_proxy_user_stability_integration.py`** — proxied command + transfer soak. `pytestmark = [pytest.mark.timeout(120), pytest.mark.stability]` (auto-stamped `integration` by `tests/integration/`). Runs via `make stability-unix` (`-m "stability and integration"`, `--count=10`; nightly `--count=100`), excluded from `make coverage` (`-m "not stability"`).
- **`tests/integration/host/test_app_shell_stability_integration.py`** — bed `mysql` AppShell soak. Same markers, plus `pytest.mark.xdist_group("app_shell_stability")` to serialize.
- **`tests/unit/host/test_app_shell_concurrency.py`** — hostless `LocalHost` `PyRepl` soak. `pytestmark = [pytest.mark.concurrency]` (no bed; `tests/unit/` is not auto-`integration`). Runs via `make stability-unit` (`-m concurrency`, `--count=50`; nightly `--count=100`); also stays in `make coverage` (fast, single pass).

Rationale for the split: a hostless soak must be `concurrency`-marked to run in a stability tier (`stability-unix` requires `integration`, i.e. a bed; a `stability`-only hostless test would run in no tier), and `concurrency` tests live under `tests/unit/host/`.

### 3.2 Fixtures & host construction (reuse existing machinery)

- Register the `sudo-su-shell` login proxy at module scope (`overwrite=True`), mirroring `test_login_proxy_e2e.py` (`_sudo_su_shell` = `sudo su -s /bin/bash <login>`, undo `exit`). Zero shared-lab-data mutation — build hosts from inline dicts.
- `proxied_host` fixture: leases a Unix host (`lease_unix_host(tmp_path_factory.getbasetemp().parent, UNIX_POOL)`), reads its IP read-only via `tests._fixtures.labdata.host_data`, and builds `create_host_from_dict(_mysql_host_dict(ip, element, user="mysql", transfer=<param>))`. Parametrized `indirect` across `["scp", "sftp", "ftp", "nc"]`. Closed in `finally`.
- AppShell subclasses (`MySql`, `Select`/`Row`/`QueryStats`; `PyRepl`, `Version`) are redefined locally in each stability file (self-contained, matching how `test_app_shell_e2e.py` defines them) — not imported from the e2e module.

### 3.3 Cases (sequential single-op, repeated by `--count`, + fan-out)

Proxied command/transfer file:
- `test_proxied_command_roundtrip` — `whoami` == `mysql`; a real command's output intact.
- `test_proxied_as_user_roundtrip` — `vagrant` → `async with host.as_user("mysql")` (`whoami`==mysql) → `vagrant`. Soaks the su/exit resync.
- `test_proxied_transfer_content_and_ownership[scp|sftp|ftp|nc]` — `put` a random-payload file to a proxied-user (`user="mysql"`) host, then `get` it back byte-identical (all four backends). Ownership is asserted **backend-real**, not uniformly `mysql`: implementation (verified on the live bed) showed that transport-authenticated backends (`scp`/`sftp`/`ftp`) authenticate as the *resolved direct via-chain cred* (`vagrant`) — `ConnectionManager.credentials` returns the direct cred and no transfer backend replays `proxy_hops` ([connections.py](../../src/otto/host/connections.py)) — so files land owned by `vagrant`, whereas `nc` pipes through the already-proxied shell and lands owned by `mysql`. This is **expected, correct-by-design** (a non-loginable user like `mysql`, with sshd `DenyUsers`, cannot authenticate a transport transfer), decided 2026-07-05: the test asserts `owner == "mysql"` for `nc` and `owner == "vagrant"` for scp/sftp/ftp, pinning the current behavior. `nc` param pinned to `xdist_group("nc-serial")` (port TOCTOU).
- `test_proxied_oneshot_fanout` — N (=8) concurrent `oneshot("echo mysql_<i>")` as mysql; assert no exceptions, all-ok, no cross-contaminated output (pooled-session double-checkout guard).

Bed AppShell file:
- `test_mysql_appshell_cycle` — inside `host.app_shell(MySql)`: DROP/CREATE/INSERT/SELECT(`parse=Select`) asserting `stats.count == 3` and exact row tuples; then a plain `run("echo back")` after the block confirms the host session is uncorrupted (recover-on-exit).

Hostless concurrency file:
- `test_pyrepl_cycle` — `local_host.app_shell(PyRepl)`: run a `parse=Version` cmd; assert `.value.major == 3`; then `local_host.run("echo back")` == `back`.
- `test_run_blocked_while_attached` — via `open_session` + `PyRepl.attach`: `run` raises `AppShellActiveError` while attached; after detach the POSIX shell is restored.
- `test_concurrent_independent_pyrepls` — N (=4) `LocalHost` instances each running `app_shell(PyRepl)` concurrently; each computes a distinct value; assert no cross-session contamination.

### 3.4 Load safety

Per the dev-VM rule (single pass, no xdist storm): `nc` transfers pinned to `xdist_group("nc-serial")`; the bed AppShell file pinned to `xdist_group("app_shell_stability")`; leasing spreads command/transfer cases across carrot/tomato/pepper. Per-file `timeout` markers bound wedges.

## 4. Testing & validation

- Smoke each new file once on the free bed: `-m "stability and integration"` (or `-m concurrency` for the hostless file) with `--count=1` → green.
- Short soak: `--count=5` on the integration files, `--count=20` on the concurrency file → stable (no intermittent failures). If a case flakes, that is a *found bug* — file it (do not add `@pytest.mark.retry`, which would mask it).
- Confirm `make coverage` still excludes the `stability` files and stays green; confirm the `concurrency` file passes a single coverage pass.
- Run under `make stability-unix` / `make stability-unit` to confirm harness wiring.

## 5. Open questions

None outstanding — AppShell scope (both bed + hostless) and the concurrency/fan-out dimension were decided during brainstorming.
