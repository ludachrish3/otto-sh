// Each of `names`, escaped for an element's `labs` membership.
local o = import 'otto.libsonnet';

function(names) [o.escapeName(n) for n in names]
