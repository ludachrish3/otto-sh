# otto docker images

Print `docker images` from every docker-capable host in the lab, exactly as
docker printed it.

```text
otto docker images [--parent HOST]
```

| Option | Description |
| ------ | ----------- |
| `--parent HOST` | Ask one docker-capable host (default: every one; see {ref}`Which host <docker-which-host>`) |

otto adds one line per host, `== <host-id> ==`, and changes nothing inside
docker's output: every image id is as short as docker prints it, every
column where docker put it. A host whose daemon could not answer prints
docker's error under its header; the command goes on to the next host and
exits 1 at the end. Library: {func}`~otto.docker.observe.list_images`.

An illustrative listing — your daemon's columns and rows are what you will see:

```text
$ otto docker images
== test3 ==
REPOSITORY   TAG       IMAGE ID       CREATED          SIZE
repo1-api    latest    cbd8571d4b6e   2 minutes ago    148MB

== test1 ==
REPOSITORY   TAG       IMAGE ID       CREATED          SIZE
```

`images` lists what each daemon holds, not what a build would produce; to
build images, see {doc}`build`. Any other docker command runs through the
passthrough: `otto host test3 exec "docker images --digests"`.
