# otto docker image rm

Run `docker image rm` on every docker-capable host in the lab, and print
what docker printed on each.

```text
otto docker image rm IMAGE... [-f|--force] [--no-prune] [--parent HOST]
```

| Option | Description |
| ------ | ----------- |
| `IMAGE` (argument) | Images to remove: a reference (`project1-api:latest`) or an id, as docker takes them. One or more |
| `-f`, `--force` | Remove an image a container still uses, or untag a reference several images share (docker's `-f`) |
| `--no-prune` | Keep the untagged parent images (docker's `--no-prune`) |
| `--parent HOST` | Run on one docker-capable host (default: every one; see {ref}`Which host <docker-which-host>`) |

Each `IMAGE` goes to docker as typed, quoted for the host's shell, after
docker's `--`, so a name that starts with `-` is never read as a flag. To name
one on otto's line, end otto's flags with `--` too:
`otto docker image rm -- -odd-name`. docker
never asks before `image rm`, so `-f` is docker's own flag, passed through
unchanged. otto adds one `== <host-id> ==` line per host and changes nothing
in docker's `Untagged:` and `Deleted:` lines. A host that does not hold an image
prints docker's own error under its header (`No such image`); a host otto
could not reach prints otto's error there. The command goes on to the next
host and exits 1 at the end. Library:
{func}`~otto.docker.image_verbs.remove_images`.

```text
$ otto docker image rm project1-api --parent test3
== test3 ==
Untagged: project1-api:latest
Deleted: sha256:cbd8571d4b6e…
```

TAB on `IMAGE` offers the images the active projects declare, then the
references the daemons listed last: with `--parent`, that host's; without it,
every docker-capable host's in the selected labs, each reference once, since
that is where the verb runs (see {ref}`Completion <docker-completion>`).
After the verb runs, otto asks each host it reached for its images again,
so TAB stops offering a removed image. A host whose daemon refused is asked
too.

## Dry run

Under `--dry-run` (see {doc}`../../dry-run`) the verb removes nothing and
prints the exact command it would run on each host, one host per line, then
"No image was removed.":

```text
$ otto --dry-run docker image rm project1-api
'remove_images(test1, test3)' was not run on host 'test1, test3': this is a dry run, …
drop --dry-run. Would run:
on test1: docker image rm -- project1-api
on test3: docker image rm -- project1-api
No image was removed.
```
