// inventory.json's three records, templated: the `.jsonnet` twin of the
// worked example's inventory. VM testN is 10.10.200.(10+N), with its eth2
// data port at 192.168.1.(10+N); what one VM has and the others lack is its
// `extra` object, merged over the shared record with `+`. Keys are the
// device names NetBox will carry; never rename a key.
local vm(n, extra={}) = {
  ip: '10.10.200.%d' % (10 + n),
  interfaces: {
    eth2: { ip: '192.168.1.%d' % (10 + n), subnet: '192.168.1.0/24' },
  },
  is_virtual: true,
  site: 'lab-a',
  rack: 1,
  shelf: 3,
} + extra;

{
  test1: vm(1, {
    interfaces+: {
      'bbeth-1350': { ip: '198.51.100.18', subnet: '198.51.100.16/30' },
    },
    extra: { asset_tag: 'VM-0011' },
  }),
  test2: vm(2),
  test3: vm(3, { shelf: 4 }),
}
