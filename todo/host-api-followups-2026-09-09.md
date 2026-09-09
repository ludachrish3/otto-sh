# Host API follow-ups: disposition record (periodic review 2026-09-02, Tier 2 item 10)

**Purpose.** Review §1.3 concluded that freeze distance for the `Host`
contract is governed by one surface: the parity spec's §8 leftovers and the
session-user question. Item 10 asks for every open Host follow-up to be
dispositioned as **land now**, **defer past the freeze** (additive later, so
it never forces a break), **won't do**, or **closed**, so that the list of
*foreseeable breaking changes* is empty and a freeze (item 14, deferred by
Chris until the models settle) is a choice rather than a gamble.

Every claim below was checked against the tree at `c7870c43` (main,
2026-09-09) — file references are to that tree.

## Summary

| # | Follow-up | Breaks callers if done later? | Disposition | Needs Chris |
|---|---|---|---|---|
| 1 | `login(user=X)` refuses a fresh connection for a direct-cred user | no (lifting a refusal is additive) | **defer** | — |
| 2 | Telnet-term per-user `exec` | no (additive; grid note documents the limit) | **defer** | — |
| 3 | Proxy-chain users on stateless surfaces | no | **won't do** | — |
| 4 | `ftp` backend `user=` | no (grid note documents the limit) | **defer** | — |
| 5 | `open_session(name)` / `HostSession` take no `user=`; container identity rides `_pending_run_user` | no (`user=None` keyword is additive; the side channel is private) | **defer** (needs a small spec) | — |
| 6 | `ShellCommand` has no `user` field | no (new optional dataclass field) | **defer** | — |
| 7 | Get-side ownership semantics | — | **closed** (documented in `Host.get` and the families grid) | — |
| 8 | Four privilege mechanisms | consolidation WOULD break | **closed: keep all four**, one docs paragraph | — |
| 9 | CLI `--as-user` is reservation identity, colliding with the new `user=` | rename IS a CLI break — the one item that gets costlier with every user | **land now** (`!`) | **name** |
| 10 | `has_bash` and `_session_mgr` still have three homes each | no | **land now** | — |
| 11 | `log_stdout` is a lab-data key nothing reads | removal IS a break for a lab file that sets it | **land now** (`!`) | **yes/no** |
| 12 | `Expect` type alias defined twice (`host.py:70`, `session.py:68`) | no (same object) | **land now** | — |
| 13 | `Status` has no sanctioned re-export from `otto` | no (additive golden line) | **land now** | docs switch? |
| 14 | `compress=`, `stat()`, `.tainted` (`todo/TODO.md`) | no (new kwargs / methods) | **defer** | — |
| 15 | Coverage fetch calls `host.get(..., show_progress=)` via the protocol; container `get` lacked it | — | **closed** (protocol and `DockerContainerHost.get` both carry `show_progress` now; conformance green) | — |
| 16 | Four `BaseHost` public members outside the protocol | — | **closed** by item 11 (`56e6f251`) | — |
| 17 | Spec §3.4 "id tolerated" row, the repr "majority" wording, the stale `UnixHost` allowlist row | — | **land now** (spec amendment, docs only) | — |
| 18 | `has_bash` on the `Host` protocol so seven `getattr` sites become plain reads | no (additive attribute) | **landed** | ruled yes |
| 19 | Golden lists module-level functions by name only — keyword renames invisible to the breaking-mark check | no (tooling) | **defer** (before a freeze) | — |

**Status 2026-09-09 (evening):** Chris ruled yes on 9, 11, 13 and 18; all
"land now" items are on main (10/12/13-export/17 in `5d27fec9`–`7f55aa12`;
18/11/9/13-docs in `b286d9db`–`3e440542`). **The foreseeable-breaks list is
empty.** Every remaining item is additive by construction. One limit
surfaced while landing 9: the public-API golden records module-level
functions by name only, so a keyword rename such as
`resolve_username(as_user=)` → `holder=` is invisible to the breaking-mark
check and is covered only by the commit's `!` (item 19, below).

## The items

### 1. `login(user=X)` fresh-connection refusal (parity spec §8)

`src/otto/host/unix_host.py:618-660`: `login` resolves the target through
the connection's login target and raises `LoginProxyError("...starting a
fresh connection as X is not supported")` when the direct cred differs from
the session's. Lifting it means opening a second authenticated channel for
the interactive surface — the same primitive `exec(user=)` already uses.
Nothing about `Host.login(user)` changes when that lands. **Defer.**

### 2. Telnet per-user sessions (parity spec §8)

