<!-- GENERATED FILE -- do not edit by hand.
     scripts/render_support_matrix.py renders this from the `capabilities`
     declaration on each host class, on every Sphinx build (docs/conf.py,
     builder-inited). Edits are overwritten. -->

# Host families

otto ships several kinds of host, and they do not all answer the four verbs the
same way. A serial console has no second user to become; a container has no
credentials for one; a unix box has both. This page is the standing answer for
each family, and every cell on it is **declared**: each row is the
`capabilities` object on that family's own host class, rendered here rather than
written here.

That is what separates this page from {doc}`../../architecture/support-matrix`.
The matrix publishes what a run **measured** against real hardware, cell by cell,
and it changes when the bed changes. This publishes what the code **promises**,
and it changes only when a host class does. A promise below that a device does
not keep is a bug in one of the two, and the conformance suite is what tells them
apart.

A class registered from your own repository must declare one too:
`register_host_class` refuses a class without it, so a custom family can never
reach this page's readers as a blank row.

## What the `user=` answers mean

| answer | what it means |
|---|---|
| `authenticate` | The verb rides a connection opened AS that user, so it runs with that user's real credentials and permissions rather than an elevation from the login user. |
| `chown` | The verb runs under the login identity's privilege and switches the result to the named user -- a `chown` over files that have landed, or `docker exec -u` for a command. The named user's own credentials are never needed. |
| `ignored` | The argument is accepted so the interface stays uniform, and has no effect; the family documents why it can have none. |
| `refused` | The verb raises `NotImplementedError` naming the alternative. The refusal is the first line of the body, so a dry run refuses too rather than declining as though the call could have been honoured. |

## Where a session's identity comes from

`run` drives a **persistent** session; `exec`, `put` and `get` are stateless. So
`run(user=)` is a question about an identity that already exists, and each family
answers it one of the ways below.

| identity | what it means |
|---|---|
| `as_user scoped` | `async with host.as_user(...)` switches the live session and restores the previous identity when the block exits. |
| `bound at open` | The run channel settles its user when it opens and cannot renegotiate one on a live shell, so a later call naming a different user refuses until the channel is dropped. |
| `none` | The connection's own identity is the only identity there is; nothing switches it. |

## The families

**Progress bar** is about the family's own transfer leg: whether the bytes move
through a backend that can report while they are moving. Per-backend strides
are published with the backends themselves, in {ref}`matrix-progress-promises`.

| family | how selected | `run(user=)` | `exec(user=)` | `put(user=)` | `get(user=)` | progress bar | session identity | transfer | note |
|---|---|---|---|---|---|---|---|---|---|
| `unix` | `os_type: unix` | `refused` | `authenticate` | `authenticate` | `authenticate` | yes | as_user scoped | `ftp`, `nc`, `scp`, `sftp` and `shell` | Direct-cred users only, and never over the `ftp` backend, which authenticates separately with its own credentials; `exec(user=)` additionally requires `term="ssh"`. `scp`, `sftp` and `nc` fan a batch out under `max_concurrent_transfers`; `shell` and `ftp` move one file at a time. |
| `embedded` | `os_type: embedded` | `refused` | `refused` | `refused` | `refused` | yes | none | `console` and `tftp` | A serial console has no user to switch to, and transfer ownership follows the connection's own identity. Refuses `put`/`get` with `--recursive`; moves one file at a time, so `--concurrent` is a no-op. |
| `zephyr` | `os_type: zephyr` | `refused` | `refused` | `refused` | `refused` | yes | none | `console` and `tftp` | A serial console has no user to switch to, and transfer ownership follows the connection's own identity. Refuses `put`/`get` with `--recursive`; moves one file at a time, so `--concurrent` is a no-op. |
| `container` | a `[docker]` service, started by `otto docker up` | `chown` | `chown` | `chown` | `ignored` | no | bound at open | the parent host's own backend for the staging leg, then `docker cp` across the container boundary, `--concurrent` governing the staging leg | `user=` defaults to the service's declared user, and falls back to the image's own `USER` when neither is set. |
| `local` | implicit -- the machine otto itself runs on | `refused` | `refused` | `refused` | `refused` | no | as_user scoped | `shutil.copy2` on the machine's own filesystem, one file at a time | otto already runs as the invoking user and local copies keep that user's ownership, so no verb takes `user=`; `as_user()` still switches the persistent session. |

The CLI pages for the verbs themselves — {doc}`../cli/host/run`,
{doc}`../cli/host/put`, {doc}`../cli/host/get` — say how to pass `--user`; this
page says what each family will do with it.
