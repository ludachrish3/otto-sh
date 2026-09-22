# `--user` completes to the logins the host actually accepts

**Status:** draft for review, 2026-09-22.
**Breaking:** no. The completion-cache schema bumps (19 → 20), which costs one
silent rebuild; no CLI surface or Python API changes shape.

## 1. Goal

`otto host dut1 exec --user <TAB>` offers the logins `dut1`'s lab record
declares — and only those the verb will accept: `exec`/`login`/`probe` take
every cred login (proxy hops are replayed), `get`/`put` take only direct-cred
logins, and a family whose capability grid refuses `user=` offers nothing.
`--term telnet` narrows the set to creds scoped to that protocol. The warm TAB
is answered by the completion shim from the cache, at today's three-module
cost; it never builds a host and never opens a transport.

The rule: **a value TAB offers is a value the command would accept.** A
suggestion that then fails with *"direct-cred users only"* is a completion
bug, not a user error.

## 2. Today

- `--user` on every host verb is synthesized from the Python signature by
  `build_cli_binding` (`src/otto/cli/param_synth.py`). The only per-parameter
  completer hook is the `Opt(remote_path="any"|"dir")` marker, wired through
  `_remote_completer`; no `user` parameter carries any completer, so the tree
  serialises it `{"kind": "none"}` and TAB returns nothing.
- The verbs with a `--user`: `exec` (`BaseHost`, docker override), `login`,
  `get`/`put` (unix, embedded, local, docker overrides), `probe`
  (`userland.py`, which has no `Opt` marker at all). `owner=` on the
  product/dev-tool verbs is a registry key, not a login.
- The shim (`src/otto/_shim_complete.py`) already captures the typed host id
  (`Resolution.host_id`, via the group's `scoped_by`) to pick the verb menu,
  but `_source_values`/`_payload_values` never see it — the only
  argument-dependent source is lab scoping, driven by the root `--lab`
  (`_note_value` records nothing else).
- Cache collectors work from `HostSummary` and must not build hosts.
  `HostSummary` (`src/otto/labs/protocol.py`) carries `id`, `labs`, `ip`,
  `docker_capable`, `os_type` and its docstring rules creds out. Logins are a
  data fact of the lab record (`HostSpec.creds: list[CredSpec]`), never a
  device fact.
- The candidate set differs per verb by construction: `ConnectionManager
  .has_direct_cred` (`src/otto/host/connections.py`) is `resolve_chain(creds,
  user, "ssh")` yielding no hops; `cred_for`/`default_login`
  (`src/otto/host/login_proxy.py`) apply `Cred.protocols` scoping.

## 3. Design

### 3.1 `HostSummary.logins` — the data

`src/otto/labs/protocol.py` gains

```python
@dataclass(frozen=True)
class LoginSummary:
    login: str
    protocols: list[str]   # Cred.protocols; [] = unscoped
    proxy: bool            # reachable only through a login-proxy hop
```

and `HostSummary.logins: list[LoginSummary] = field(default_factory=list)`.
The default is the degradation: a backend that never fills it keeps today's
behaviour (no user completion) rather than breaking. `HostSummary`'s docstring
is revised — logins are identity, not creds; **passwords, `via`, `params` and
the proxy registry key never enter a summary**.

Fillers:

- **json backend** (`src/otto/labs/json_repository.py`, the
  `SupportsHostSummaries` fast path): data-only from `HostSpec.creds` —
  `LoginSummary(c.login, list(c.protocols), proxy=c.proxy is not None)`.
- **`load_lab` fallback** (`otto.labs.host_summaries`): from the constructed
  host's `RemoteHost.creds` (`Cred` has the same three fields). Hosts without
  `creds` (local, embedded) summarise to `[]`.
- **composite** (`src/otto/labs/composite.py`): merges like `labs` — a host
  appearing in several sources keeps the winning source's logins, exactly as
  dispatch's later-overrides-earlier rule.

The backend conformance suite (`tests/unit/labs/`, the summary-vs-built-host
comparison `os_type` already goes through) compares `logins` against the
built host's creds, so a custom backend that implements the fast path cannot
drift from `load_lab`.

### 3.2 The payload — `names["logins_by_host"]`

`collect_logins_by_host(repos) -> dict[str, list[dict]]` in
`src/otto/config/completion_cache.py`, beside `collect_host_classes_by_id`
and shaped like it: one enumeration over `repo_host_summaries`, keyed by host
id, each entry `{"login": str, "protocols": [str], "proxy": bool}`, sorted by
login. Hosts with no logins are omitted (the completer offers nothing for a
missing key — same as an unknown host id).

Wired through `write_cache` as a new keyword, written from `entry()`'s
slow-path block in `src/otto/cli/main.py` alongside `hosts_by_lab`, and named
in `DELEGATED_NAMES_KEYS`. `SCHEMA_VERSION` and the shim's `SCHEMA` go 19 →
20 together.

### 3.3 The marker — `Opt(host_user="any" | "direct")`

`otto.utils.Opt` gains `host_user: Literal["any", "direct"] | None = None`,
the exact sibling of `remote_path`. `param_synth` gains `_user_completer
(marker)` beside `_remote_completer`, wired at the scalar-`Opt` site, and the
existing list/dict guard extends to `host_user` (a user is never a list).
`Arg` does not get the field: no host verb takes a user positionally.

| method | marker | why |
|---|---|---|
| `BaseHost.exec`, `BaseHost.login` | `host_user="any"` | proxy hops replayed |
| `userland.probe` | `host_user="any"` (adds the `Opt` it lacks today) | opens the session as that login |
| `UnixHost.get` / `UnixHost.put` | `host_user="direct"` | `has_direct_cred` |
| embedded / zephyr / local `get`, `put` | none | grid says `refused` |
| docker `exec` / `put` / `get` | none | `--user` names an account **inside the container** — a cred-derived list would be wrong |

Per-verb and per-family accuracy is free: `HostGroup._synthesize_command`
binds the **resolved host class's** method, so the override's marker (or its
absence) is what the tree records for that class's verb.

The completer itself lives in `src/otto/cli/completers.py`:

```python
@completion_source(kind="payload", key="logins_by_host",
                   host_scoped=True, flavour=..., term_scoped=True, sort=True)
```

— one factory producing the `"any"` and `"direct"` flavours, cache-then-live
like every neighbour: read `names["logins_by_host"]`, falling through to
`collect_logins_by_host(get_repos())` on a miss. It finds the typed host id
and `--term` by walking `ctx` to the `otto host` group's `params` (the
`selected_lab_names` walk, one level nearer). No host is built on either
path.

