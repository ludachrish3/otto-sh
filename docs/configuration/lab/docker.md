# Docker-capable hosts

Mark hosts that can host containers.  `docker_capable` is a *host* field, so
it goes on the host entry inside its element, and `docker_priority` sits beside
it:

```text
{
  "name": "test3",
  "labs": ["unix"],
  "hosts": [
    { "ip": "...", "creds": [...], "docker_capable": true, "docker_priority": 10 }
  ]
}
```

`docker_priority` is lab **intent** — which machine this lab prefers to run
containers on — not a fact about the machine: it is a lab-file field, and the
inventory never supplies it.  It defaults to `0`, and the key is allowed only on a
docker-capable host, even as `0`. A host is docker-capable when `docker_capable`
is set, or when the host class's own default says so.  Which host the docker verbs default to, and what a tie
does, is {ref}`Which host <docker-which-host>`.
