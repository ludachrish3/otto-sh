// A stand-in for NetBox: one router racked at each site, in group `dut`.
local sites = { east: '10.1.0', west: '10.2.0' };

{
  [site + '-r1']: {
    ip: sites[site] + '.1',
    site: site,
    groups: ['dut'],
  }
  for site in std.objectFields(sites)
}
