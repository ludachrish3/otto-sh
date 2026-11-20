# Identity and elevation on a container (#935) — design

Status: **draft, not ready for review.** Split out of
`2026-11-19-container-cells-design.md` by Chris's decision of 2026-11-19. The
proposal below is that design's section 6 as it stood at revision 3, kept as
the starting point. It was reviewed twice by Astra and Fable and once by
Astra 6.2 (`.superpowers/sdd/pr4-design-review-*.md`); the Astra 6.2 findings
listed under "Open" are not yet folded in.

## Decided (Chris, 2026-11-19)

- Identity and elevation get their own design, covering the host verbs and
  named sessions together.
- `run(sudo=True)` on a container follows **one fixed rule**, not a
  per-service policy. Which rule is this design's first question. If one rule
  proves too inflexible, users can ask for more later.

## Accepted from review, to fold in

- `sudo=True` with `user=X`: `X` is resolved in the container and an absent
  account is refused on both verbs before anything is elevated. Otto never
  invents identity; a misspelt `X` must not become a root command.
- `HostSession.as_user` and `switch_user` on a container's named session
  change with the host verbs, through one shared transition. A named handle
  stays independent of the host's scope.
- The authority change is voted on separately from the state-lifetime change:
  what `password=` means, login-shell and restricted-shell accounts, docker's
  numeric and `user:group` forms, and what `UserSupport.switch` then says on
  the families page.
- A fresh open on a `run(user=Y)` that shares a warm channel today is a
  sunny-day cost and needs its own ruling.

## Open

- The fixed rule for `run(sudo=True)`. The four candidates and their costs are
  in the table of the cells design's revision 3 questions, repeated below.
  Astra 6.2's objections to the root-channel candidate: a directory read, an
  open and a close on every successful call; the root shell interprets the
  command where the session's shell does today; a directory replaced between
  the read and the open is entered silently.
- Who owns a scope when its shell or the event loop goes away:
  `rebuild_connections()` is synchronous and drops managers unclosed.
- What a sibling task's `exec` sees while another task holds an elevated
  scope, and what a typed `exit` does after an unscoped `switch_user`.
- Witnesses required: two runs inside one block, two separate `user=` calls,
  nested restoration, a body that raises or is cancelled.

Candidates for `run(sudo=True)`:

1. A root channel for the one call, opened in the directory the session is in.
2. The same as `run(user="root")`: a fresh root shell in `WORKDIR`.
3. One root channel kept for the host, each command typed as
   `(cd <dir> && cmd)`. A subshell keeps its `cd` and assignments out of the
   kept shell; the costs are the directory, serialising the kept terminal, and
   recovery after a failed command.
4. The image's own `sudo`, as today: fails on both bed images, and `exec`
   would have to use it too for the two verbs to agree.

## The proposal as it stood (cells design, revision 3, section 6)


