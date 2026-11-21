// A layout that emits one name twice: `which` is 'lab' or 'element'.
local o = import 'otto.libsonnet';

function(otto, which)
  o.layout({
    instances: o.groupBy(otto.inventories.physical, ['site']),
    lab(i): if which == 'lab' then 'bench' else 'bench.' + i.id.site,
    elementName(i, role, record, n): if which == 'element' then 'dut' else record.key,
    roles: {
      dut: { match: o.inGroup('dut'), count: 1, host: { label: 'dut', fn(r): {} } },
    },
  })
