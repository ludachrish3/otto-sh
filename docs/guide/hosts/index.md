# Hosts

One host object stands behind everything otto does to a machine.  The same
object serves `otto host <verb>` on the command line
({doc}`../cli/host/index`) and the `Host` API from Python
({doc}`../../api/host/index`) — the CLI verb is the Python method, exposed.

Four verbs carry almost all of the traffic:

- **`run`** — a command on the host's *persistent* shell, where `cd`,
  environment variables and shell state survive from one call to the next
  ({doc}`../cli/host/run`).
- **`exec`** — one command, statelessly, in a fresh channel; safe to run
  several at once.
- **`put`** / **`get`** — files up and down ({doc}`../cli/host/put`,
  {doc}`../cli/host/get`).

That split is not a stylistic one, and it decides where identity lives.  A
persistent session *already has* a user, so changing it is a scoped operation
on the session — `as_user`, described in
{doc}`../cli/host/capabilities/privilege` and
{doc}`../../library/sessions` — and `run` refuses a per-call `user=` on the
families that work this way.  A stateless verb has no such history: `exec`,
`put` and `get` can each take a user directly, because each call opens its own
channel and can open it as somebody else.

What "can" means there depends on the family.  Not every host has a second
user to become, and the ones that do reach it by different routes.  Each
family declares its own answers, and {doc}`families` renders them.

```{toctree}
:hidden:

families
```
