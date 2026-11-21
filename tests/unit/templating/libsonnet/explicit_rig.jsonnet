// Labs from the `lab` inventory's own instance map. Where an instance has
// roles they assign the members, so no role needs a `match`; with `match`
// set, each role also selects by group, for an instance with members only.
local o = import 'otto.libsonnet';

function(otto, match=false)
  local role(name) =
    { count: 1, host: { label: 'hosts.' + name, fn(r): { os_type: 'unix' } } }
    + (if match then { match: o.inGroup(name) } else {});
  o.layout({
    instances: o.instances(otto, 'lab'),
    lab(i): 'rig.' + i.id.instance,
    elementName(i, role, record, n): i.id.instance + '-' + role,
    roles: { dut: role('dut'), tgen: role('tgen') },
  })