`exec(user=)` requires `term="ssh"` (unix row note in
`docs/guide/hosts/families.md`; `unix_host.py:776` explains telnet has no
stateless channel to authenticate on). Adding it is behaviour under an
existing signature; the conformance asserter's "declaration is per class,
behaviour per instance" paragraph already tells a telnet `UnixHost` what to
expect. **Defer.**

### 3. Proxy-chain users on stateless surfaces (parity spec §8)

Would need a hop-replay `exec` primitive. The grid note says "direct-cred
users only", which is the honest contract. **Won't do**; revisit only if a
lab appears whose target user is reachable solely through a proxy chain.

### 4. `ftp` backend user (parity spec §8)

The `ftp` backend authenticates with its own credentials
(`src/otto/host/transfer/ftp.py`); the unix row note says so. A future
`user=` there is additive. **Defer.**

### 5. Session user binding

`Host.open_session(name)` (`host.py:638`) takes no user; `HostSession.run`
and `send` (`session.py:1577`, `1697`) take none either. The container
family binds a session's identity at open through the private
`_pending_run_user` side channel (`docker_host.py:154`, `:726`), which is
what `SessionIdentity.bound_at_open` describes. The natural public shape is
`open_session(name, *, user=None)`: keyword, default `None`, so the golden
line grows a name and no caller breaks. It deserves a one-page spec (what a
non-`None` user means per family, and whether `HostSession.run(user=)` is
ever allowed) rather than a drive-by. **Defer**, with that spec as the
first step whenever session identity is next touched.

### 6. `ShellCommand.user`

`ShellCommand` (`host.py:291`) is a slotted dataclass whose `None` fields
inherit from the run-level kwargs. A `user: str | None = None` field
follows that pattern and is additive. It only matters once `run(user=)`
means something on more than the container family. **Defer.**

### 7. Get-side ownership

`Host.get`'s docstring (`host.py:710`) states the three-valued semantics
(unix authenticates, containers ignore because reads are
ownership-indifferent, the rest refuse) and the families grid publishes it
per family with the item 13 bed test as evidence. **Closed.**

### 8. Four privilege mechanisms

`run(sudo=True)` (per-command elevation), `as_user()` / `switch_user()`
(session identity, now protocol members), the userland-selected elevation
path (`unix_host.py:364`, `PosixPrivilege`), and the connection-level login
target (`connections.py:309`). They are four layers, not four spellings of
one thing, and item 11 just committed the middle two to the protocol.
Consolidating would be a break with no user asking for it. **Closed: keep
all four.** One docs paragraph in
`docs/guide/cli/host/capabilities/privilege.md` naming the four layers and
when each applies is worth landing with item 10's docs commit.

### 9. CLI `--as-user` name collision — land now, needs a name

`src/otto/cli/main.py:628`: `--as-user USERNAME` means "check reservations
as USERNAME instead of the current user" — reservation *holder* identity.
Since `6b3e35dd`, `user=` / `--user` on `exec`/`put`/`get` means "run as
that account on the target". Two meanings of "user" one flag apart. The
rename is a CLI break, and it is the single item whose cost grows with
every new user; today there is one. Proposed: `--holder USERNAME` (the
reservations vocabulary already says "holder" — `SupportsResourceHolders`
in `otto.reservations`), the completer renamed to match, no deprecated
alias (Chris deferred migration pointers while he is the only user), the
CLI docs pages that mention `--as-user` updated, commit `feat(cli)!`.
**Chris rules on the name.**

### 10. `has_bash` and `_session_mgr` — land now

