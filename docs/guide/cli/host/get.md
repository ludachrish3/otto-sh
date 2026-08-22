# otto host get

Retrieve files from a remote host with `get`:

```bash
otto --lab my_lab host router1 get /var/log/syslog ./logs/
```

Multiple remote paths are supported:

```bash
otto --lab my_lab host router1 get /var/log/syslog /var/log/auth.log ./logs/
```
Arguments mirror {doc}`put` — see its argument table.
