// One bench lab per number. Add a bench by adding its number to `benches`.
local benches = [1, 2, 3];

// The address and port scheme. Change it here, and every bench follows.
local subnet(n) = '10.20.' + n;  // bench n's network, 10.20.n.0/24
local sshPortBase(n) = 2200 + 100 * n;  // host k of bench n listens on base + k

// The elements of bench n: a DUT (host 1) and a traffic generator (host 2).
local bench(n) = [
  {
    name: 'bench' + n + '-dut',
    labs: ['bench' + n],
    hosts: [{ ip: subnet(n) + '.1', ssh_options: { port: sshPortBase(n) + 1 } }],
  },
  {
    name: 'bench' + n + '-tgen',
    labs: ['bench' + n],
    hosts: [{ ip: subnet(n) + '.2', ssh_options: { port: sshPortBase(n) + 2 } }],
  },
];

{
  labs: { ['bench' + n]: {} for n in benches },
  elements: std.flattenArrays([bench(n) for n in benches]),
}
