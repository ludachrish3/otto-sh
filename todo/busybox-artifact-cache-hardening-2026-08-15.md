# BusyBox artifact cache: four residual hazards around fetch and verify

Found 2026-08-15 by the adversarial review of the temp-file race fix (the commit
that adds this file). All four are **pre-existing** — none is introduced or
worsened by that change — and none blocked it. Filed together because they all
live in `tests/_fixtures/busybox.py` and two of them interact.

## 1. A hash mismatch poisons a persistent cache permanently (LOW, the only one worth real effort)

`busybox_binary` (`:326-333`) fetches only when the target is absent:

```python
if not target.exists():
    _fetch(release, target)
_verify(release, target)
```

`_verify` (`:418-430`) raises on a pin mismatch and **does not unlink the bad
artifact**. So a cache that once received wrong bytes keeps them: every later
run finds `target.exists()`, skips the fetch, re-hashes the same bad file and
re-raises. The only exit is a manual `rm`, and the error text does not say so —
it tells the reader to investigate upstream and update the pin, which is the
right advice for a genuine upstream rebuild and the wrong advice for a corrupt
download.

This does not bite CI: `.github/workflows/ci.yml:140` deliberately does NOT
cache these artifacts (a cache keyed on the pins would skip exactly the runs
that detect an in-place upstream rebuild), so a CI cache dies with the runner.
It bites a dev VM and any `OTTO_BUSYBOX_CACHE` pointed at durable storage.

Interacts with #2 below: whether a mismatch means "upstream changed" or "this
download was corrupt" is exactly what the operator cannot tell today, and the
right shape probably distinguishes them rather than blanket-unlinking — a
blanket unlink would re-download an upstream rebuild on every run and turn a
loud, investigable stop into a silent retry loop. Decide that before coding.

## 2. Two publishers can cross-attribute the red (LOW, mostly a diagnosis problem)

After the race fix, two workers may both fetch and both publish; `replace` is
atomic and the bytes are normally identical. If upstream serves divergent bytes
to the two requests, worker A can `_verify` good bytes and then execute what
worker B republished underneath it — while B's own `_verify` reds. Nothing goes
silently green (the pin catches it), but the failure is reported against the
wrong worker's test, and per #1 the poisoned file stays in the cache.

Worth fixing only if #1 is fixed; on its own the mis-attribution costs one
confusing triage.

## 3. Retry safety depends on `write_bytes` truncating the whole body (NOTE — a tripwire for a future editor)

`_fetch`'s cleanup is now a `finally` around the whole retry `for`, so a partial
`.part` from a transient failure survives into the next attempt. That is safe
**only** because attempt 2 calls `tmp.write_bytes(resp.read())` (`:372`), which
opens `'wb'` and truncates. A switch to streaming or append writes — the obvious
change the day someone wants a progress bar or wants to stop holding a whole
artifact in memory — makes the retained partial real, and the resulting artifact
would be a clean-looking concatenation that only the pin catches.

Either leave a comment at the write site saying the `finally` depends on it, or
unlink at the top of each attempt. The comment is probably enough; the point is
that nothing currently states the dependency where the editor will be looking.

## 4. Two cosmetic edges (NOTE, probably WONTFIX — recorded so they are not re-derived)

- **NFS ESTALE.** On the shared-cache-across-machines shape the fix's own
  comment invokes, a cross-machine republish during `_sha256`'s read can ESTALE
  the reader. Crash, not corruption, and strictly better than the pre-fix shared
  `.part` on that same shape.
- **`finally` masking.** If `tmp.unlink(missing_ok=True)` (`:417`) itself raises
  for a non-missing reason — EACCES on the directory — it would mask an
  in-flight `BusyBoxUnavailableError` and its priming instructions. The old
  code had the same property.

Both are vanishingly rare. Recorded to save the next reviewer the derivation,
not because either is worth code today.

## Guarding whatever gets fixed

The existing guards in `tests/unit/host/test_busybox_artifacts.py` show the
shape: drive `_fetch`/`busybox_binary` directly against a stubbed URL, force any
interleaving by re-entrancy (patch `Path.write_bytes` and run the other fetcher
from inside the spy) rather than by racing threads, and score a mutation red
only on `rc == 1` plus the named failing test. Do NOT put a guard in
`tests/busybox/` — that tier touches the network and is excluded from every
default lane, so it would not run where it matters.
