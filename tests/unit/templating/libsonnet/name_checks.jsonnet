// Sites as labs: `name` picks the name function that is not a string for an
// instance whose site is missing ('lab'), or for every element ('element').
local o = import 'otto.libsonnet';

function(otto, name)
  o.layout({
    instances: o.groupBy(otto.inventories.physical, ['site']),
    lab(i): if name == 'lab' then i.id.site else 'bench.' + i.id.site,
    elementName(i, role, record, n): if name == 'element' then null else record.key,
    roles: {
      dut: { match: o.inGroup('dut'), count: 1, host: { label: 'dut', fn(r): {} } },
    },
  })
