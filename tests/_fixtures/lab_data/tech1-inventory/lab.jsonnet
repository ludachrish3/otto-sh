// lab.json, templated: the worked example's labs laid out over the default
// inventory's records with otto.libsonnet. Every machine fact comes from the
// inventory; every otto fact stays here. The source that reads this file
// declares `inventories = ["default"]`, and each element references its
// record by its qualified name ({"name": "default", "key": "testN"}), where
// lab.json writes the bare key.
local o = import 'otto.libsonnet';

// What otto knows of every VM, and what one VM adds to it.
local vm = {
  os_type: 'unix',
  docker_capable: true,
  valid_terms: ['ssh', 'telnet'],
  valid_transfers: ['scp', 'sftp', 'ftp', 'nc'],
};
local extra = {
  test1: {},
  test2: {
    valid_terms: ['telnet', 'ssh', 'console'],
    valid_transfers: ['nc', 'scp', 'sftp', 'ftp'],
    console_options: { server: 'test1', port: 4001 },
    resources: ['test2-console'],
  },
  test3: { docker_priority: 10 },
};

// test1 is also the busybox lab's hub, with its own resources and role.
local hub = {
  labs+: ['busybox'],
  resources: ['test1-chassis'],
  metadata: { role: 'hub' },
};

function(otto)
  local doc = o.layout({
    instances: o.groupBy(otto.inventories.default, ['site']),
    lab(i): 'unix',
    elementName(i, role, record, n): record.key,
    labsEntry(i): {
      resources: ['test1', 'test2', 'test3'],
      metadata: { description: 'unix regression bed' },
    },
    roles: {
      vm: {
        match: o.inInventory('default'),
        count: 3,
        host: { label: 'ubuntu-vm', fn(record): vm + extra[record.key] },
      },
    },
  });
  doc {
    labs+: {
      busybox: { resources: ['test1', 'bb1161', 'bb1211', 'bb1281', 'bb1310', 'bb1350'] },
    },
    elements: [e + (if e.name == 'test1' then hub else {}) for e in super.elements],
  }