Final review of item 11 (S2): both are answered by all five families yet
declared three times (`remote_host.py:213/307`, `local_host.py:172/213`,
`docker_host.py:114/148`). Evidence they belong on `BaseHost`: every family
answers them, and `BaseHost.current_user` reaching `self._session_mgr`
through a `# ty: ignore[unresolved-attribute]` (`host.py:1347`). (An earlier
draft claimed the seven `getattr(h, "has_bash", False)` consumers would
become plain reads; that was wrong — their receivers are typed as the
`Host` *protocol*, which does not declare `has_bash`. See item 18.) Move both
to `BaseHost` (`has_bash: bool = True`; `_session_mgr: "SessionManager" =
field(init=False, repr=False)`), keep `EmbeddedHost`'s `has_bash = False`
override (already in the allowlist), drop the three copies and the
type-ignore, and amend spec §3.2 so its rule ("common to all five
families") and §3.4's table agree. Non-breaking. **Land.**

### 11. `log_stdout` — dead key, land now, needs a yes

`log_stdout: bool = True` is declared on `RemoteHost` and
`DockerContainerHost`, accepted from lab data (`models/host.py:196`) and
documented in `docs/guide/configuration/lab-config.md` — and read by
**nothing** in `src/otto` (the only non-declaration hit is the spec's
field list). Removing it deletes a lab key (`HostSpec` is
`extra="forbid"`, so a lab file that sets it would fail to load) — a
break, but of a knob that has no effect. Alternatives: keep a dead field
forever, or wire it to something (there is nothing it was meant to do that
`log: LogMode` does not already do). Proposed: remove field, spec key and
docs line, commit `refactor(host)!`. **Chris rules yes/no.**

### 12. `Expect` twice — land now

`host.py:70` and `session.py:68` both define
`Expect = tuple[str | re.Pattern[str], str]`. `session.py` already imports
from `.host` (`session.py:56`), so it can import the alias instead of
restating it. Same object, no public change. **Land.**

### 13. `Status` re-export — land now

Five docs pages (eight fenced imports) teach `from otto.utils import
Status`; `otto.__all__` does not carry `Status`, so the golden does not
guard the path the docs teach. Add `Status` to `otto`'s lazy exports (one
additive golden line). Whether the docs then switch to `from otto import
Status` is a docs-churn call: one home per topic says yes, once, in the
same commit. **Land; Chris says whether the docs switch.**

### 14. `compress=`, `stat()`, `.tainted` — defer

`todo/TODO.md:37/59/64`. All additive (a new kwarg with a default, new
methods). None constrains the freeze. **Defer.**

### 15. Container `get(show_progress=)` — closed

The protocol's `get` carries `show_progress`
(`tests/unit/api_snapshot/public_api.txt:64`) and
`DockerContainerHost.get` accepts it (`docker_host.py:1024`); the
conformance asserter checks every family's `get` against the protocol and
the bed lane is green. **Closed.**

### 16. `BaseHost`-only public members — closed

`app_shell`, `as_user`, `switch_user`, `current_user` joined the protocol
in `56e6f251`. **Closed.**

### 18. `has_bash` on the `Host` protocol — needs a yes

Landed 2026-09-09 (`75ef347a`): `has_bash` and `_session_mgr` now live on
`BaseHost`. The seven `getattr(h, "has_bash", False)` sites in `src/otto`
(`tunnel/manage.py:205/925`, `tunnel/discovery.py:229`,
`tunnel/records.py:68`, `host/daemon.py:174`, …) still read through
`getattr` because their receivers come from `Lab.hosts: dict[str, Host]` —
the protocol — and the protocol does not name `has_bash`. Every family
answers it (it is a `BaseHost` field now), so adding `has_bash: bool` to
the protocol's attribute block is valid contract growth: one golden-visible
attribute, one `PROTOCOL_ATTRIBUTES` edit in the guard, and the seven sites
become plain reads. **Chris rules yes/no** (contract growth).

### 19. Golden is blind to keyword renames of module-level functions — defer

`scripts/api_snapshot.py` writes parameter names only for `Host` protocol
methods; every other public callable is a bare name. A keyword rename on
`resolve_username` or any `otto.__all__` function therefore moves no golden
line and `check_breaking_marks` cannot see it. Extending the line shape to
`module:func(params)` for every callable in `otto.__all__` is additive to
the tooling and would have caught item 9's second break automatically.
**Defer**; worth doing before a freeze.

### 17. Spec 2026-09-09 amendments — land with 10

§3.4's "the allowlist tolerates `id` during the migration" describes a state
that never happened (no leaf overrides `id`); §3.2's "majority wins" for
`repr` is the rationale but the landed rule is "identity fields shown,
bulk and provenance hidden"; and §3.4's `UnixHost` allowlist row still
lists `os_type`/`term`/`transfer`/`valid_terms`/`valid_transfers`, which the
landed guard does not (`{creds, os_name}`). Amend all three. **Land.**

## Proposed landing order

1. `refactor(host): has_bash and _session_mgr live on BaseHost` (+ spec
   amendments, + the privilege-layers paragraph) — items 10, 17, 8.
2. `refactor(host): one Expect alias` — item 12.
3. `feat(api): otto.Status` (+ docs switch if ruled) — item 13.
4. `feat(cli)!: --as-user becomes --holder` — item 9, after the name ruling.
5. `refactor(host)!: drop the unread log_stdout key` — item 11, after the
   yes/no.

Items 1–3 are non-breaking and start now; 4 and 5 wait for the rulings.
