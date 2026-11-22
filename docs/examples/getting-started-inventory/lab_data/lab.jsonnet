// The unix lab of ../getting-started, with every machine fact read from
// inventory.json and every password from creds.json. One element per
// inventory record: this file holds only otto's facts. Declared inline,
// these three hosts come out identical.
local o = import 'otto.libsonnet';

// What every unix VM states: otto's vocabulary, never a machine fact.
local unixVm = {
  os_type: 'unix',
  docker_capable: true,
  valid_terms: ['ssh', 'telnet'],
  valid_transfers: ['scp', 'sftp', 'ftp', 'nc'],
};

// What a VM states beyond that, by inventory key: fields of its host entry
// (`host`) and of its element (`element`).
local differs = {
  // Two login-only creds that pin the login order, and the sudo-root route.
  test1: {
    host: {
      creds: [
        { login: 'vagrant' },
        { login: 'test' },
        { login: 'root', proxy: 'sudo-root', via: 'vagrant' },
      ],
    },
    element: { resources: ['test1-chassis'], metadata: { role: 'hub' } },
  },
  test2: {
    host: {
      valid_terms: ['telnet', 'ssh', 'console'],
      valid_transfers: ['nc', 'scp', 'sftp', 'ftp'],
      console_options: { server: 'test1', port: 4001 },
      resources: ['test2-console'],
    },
  },
};

function(otto) {
  labs: {
    unix: {
      resources: ['unix-bench'],
      metadata: { description: 'unix regression bed' },
    },
  },
  elements: [
    local own = std.get(differs, record.key, {});
    o.element(record.key, record, unixVm + std.get(own, 'host', {}), ['unix'])
    + std.get(own, 'element', {})
    for record in otto.inventories.default
  ],
}
