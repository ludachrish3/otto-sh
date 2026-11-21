// A representative large fleet: 500 labs, each of 4 role elements (2000
// elements, one host each), with links between the roles. Every element is
// built through nested helpers, the way a real layout composes defaults,
// overrides and per-role differences, so the evaluator's stack depth and the
// output's size are those of a real template. otto.templating.render's
// RenderLimits docstring records what it measures.
//
// Strings are joined with `+`, never `%`: std.format is written in Jsonnet
// and costs about 0.3 ms a call under gojsonnet, so a `%` for every field of
// this fleet alone takes seconds, which would measure std.format rather than
// the fleet.
local count = 500;
local roles = ['router', 'switch', 'server', 'probe'];

local roleIndex(role) = std.find(role, roles)[0];
local octets(n) = [10 + std.floor(n / 250), n % 250 + 1];
local dotted(parts) = std.join('.', [std.toString(p) for p in parts]);
local padded(n) = std.substr('00' + n, std.length('00' + n) - 3, 3);

local defaults = {
  os_type: 'unix',
  valid_terms: ['ssh', 'telnet'],
  valid_transfers: ['scp', 'sftp', 'ftp', 'nc'],
  is_virtual: true,
  site: 'lab-a',
  docker_capable: false,
};

local roleExtras = {
  router: { valid_terms: ['ssh', 'telnet', 'console'], interfaces_count:: 4 },
  switch: { is_virtual: false, interfaces_count:: 8 },
  server: { docker_capable: true, interfaces_count:: 2 },
  probe: { valid_transfers: ['nc'], interfaces_count:: 1 },
};

local interface(lab, role, k) = {
  ip: dotted([192, 168, k, (lab * 4 + roleIndex(role)) % 250 + 1]),
  subnet: dotted([192, 168, k, 0]) + '/24',
};

local host(lab, role) =
  local extra = roleExtras[role];
  local o = octets(lab * 4 + roleIndex(role));
  defaults + extra + {
    ip: dotted([10, o[0], o[1], roleIndex(role) + 1]),
    rack: 1 + std.floor(lab / 20),
    shelf: 1 + lab % 20,
    interfaces: {
      ['eth' + k]: interface(lab, role, k)
      for k in std.range(1, extra.interfaces_count)
    },
  };

local labName(lab) = 'fleet-' + padded(lab);
local elementName(lab, role) = labName(lab) + '-' + role;

local element(lab, role) = {
  name: elementName(lab, role),
  labs: [labName(lab)],
  metadata: { role: role, index: lab },
  hosts: [host(lab, role)],
};

local links(lab) = [
  {
    endpoints: [
      { element: elementName(lab, 'router'), interface: 'eth1' },
      { element: elementName(lab, peer), interface: 'eth1' },
    ],
  }
  for peer in roles[1:]
];

local labs = std.range(0, count - 1);

{
  labs: {
    [labName(lab)]: {
      resources: [elementName(lab, role) for role in roles],
      metadata: { description: 'fleet lab ' + (lab + 1) + ' of ' + count },
    }
    for lab in labs
  },
  elements: std.flattenArrays([[element(lab, role) for role in roles] for lab in labs]),
  links: std.flattenArrays([links(lab) for lab in labs]),
}
