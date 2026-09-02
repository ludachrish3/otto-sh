# Container users — a uniform `user` parameter across the host API

**Status:** draft for review
**Date:** 2026-08-31
**Depends on:** 2026-08-30-docker-use-cases-design.md (shipped as 0.10.0's use-case machinery)

## 1. Problem

A container's access user is unspecifiable today. `docker exec` runs as the
image's `USER`; transfers land with `docker cp`'s ownership; the interactive
channel refuses the one user knob the host API has (`login(as_user=...)`
raises on `DockerContainerHost`). Meanwhile a container frequently runs as a
non-root UID (`1000:1000`, `postgres`) and the operator legitimately needs
both "run this as the service user" and "get a root shell in there".

The broader survey: `as_user` exists on exactly one surface — `login()`.
`run`/`exec`/`put`/`get` have no user concept on any host type. The root
`otto --as-user` flag is an unrelated concept (reservation identity).

## 2. Decision summary

1. **One name: `user`.** The host API's user-switching parameter is `user`
   on every surface that takes one. The existing `login(as_user=...)` is
   RENAMED to `login(user=...)` — a breaking rename riding the 0.10.0
   breaking window. The synthesized CLI flag becomes `--user` wherever the
   parameter appears.
2. **Uniform contract, per-host-type implementation.** `login`, `run`,
   `exec`, `put`, `get` all accept `user: str | None = None` at the
   protocol level. A host type that has semantics for it implements it; a
   host type that does not REFUSES loudly per call (never a silent ignore).
   - `DockerContainerHost`: implements all five (details §4).
   - `UnixHost`: `login(user=...)` keeps today's behavior (rename only).
     `run/exec/put/get` with a non-None `user` refuse loudly — a unix
     run-as-user needs a sudo/su privilege story, which is a ledgered
     follow-up, not smuggled in here.
   - Embedded/serial hosts: refuse loudly (no user concept on a console).
   - LocalHost: refuses loudly on every new surface (otto already runs as
     the invoking user; local transfers keep the invoking user's ownership).
3. **Declared per-service defaults** for containers, in the compose file
   inventory (§3). Per-call `user` overrides the declared default; the
   declared default overrides nothing (docker's image `USER` prevails when
   neither is set).
4. **Out of scope, explicitly:**
   - The root `otto --as-user` reservation-identity flag is UNTOUCHED (a
     different concept; renaming it would collide with the new verb flags).
   - The container's RUNTIME user (compose `user:` key) stays the product's
     compose-file concern — lab-varied via the env channel
     (`user: ${SERVICE_USER:-1000:1000}`), documented, no otto surface.
   - Unix run-as-user (sudo/su semantics): follow-up.

## 3. Declared defaults — schema

`[[docker.composes]]` gains an optional `users` table mapping service name
to access user:

```toml
[[docker.composes]]
name = "core"
path = "docker/compose.yml"
services = ["api", "db"]
users = { db = "postgres", api = "1000:1000" }   # optional, per service
```

- Keys must name entries of this file's `services` list (unknown key =
  loud config refusal at parse, naming the service and the declared set).
- Values are passed to docker verbatim and accepted in every form
  `docker exec -u` accepts: a name (`root`, `postgres`), a UID (`1000`),
  `UID:GID` (`1000:1000`), `name:group`. otto validates only that the
  value is non-empty and contains no whitespace; docker remains the
  authority on validity (products own their interface).
- Registration threads the value onto the container host (§4); it applies
  to both the legacy per-repo path and the use-case path (both funnel
  through `register_stack_hosts`).

## 4. Container implementation

New init field `DockerContainerHost.user: str | None = None` (host
init-field rule: it is a spec field, this section). Populated from the
`users` table at registration; `None` when undeclared.

Effective user per operation: the per-call `user` argument if not None,
else the host's declared `user` field, else nothing (docker default).

- **`exec` / `run`:** the exec wrapper becomes
  `docker exec [-u <user>] <ctr> sh -c ...`; the persistent `run` channel
  opens its `docker exec -it` with the same `-u`. The channel BINDS its
  user at open: a later `run(user=...)` that differs from the live
  channel's bound user refuses loudly (naming both users) rather than
  silently answering from the wrong identity — closing and reopening the
  session is the caller's explicit move. `exec`, being channel-less, has
  no such constraint.
- **`login`:** implements the (renamed) protocol param via
  `docker exec -it -u <user>` — replacing today's hard refusal.
- **`put`:** two-step stays (`stage to parent` → `docker cp`). When an
  effective user exists, a third step runs
  `docker exec -u root <ctr> chown <user> <dest...>` for the landed paths.
  If that chown fails (image has no root, no chown binary), the transfer
  FAILS loudly naming the file, the user, and the reason — files are never
  silently left with wrong ownership. No effective user → today's behavior.
- **`get`:** reads are ownership-indifferent; `user` is accepted for
  interface uniformity and used for the source-side `docker cp` only if a
  future need appears — v1 documents that `get` ignores it beyond
  validation. (Kept in the signature so the CLI/API surface is uniform.)
- Refusal phrases are distinct and exact-phrase-tested per house rule.

## 5. CLI

No bespoke CLI code: the verbs are synthesized from `@cli_exposed`
signatures. Adding `user: Annotated[str | None, Opt(name="--user")]` (the
`Opt` default naming already yields `--user`) to the five methods surfaces
`--user` on `otto host <id> login/run/put/get` for every host type, and on
the `run` builtin. Refusals surface as exit-1 errors with the library's
phrase. Completion: no completer for the value (user strings are free-form;
the reservation `--as-user` completer is NOT reused — different concept).

## 6. Breaking changes and migration

- `login(as_user=...)` → `login(user=...)` (library) and
  `otto host <id> login --as-user` → `--user` (CLI). No deprecation shim
  (0.10.0 window, consistent with the use-case cutover's no-shims stance).
- Rename sweep covers the PARAMETER family only: `login(as_user=)` /
  `_login(as_user=)` signatures, call sites, docstrings, and synthesized
  flags (`host.py`, `unix_host.py`, `docker_host.py`, `embedded_host.py`)
  and their tests/docs. It must NOT touch the ELEVATION context-manager
  family — `PosixPrivilege.as_user()` / `HostSession.as_user()`
  (`privilege.py`, `session.py`, `login_proxy.py` prose) keep their name:
  "become this user, with undo" is a different concept from "run this
  call as this user", and `async with host.user(...)` would collide with
  the new field/param. It must equally NOT touch the reservation-identity
  `as_user` family
  (`cli/main.py` root option, `cli/reservation.py`,
  `reservations/identity.py`, `remote_completion.py`). The two families
  share a spelling today; the sweep's grep discipline is the whole game.
- Docs: login page, host verb pages, the docker use-cases guide (a
  "container users" section: declared defaults + per-call override +
  the runtime-user-is-compose's pattern), lab-config/settings tables for
  the new `users` key (docs-coverage guards will enforce the rows).

## 7. Testing

- Schema: users-table round-trip; unknown-service key refusal; form
  pass-through (name, UID, UID:GID).
- Container: wrapper carries `-u` exactly when an effective user exists
  (declared, per-call, per-call-beats-declared, neither); login channel
  gains `-u`; put chown-success and chown-failure (loud, file-named)
  paths; get accepts-and-ignores documented behavior pinned.
- Refusals: unix run/exec/put/get with user=...; embedded; each exact
  phrase, each branch executed (100% on new arms, house standard).
- CLI: synthesized `--user` present on the five verbs; login rename
  (old `--as-user` gone from verb help; root reservation flag intact);
  e2e: `otto host <ctr-id> run --user root id` prints `uid=0` on the bed.
- Mutation honesty: the effective-user precedence and the chown-failure
  refusal each proven load-bearing (flip → named test red).

## 8. Follow-ups (ledgered, not this design)

- Unix `run/exec(user=...)` via a sudo/su privilege story.
- `get`-side ownership semantics if a need materializes.
- Reservation `--as-user` naming (left alone here; any rename is its own
  discussion precisely because these two concepts must not blur).
