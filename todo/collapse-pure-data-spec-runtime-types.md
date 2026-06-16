# Collapse the pure-data two-type splits into single frozen pydantic models

> **Status: Deferred to post-freeze (Pydantic Phase B-adjacent). Do NOT act pre-freeze.**
> Surfaced during Pydantic Phase A Plan 4 (monitor records), 2026-06-16.

## Context — the two-type split and where it's load-bearing

Phase A introduced the **two-type split**: a pydantic `*Spec` (the JSON boundary,
`extra='forbid'`) builds an *unchanged* runtime object via `to_runtime()`. For the
**library-forwarding** option types (`SshOptionsSpec`/`TelnetOptionsSpec`/
`SftpOptionsSpec`/`ScpOptionsSpec`/`FtpOptionsSpec`) the split is essential and stays:
the runtime `*Options` dataclass carries an `extra` dict, callables (`post_connect`),
and the `_kwargs()` / `_client_kwargs()` adapters that spread into
`asyncssh.connect()` etc. — pydantic must stay off that third-party seam.

## The opportunity — pure-data, otto-owned value types

For the **otto-owned, no-library-seam** value types there is no such seam: the spec and
its runtime twin are field-identical and `to_runtime()` is a trivial 1:1 copy. The
clearest cases are the three forward types:

- [`LocalPortForwardSpec`](../src/otto/models/options.py) → [`LocalPortForward`](../src/otto/host/options.py) (4 plain `str`/`int` fields)
- `RemotePortForwardSpec` → `RemotePortForward` (4 fields)
- `SocksForwardSpec` → `SocksForward` (2 fields)

Each spec's `to_runtime()` is a positional field copy; the runtime twin is a
`@dataclass(slots=True, frozen=True)`. These could be a **single frozen pydantic model**
serving as both the boundary validator and the immutable value object — eliminating the
duplicated field list, the boilerplate `to_runtime()`, and the per-type drift-guard.

## What unlocks it

pydantic v2 **merges** a subclass's `model_config` with the parent's (dict-update, not
replace), so a model can carry **both** `frozen=True` (its own) **and** `extra='forbid'`
(inherited from `OttoModel`) in one class — exactly the pattern Plan 4 used for
`SnmpMetric`. Verified empirically:

```python
class SnmpMetric(OttoModel):                 # OttoModel: extra='forbid'
    model_config = ConfigDict(frozen=True)
# -> SnmpMetric.model_config == {'extra': 'forbid', 'frozen': True}
```

So a frozen + forbid pydantic value object is a first-class option; a pure-data type does
**not** need a separate frozen-dataclass twin.

## Why it is NOT a Phase A fix (the deferral reason)

The runtime forward types are **consumed by the SSH transport/session adapters** (e.g.
`SshOptions._kwargs()` reads `local_forwards: list[LocalPortForward]` and hands them to
asyncssh). Collapsing the two types changes the type those consumers see, and Phase A's
lowest-risk principle deliberately does **not** touch the churny async consumer code
(transports/sessions/transfer). So this is a **post-freeze, semver-minor internal
refactor** — naturally folded into or run alongside **Pydantic Phase B**, which already
rewrites the options layer and touches that consumer code.

## Scope when picked up

- The three forward types are the cleanest, highest-confidence candidates.
- **Audit** the other otto-owned, no-`extra` option specs (`NcOptionsSpec`,
  `TftpOptionsSpec`, `SnmpOptionsSpec`) — some may also be pure data, but confirm none
  carry a runtime-only seam (a callable, an adapter, a `field_validator`-driven
  conversion like `TelnetOptionsSpec.login_prompt` str→bytes or `SnmpOptionsSpec.oids`
  list→tuple) before collapsing. Anything with a runtime-only conversion stays two-type.
- The five **library-forwarding** specs (Ssh/Telnet/Sftp/Scp/Ftp) **stay** two-type.

## Payoff

Removes the duplicated field list + boilerplate `to_runtime()` for the pure-data types,
gives them one source of truth, and retires their slice of the option-spec ↔ runtime
drift-guard test.

## See also

- [Pydantic Phase A design](../docs/superpowers/specs/2026-06-14-pydantic-phase-a-design.md) — §1 "Option models — the two-type split" (the rationale this refines), and "Out of scope (deferred) — Pydantic Phase B".
- [Fable-review sequencing](../docs/superpowers/specs/2026-06-13-fable-review-sequencing-design.md) — Phase B is "exactly the rewrite of that option-expansion layer".
- [todo/registry_builtin_registration_symmetry.md](registry_builtin_registration_symmetry.md) — a sibling deferred-hygiene note from the same migration.
