# Migrating from the hosts array

Before v2, a `lab.json` carried a top-level `hosts` array, and each host entry
declared its own `element`, `element_id`, and `labs` (and, then as now, its
own `resources`).  That shape
is gone: a top-level `hosts` key fails the load with a message naming the
file.  There is no migration tool — the transform is mechanical, and this is
it.

Before:

```json
{
    "hosts": [
        {
            "ip": "10.10.200.11",
            "element": "test1",
            "creds": [{"login": "vagrant", "password": "vagrant"}],
            "resources": ["test1"],
            "labs": ["unix"]
        },
        {
            "ip": "10.0.0.5",
            "element": "dut",
            "element_id": 3,
            "board": "cpu",
            "creds": [{"login": "admin", "password": "admin"}],
            "labs": ["embedded"]
        },
        {
            "ip": "10.0.0.6",
            "element": "dut",
            "element_id": 3,
            "board": "mgmt",
            "creds": [{"login": "root", "password": "root"}],
            "labs": ["embedded"]
        }
    ],
    "links": []
}
```

After:

```json
{
    "labs": {
        "unix": {"resources": ["test1"]},
        "embedded": {}
    },
    "elements": [
        {
            "name": "test1",
            "labs": ["unix"],
            "hosts": [
                {
                    "ip": "10.10.200.11",
                    "creds": [{"login": "vagrant", "password": "vagrant"}]
                }
            ]
        },
        {
            "name": "dut",
            "id": 3,
            "labs": ["embedded"],
            "hosts": [
                {
                    "ip": "10.0.0.5",
                    "board": "cpu",
                    "creds": [{"login": "admin", "password": "admin"}]
                },
                {
                    "ip": "10.0.0.6",
                    "board": "mgmt",
                    "creds": [{"login": "root", "password": "root"}]
                }
            ]
        }
    ],
    "links": []
}
```

Three steps, in order:

1. **Group the `hosts` array by `element`.**  Each group becomes one
   `elements` entry: `element` becomes its `name`, and an `element_id` the
   group carried becomes its `id` — data (see {ref}`host-identity`), so
   it no longer disambiguates.  Two groups that shared an `element` and
   differed only in `element_id` need distinct names now.  Delete both keys
   from the host entries — they are errors there now.
2. **Move each group's `labs` up to the element.**  The hosts of one element
   must have carried the same `labs`; where they did not, they were never one
   element — split them into elements with distinct names.
3. **Union each lab's hosts' `resources` into that lab's `labs` entry, or
   keep them on the host entries.**  Both are valid: the lab is one reservable
   unit among three, and `resources` is still a host field (an element may
   declare its own too).  Union them when the hosts were all standing in for
   one shared thing; keep them per host when each really is its own slot.
   Either way, every lab name any element joins must appear in some source's
   `labs` table — a lab exists only once it is declared, and a lab that
   reserves nothing is written `{}`.

Every operational host field stays exactly where it was.  Host ids do
change, though, wherever the v1 id embedded an `element_id`: it drops out of
the composition entirely (see {ref}`host-identity`), so `dut3_cpu`
becomes `dut-cpu` once `element_id: 3` moves to the element's `id`.  Update
every place that named the old id: declared-link endpoints, `[project]
host_patterns`, `[host_preferences]` selectors (which match by host id),
reservation identifiers that embed a host id, live tunnel markers, and any
scripted `get_host(...)` call.

Check your {doc}`os_profile <../os-profiles>` defaults for `element_id` as well.
A profile's `defaults` merge into every host entry it applies to, so one still
setting `element_id` now fails that entry's validation with `extra_forbidden`
on `element_id` — the key is gone from the host entry, not merely relocated.
Remove it from the profile.

One host field does change alongside them: `log` no longer accepts `true` or
`false`.  Write the mode name — `"normal"`, `"quiet"`, or `"never"`.
