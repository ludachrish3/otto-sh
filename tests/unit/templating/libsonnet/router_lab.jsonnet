// One router lab per site and rack: a DUT and a traffic generator, grouped
// across the `physical` and `virtual` inventories. `flip` reverses the
// records' enumeration order; `duts` is how many DUTs a lab needs.
local o = import 'otto.libsonnet';

function(otto, flip=false, duts=1)
  local recs = otto.inventories.physical + otto.inventories.virtual;
  local name(i) = 'router-lab.' + i.id.site + '-' + i.id.rack;
  o.layout({
    instances: o.groupBy(if flip then std.reverse(recs) else recs, ['site', 'rack']),
    lab(i): name(i),
    elementName(i, role, record, n): i.id.site + '-' + i.id.rack + '-' + role + n,
    roles: {
      dut: {
        match: o.inGroup('dut'),
        count: duts,
        host: { label: 'hosts.routerDut', fn(r): { os_type: 'unix', valid_terms: ['ssh'] } },
      },
      tgen: {
        match: o.inGroup('tgen'),
        count: 1,
        host: { label: 'hosts.linuxTgen', fn(r): { os_type: 'unix' } },
      },
    },
  })
