// The records (as `<inventory>/<key>`) each predicate selects.
local o = import 'otto.libsonnet';

function(otto)
  local recs = otto.inventories.physical + otto.inventories.virtual;
  local keys(pred) = [r.inventory + '/' + r.key for r in recs if pred(r)];
  {
    inGroup: keys(o.inGroup('dut')),
    label: keys(o.label('pod', 'p1')),
    fact: keys(o.fact('site', 'east')),
    inInventory: keys(o.inInventory('virtual')),
    allOf: keys(o.allOf([o.inGroup('dut'), o.fact('site', 'east')])),
    anyOf: keys(o.anyOf([o.inGroup('tgen'), o.label('pod', 'p1')])),
    version: o.version,
  }