One mechanism replaces the six: **a scoped channel.** The default channel is
unchanged: it belongs to the declared user (else the image's), and a plain
`run` keeps its state there, as today.

| Call | On a container |
| --- | --- |
| `as_user(Y)` | Opens a channel `docker exec -it -u Y <cid> sh`, makes it the default session for the block, closes it at exit and puts the outer session back. Nested scopes stack. Successive `run`s in the block share it; the outer session is untouched. |
| `run(user=Y)` | `as_user(Y)` around the one call: the dispatcher a unix host already uses (`UserSupport.switch`, `host.py:2356`). The container declares `run_user=switch`, `session_identity=as_user_scoped`. |
| `switch_user(Y)` (unscoped) | The same channel, kept until the switch is undone or the host closes. |
| `send`, `expect` | The current default session, so inside a scope they follow the switch, as on a unix host. |
| `exec(user=Y)`, `exec` inside a scope | `docker exec -u Y`, as today. |
| `exec(sudo=True)` | `docker exec -u root`, as today. |
| `sudo=True` with `user=X`, on `run` and `exec` | Runs as root. `X`'s spelling is checked; docker is never asked about `X`, so a misspelt `X` is not noticed. |
| `run(sudo=True)` | A root channel for the one call, opened in the directory the session is in (open, above). |

Only the dispatcher is shared with a unix host. Underneath, a unix switch
types into one session and undoes it by typing; a container switch replaces
the session. The plan designs that seam (`_switch_default_session` and
`_undo_switch` are the two methods to vary), to these requirements:

- The transition owns the outer session, the new one, and the restoration,
  including after a partly failed open.
- The ambient user (what `exec` inside the scope runs as) is the scope's, and
  it survives a channel that dies mid-block: a dead scoped channel is reopened
  as the scope's user or the block fails, never silently as the declared user.
- `close()` and `rebuild_connections()` close every channel of the stack.
- Scopes are host-wide and one at a time, as a switched unix session is.
  Concurrent `run` on one host is documented as unsupported and stays so.
  Concurrent `exec` stays independent.

The promises this adds get an arm on every cell that has a second user, lab
hosts included: state is shared inside one `as_user` block, a `run(user=Y)`
starts fresh each time, and the outer session is as it was afterwards. A unix
host passes it today.

Why this shape, against the ones considered:

- **A persistent channel per user** (revision 1): `Y`'s state would outlive
  the switch, which a unix host never does. Rejected.
- **Open the one channel as root and `su` per command:** a `su -c` subshell
  loses the state `run` promises, and it makes every container session
  root-capable whatever the declared user. Rejected.
- **`docker exec -u root … su - Y`** to get a real login shell: refused for
  every `nologin` account, which includes `nobody` and `daemon` in both bed
  images (measured). Rejected.
- **Keep one bound channel and declare the refusals:** needs
  `HostCapabilities` to accept a `library` refusal of an option, which rule 4
  forbids as written, and leaves `sudo=` unusable through `run` on both bed
  images. Rejected.

`sudo=True` on a container is the docker daemon's authority, not the image's
`sudoers`: whoever may reach the daemon is already root in every container.
That is why `exec(sudo=True)` is `-u root` today (documented,
`docs/cli/host/exec.md:38`). A unix host becomes `X` first and asks whether
`X` may elevate; a container does not ask. Where both succeed the result is
the same, and the `sudo+user` arm asserts that result.

What differs from a unix host afterwards:

- A switched user gets a bare `sh` in the image's `WORKDIR`, not a login shell
  in their home (no profile is read). Already true of `exec(user=Y)`.
  Documented in `(container-users)=`. The starting directory and environment
  of a switched shell are not asserted on any host; that is a non-goal with an
  issue.
- `SessionIdentity.bound_at_open` stays in the enum (a project's host class
  may declare it); no shipped family uses it.

Breaks, each a `fix!`:

1. `run(cmd, user=Y)` naming the same `Y` on every call shares one channel
   today, so a `cd` in one call is seen by the next. Each call would start
   fresh, as on a unix host.
2. `as_user(X)` and `switch_user(X)` on a root-default container work today
   through `su - X` and give `X`'s login shell. They would give a bare shell
   as `X`. For an `X` with a `nologin` shell they fail today and would work.
   A `password=` given to `as_user` would be unused.
3. `run(sudo=True)` on an image that has `sudo` runs today through it, typed
   in the session. It would run in a root shell: no `sudoers` policy, no
   `SUDO_USER`, and the root shell interprets the whole command, so an
   unexported variable of the session is no longer expanded and a redirection
   is root's.
4. Three errors become results: `run(user=Y)` on a channel open as another
   user, `exec(sudo=True, user=X)`, `run(sudo=True)` on an image without
   `sudo`.
5. The families page changes for containers: `chown` becomes `switch`,
   "bound at open" becomes "as_user scoped".

Left as they are, with an issue each: `HostSession.as_user` on a *named*
session still types `su` (superseded: named sessions are included); a named session opened inside a scope
opens as the scope's user; `login` keeps its own fresh channel; `put`'s
ownership reads the per-call and declared user only, so an enclosing scope
does not change who owns a transferred file. The monitor and coverage
collection call plain `run` and `run_sh_elevated`, and get the table above.

Docs that state the old behaviour change with it:
`docs/cli/docker/use-cases.md:400-434`, `docs/cli/host/exec.md:38-40`,
`docs/architecture/subsystems/docker-hosts.md:293-336`, and the generated
`docs/cli/host/families.md`.

With this, no option is refused on a container. `HostCapabilities` keeps its
device-only check, rule 4 stands, and item 2 of #1172 ("a library-kind refusal
cannot be declared") is closed as not needed. If Chris prefers the declared
alternative, item 2 is needed and rule 4 has to be amended first.
