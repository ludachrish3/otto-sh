// Author-chosen lab and element names, and a lab entry for each runnable lab.
local o = import 'otto.libsonnet';

function(otto)
  o.layout({
    instances: o.groupBy(otto.inventories.physical, ['site']),
    lab(i): 'bench(' + i.id.site + ')',
    elementName(i, role, record, n): record.key + '@' + i.id.site,
    roles: {
      dut: { match: o.inGroup('dut'), count: 1, host: { label: 'dut', fn(r): {} } },
    },
    labsEntry(i): { metadata: { site: i.id.site } },
  })
