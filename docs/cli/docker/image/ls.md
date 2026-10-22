# otto docker image ls

Print `docker image ls` from every docker-capable host in the lab, exactly as
docker printed it. `otto docker images` is the same command, the way
`docker images` is docker's own alias for `docker image ls`.

```text
otto docker image ls [-a|--all] [--parent HOST]
otto docker images   [-a|--all] [--parent HOST]
```

| Option | Description |
| ------ | ----------- |
| `-a`, `--all` | Show every image, intermediate ones too (docker's `-a`) |
| `--parent HOST` | Ask one docker-capable host (default: every one; see {ref}`Which host <docker-which-host>`) |

otto adds one line per host, `== <host-id> ==`, and changes nothing inside
docker's output: every image id is as short as docker prints it, every
column where docker put it. A host whose daemon could not answer prints
docker's error under its header, and a host otto could not reach prints
otto's error there; the command goes on to the next host and exits 1 at the
end. Library: {func}`~otto.docker.observe.list_images`.

An illustrative listing — your daemon's columns and rows are what you will see:

```text
$ otto docker image ls
== test3 ==
REPOSITORY     TAG       IMAGE ID       CREATED          SIZE
project1-api   latest    cbd8571d4b6e   2 minutes ago    148MB

== test1 ==
REPOSITORY   TAG       IMAGE ID       CREATED          SIZE
```

`image ls` lists what each daemon holds, not what a build would produce; to
build images, see {doc}`../build`. To remove some, see {doc}`rm` and
{doc}`prune`. Any other docker command runs through the passthrough:
`otto host test3 exec "docker image ls --digests"`.

## Dry run

Under `--dry-run` (see {doc}`../../dry-run`) the verb asks no daemon and prints
the exact command it would run on each host, one `on <host>: docker image ls`
line per host.
