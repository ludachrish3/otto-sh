// One router lab per site: a DUT from `physical`, a traffic generator from
// `virtual`.
local o = import 'otto.libsonnet';
local hosts = import 'hosts.libsonnet';

function(otto)
  local records = otto.inventories.physical + otto.inventories.virtual;
  o.layout({
    instances: o.groupBy(records, ['site']),
    lab(i): 'router-lab.' + i.id.site,
    elementName(i, role, record, n): i.id.site + '-' + role + n,
    roles: {
      dut: { match: o.inGroup('dut'), count: 1, host: { label: 'dut', fn: hosts.routerDut } },
      tgen: { match: o.inGroup('tgen'), count: 1, host: { label: 'tgen', fn: hosts.linuxTgen } },
    },
  })
