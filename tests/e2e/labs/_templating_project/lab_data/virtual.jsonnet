// The virtual inventory: VM testN answers at 10.10.200.(10+N), racked with
// NetBox's edge-r1 at site lab-a, rack 1. The rack is the string NetBox's
// rack name is, so both inventories' records group into one instance.
local creds = [
  { login: 'vagrant', password: 'vagrant' },
  { login: 'test', password: 'Password1' },
];

{
  ['test' + n]: {
    ip: '10.10.200.' + (10 + n),
    site: 'lab-a',
    rack: '1',
    is_virtual: true,
    creds: creds,
  }
  for n in [1, 2, 3]
}