### 3.4 The shim

Two captures, one filter, all in `src/otto/_shim_complete.py`:

- `Resolution` gains `term: str | None`. `_note_value` — today keyed only to
  root `labs` — also records the host group's `term` option (the node whose
  `scoped_by` is set). `_descend` carries it down as it carries `host_id`.
- `complete` (the one place holding both `res` and the source) passes
  `res.host_id` and `res.term` into `_source_values` → `_payload_values`.
- `_host_logins(source, names, host_id, term)` beside `_lab_host_set`:
  entries for `host_id`; drop `proxy` entries when `flavour == "direct"`;
  when `term_scoped` and a term was typed, keep an entry only if its
  `protocols` is empty or contains the term. No `host_id` (impossible for a
  leaf under `otto host`, but the shim is defensive) → `[]`.

Filter semantics are stated once, in the completer docstring, and the shim
mirrors them verbatim — the differential test is the net (§4).

`flavour="direct"` reads the same fact `has_direct_cred` does (no hop) but is
not protocol-specific; `get`/`put` are ssh-only today, and `term_scoped`
supplies the protocol cut when `--term` is typed. This is stated in the
docstring so a future non-ssh transfer path knows where the seam is.

### 3.5 What does not change

- The reservation `usernames` payload and `--holder` completer: a different
  namespace, not reused.
- `HostSummary` consumers other than this collector: the new field has a
  default and is ignored by them.
- Dispatch: none of this touches how `--user` is *applied*.

## 4. Testing

- **Differential** (`tests/unit/shim/test_differential.py`): shim answer ==
  Typer answer for `otto host dut1 exec --user <TAB>`, `get --user` (proxy
  login dropped), `--term telnet exec --user` (ssh-only cred dropped;
  unscoped cred kept), a zephyr host (`[]`), an unknown host id (`[]`), a
  prefix fragment. The generator's host records grow a proxied cred and a
  protocol-scoped cred so those shapes are exercised.
- **Tree** (`tests/unit/config/test_completion_tree.py`): every completer is
  registered (no new `live`); the `exec`/`get` `--user` source dicts pinned;
  docker `exec --user` and embedded `get --user` still `{"kind": "none"}`.
- **Collector** (`tests/unit/config/test_completion_cache_unit.py`):
  `collect_logins_by_host` shape, omission of login-less hosts, no password
  in any entry (grep the written JSON for every fixture password).
- **Summary conformance** (`tests/unit/labs/`): json fast-path `logins` ==
  `load_lab` fallback `logins`; composite override rule.
- **param_synth**: `host_user` on a list/dict `Opt` raises like `remote_path`.
- **Import budget**: the existing warm-TAB snapshot proves the fast path is
  still three modules — no change expected, run as the guard it is.
- **Schema**: the version-pair test (cache `SCHEMA_VERSION` == shim `SCHEMA`)
  already exists; both move.

## 5. Risks

- **Login names in the cache file.** They are already there for `--holder`,
  and already readable in `lab.json`; the collector test pins that no
  password can follow them.
- **A backend that fills `logins` wrong** is caught by the conformance
  comparison, not at TAB time.
- **The docstring boundary on `HostSummary`** is deliberately loosened from
  "no creds" to "identity, never secrets" — this spec is the record of why.
