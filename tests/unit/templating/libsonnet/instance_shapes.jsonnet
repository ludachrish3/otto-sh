// Both instance providers over the `lab` inventory, for comparing their shape.
local o = import 'otto.libsonnet';

function(otto) {
  explicit: o.instances(otto, 'lab'),
  derived: o.groupBy(otto.inventories.lab, ['site']),
}
