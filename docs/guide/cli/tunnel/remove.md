# otto tunnel remove

```bash
otto tunnel remove <id>
otto tunnel remove --all
otto tunnel remove --all -y
```

`remove <id>` discovers every tagged process for that id across every host
that might be running one, kills them, then **re-scans** the hosts it just
killed on to confirm they're actually gone. `remove --all` reaps **every**
otto tunnel it finds — not just ones this invocation or this user created;
tunnel ownership isn't tracked — see
[Tunnel identity](identity.md#tunnel-identity). Because `--all` is
destructive and owner-agnostic, it asks for confirmation first; pass `-y` / `--yes` to skip the prompt (e.g. from a
script or CI cleanup step).

If any killed process is still alive on the post-kill scan, `remove` names
it as a survivor and exits non-zero — never a silent trust of the kill
command's own exit code.

