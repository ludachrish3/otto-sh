// One router lab per site and rack: NetBox's device is the DUT, and the
// virtual inventory's test1 the traffic generator. NetBox records carry no
// `groups` yet (#1192), so roles select by inventory membership and facts.
local o = import 'otto.libsonnet';
local hosts = import 'hosts.libsonnet';

function(otto)
  local records = otto.inventories.physical + otto.inventories.virtual;
  o.layout({
    instances: o.groupBy(records, ['site', 'rack']),
    lab(i): 'router-lab.' + i.id.site + '-' + i.id.rack,
    elementName(i, role, record, n): record.key,
    roles: {
      dut: {
        match: o.inInventory('physical'),
        count: 1,
        host: { label: 'dut', fn: hosts.ubuntu },
      },
      tgen: {
        match: o.allOf([o.inInventory('virtual'), o.fact('key', 'test1')]),
        count: 1,
        host: { label: 'tgen', fn: hosts.ubuntu },
      },
    },
  })
