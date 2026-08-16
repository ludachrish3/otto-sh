# `Repo.commit` stores a failed `git log`'s own output as the commit SHA

## The defect

`src/otto/config/repo.py`, `Repo.set_commit_hash`:

```python
result = await self.run_git_command("log -1 --format=%H")
self._git_hash = result.value
```

`result.value` is read with no status check. When the git command **fails** —
`sut_dir` is not a git repository, `.git` is unreadable, the binary is missing —
whatever that failed command produced is stored as the repo's HEAD SHA and
returned by the `commit` property, which is typed `str | None`.

Its sibling three lines up already gets this right:

```python
async def set_git_description(self) -> None:
    result = await self.run_git_command("describe")
    if result.status == Status.Success:
        self._git_description = f"({result.value.strip()})"
    else:
        self._git_description = ""
```

So the inconsistency is inside one pair of methods that share one helper.

## Pre-existing, and confirmed so

Present at `ac5a13b3`, before the dry-run contract branch touched this file:
`git show ac5a13b3:src/otto/config/repo.py` has the same unguarded
`self._git_hash = result.value`, and the same `Status.Success` check in
`set_git_description`. Nothing on that branch created or worsened it.

## Why it was left alone there

The dry-run work made `run_git_command` exempt from the `--dry-run` decline
(`LocalHost.dry_run_exempt`), so the *decline* case can no longer reach this
read at all — a dry-run branch here would be a guard that cannot fail, and the
reasoning for not adding one is recorded in `set_commit_hash`'s own docstring.
The **failure** case is a different question, out of that branch's scope, and
changing what `commit` returns on failure ripples past it.

## What a fix has to decide

`commit` returning `None` means "not fetched yet" to the property
(`if self._git_hash is not None: return self._git_hash`), so a failed read that
stores `None` re-runs git on every access. Storing `""` marks "asked, no
answer" and stops the re-ask, at the cost of a falsy-but-not-None value callers
have never had to handle. Pick one deliberately rather than by defaulting.

Consumers to check first — there are few:

- `otto.cli.invoke.repo_provenance` → the preamble's per-repo debug line.
- `Repo.commit_name`, which f-strings `commit` and `description` together.

Both are display paths, which is why the current behaviour has gone unnoticed:
a garbage SHA looks like a SHA in a log line nobody reads.

## Guard shape

A repo whose `sut_dir` is a real directory that is **not** a git checkout
(`tmp_path`), asserting `commit` is the chosen sentinel and specifically is not
git's own error text. Positive control in the same test: a `sut_dir` that *is*
a checkout still yields a 40-hex SHA — otherwise the test passes against a
`commit` that always returns the sentinel.
