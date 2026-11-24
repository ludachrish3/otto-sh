// tech1: otto's own lab bed, the cell every bed lane leases (the Makefile's LAB_FILE).
//
// The four Ubuntu VMs come from one function, ubuntuVm(n): VM testN answers
// at 10.10.200.(10+N) and its eth2 data port at 192.168.1.(10+N), with the
// facts every VM shares. What one VM has and the others lack is its `extra`
// object, merged over the shared shape with `+`. The Zephyr instances, the
// BusyBox guests and the chaos guest are written out as they are: each is
// one of a kind.
//
// tests/_fixtures/goldens/tech1_lab.json is this file rendered, frozen
// (tests/unit/labs/test_testbed_templates.py holds the two equal).

local creds = [
  { login: 'vagrant', password: 'vagrant' },
  { login: 'test', password: 'Password1' },
];

// Element test<n>: one Ubuntu VM. Its host entry is the hidden `host` field,
// so an `extra` reaches it with `host+:`; `labs` and the element's own
// fields are set by `extra` directly.
local ubuntuVm(n, extra={}) = {
  name: 'test%d' % n,
  host:: {
    ip: '10.10.200.%d' % (10 + n),
    os_type: 'unix',
    valid_terms: ['ssh', 'telnet'],
    valid_transfers: ['scp', 'sftp', 'ftp', 'nc'],
    is_virtual: true,
    creds: creds,
    interfaces: {
      eth2: { ip: '192.168.1.%d' % (10 + n), subnet: '192.168.1.0/24' },
    },
  },
  hosts: [self.host],
} + extra;

// The unix regression VMs' place in the rack, and their docker parent role.
local unixBed(shelf) = {
  host+: { site: 'lab-a', rack: 1, shelf: shelf, docker_capable: true },
};

local test1 = ubuntuVm(1, unixBed(3) + {
  labs: ['unix', 'busybox'],
  resources: ['test1-chassis'],
  metadata: { role: 'hub' },
  host+: {
    interfaces+: {
      'bbeth-1350': { ip: '198.51.100.18', subnet: '198.51.100.16/30' },
    },
  },
});

local test2 = ubuntuVm(2, unixBed(3) + {
  labs: ['unix'],
  host+: {
    valid_terms: ['telnet', 'ssh', 'console'],
    valid_transfers: ['nc', 'scp', 'sftp', 'ftp'],
    console_options: { server: 'test1', port: 4001 },
    resources: ['test2-console'],
  },
});

local test3 = ubuntuVm(3, unixBed(4) + {
  labs: ['unix'],
  host+: { docker_priority: 10 },
});

// test4 is the hop of every Zephyr instance and of the chaos guest.
local test4 = ubuntuVm(4, {
  labs: ['embedded', 'chaos'],
  host+: {
    interfaces+: {
      'ubeth-data': { ip: '203.0.113.6', subnet: '203.0.113.4/30' },
    },
  },
});

{
  labs: {
    unix: {
      resources: ['test1', 'test2', 'test3'],
      metadata: { description: 'unix regression bed' },
    },
    busybox: {
      resources: ['test1', 'bb1161', 'bb1211', 'bb1281', 'bb1310', 'bb1350'],
    },
    embedded: {
      resources: [
        'test4',
        'zephyr37_fat',
        'zephyr37_lfs',
        'zephyr37_nofs',
        'zephyr27_fat',
        'zephyr44_lfs',
        'zephyr37_llext',
        'zephyr44_llext',
      ],
    },
    chaos: {
      resources: ['test4', 'ubu2404'],
      metadata: { description: 'reboot chaos: test4 and its QEMU Ubuntu guest' },
    },
  },
  elements: [
    test1,
    test2,
    test3,
    test4,
    {
      name: 'zephyr37_fat',
      labs: ['embedded'],
      hosts: [
        {
          ip: '192.0.2.1',
          os_type: 'zephyr',
          os_version: '3.7',
          valid_transfers: ['console'],
          filesystem: 'fat-ram',
          max_filename_len: 32,
          is_virtual: true,
          hop: 'test4',
          snmp: {
            address: test4.host.ip,
            port: 16101,
            community: 'public',
            oids: [
              '1.3.6.1.2.1.1.3.0',
              '1.3.6.1.4.1.63245.1.1.0',
              '1.3.6.1.4.1.63245.1.2.0',
              '1.3.6.1.4.1.63245.1.3.0',
              '1.3.6.1.4.1.63245.1.4.0',
            ],
          },
        },
      ],
    },
    {
      name: 'zephyr37_lfs',
      labs: ['embedded'],
      hosts: [
        {
          ip: '192.0.2.5',
          os_type: 'zephyr',
          os_version: '3.7',
          valid_transfers: ['console'],
          filesystem: 'littlefs',
          max_filename_len: 32,
          is_virtual: true,
          hop: 'test4',
          snmp: {
            address: test4.host.ip,
            port: 16102,
            community: 'public',
            oids: [
              '1.3.6.1.2.1.1.3.0',
              '1.3.6.1.4.1.63245.1.1.0',
              '1.3.6.1.4.1.63245.1.2.0',
              '1.3.6.1.4.1.63245.1.3.0',
              '1.3.6.1.4.1.63245.1.4.0',
            ],
          },
        },
      ],
    },
    {
      name: 'zephyr37_nofs',
      labs: ['embedded'],
      hosts: [
        {
          ip: '192.0.2.37',
          os_type: 'zephyr',
          os_version: '3.7',
          command_frame: 'zephyr-serial',
          valid_transfers: ['console'],
          filesystem: 'none',
          max_filename_len: 32,
          is_virtual: true,
          valid_terms: ['console'],
          console_options: {
            server: 'test4',
            port: 2325,
            write_chunk_size: 64,
            write_chunk_delay: 0.015,
          },
          hop: 'test4',
        },
      ],
    },
    {
      name: 'zephyr27_fat',
      labs: ['embedded'],
      hosts: [
        {
          ip: '192.0.2.13',
          os_type: 'zephyr',
          os_version: '2.7',
          command_frame: 'zephyr-inline',
          valid_transfers: ['console'],
          filesystem: 'fat-ram',
          max_filename_len: 32,
          is_virtual: true,
          hop: 'test4',
        },
      ],
    },
    {
      name: 'zephyr44_lfs',
      labs: ['embedded'],
      hosts: [
        {
          ip: '192.0.2.29',
          os_type: 'zephyr',
          os_version: '4.4',
          valid_transfers: ['console'],
          filesystem: 'littlefs',
          max_filename_len: 32,
          is_virtual: true,
          hop: 'test4',
          snmp: {
            address: test4.host.ip,
            port: 16108,
            community: 'public',
            oids: [
              '1.3.6.1.2.1.1.3.0',
              '1.3.6.1.4.1.63245.1.1.0',
              '1.3.6.1.4.1.63245.1.2.0',
              '1.3.6.1.4.1.63245.1.3.0',
              '1.3.6.1.4.1.63245.1.4.0',
            ],
          },
        },
      ],
    },
    {
      name: 'zephyr37_llext',
      labs: ['embedded'],
      hosts: [
        {
          ip: '192.0.2.33',
          os_type: 'zephyr',
          os_version: '3.7',
          command_frame: 'zephyr-serial',
          valid_transfers: ['console'],
          filesystem: 'none',
          loader: 'llext-hex',
          max_filename_len: 32,
          is_virtual: true,
          valid_terms: ['console'],
          console_options: {
            server: 'test4',
            port: 2323,
            write_chunk_size: 64,
            write_chunk_delay: 0.015,
          },
          toolchain: {
            sysroot: '/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi',
            gcov: 'bin/arm-zephyr-eabi-gcov',
            lcov: '/usr/bin/lcov',
          },
          hop: 'test4',
        },
      ],
    },
    {
      name: 'zephyr44_llext',
      labs: ['embedded'],
      hosts: [
        {
          ip: '192.0.2.34',
          os_type: 'zephyr',
          os_version: '4.4',
          command_frame: 'zephyr-serial',
          valid_transfers: ['console'],
          filesystem: 'none',
          loader: 'llext-hex',
          max_filename_len: 32,
          is_virtual: true,
          valid_terms: ['console'],
          console_options: {
            server: 'test4',
            port: 2324,
            write_chunk_size: 64,
            write_chunk_delay: 0.015,
          },
          toolchain: {
            sysroot: '/home/vagrant/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi',
            gcov: 'bin/arm-zephyr-eabi-gcov',
            lcov: '/usr/bin/lcov',
          },
          hop: 'test4',
        },
      ],
    },
    {
      name: 'ubu2404',
      labs: ['chaos'],
      hosts: [
        {
          _comment: 'The chaos reboot target: an Ubuntu 24.04 QEMU guest behind test4. CHAOS_GUEST in scripts/render_chaos_guest_seed.py is the source of truth for these literals.',
          ip: '203.0.113.1',
          os_type: 'unix',
          board: 'qemu',
          os_name: 'ubuntu',
          os_version: '24.04',
          valid_terms: ['ssh'],
          valid_transfers: ['scp', 'sftp'],
          is_virtual: true,
          hop: 'test4',
          creds: creds,
          interfaces: {
            eth1: { ip: '203.0.113.1', subnet: '203.0.113.0/30' },
            eth2: { ip: '203.0.113.5', subnet: '203.0.113.4/30' },
          },
        },
      ],
    },
    {
      name: 'bb1161',
      labs: ['busybox'],
      hosts: [
        {
          ip: '198.51.100.1',
          os_type: 'unix',
          command_frame: 'ash',
          board: 'qemu',
          sw_version: '1.16.1',
          valid_terms: ['telnet'],
          valid_transfers: ['shell', 'nc'],
          is_virtual: true,
          hop: 'test1',
          creds: [{ login: 'root', password: 'otto' }],
          userland_options: {
            applet_base64: 'absent',
            applet_nc: 'present',
            applet_poweroff: 'present',
            applet_scp: 'absent',
            applet_shutdown: 'absent',
            applet_uudecode: 'present',
            applet_uuencode: 'present',
            base64_flag: 'absent',
            checksum: 'md5sum',
            elevation: 'su',
            stat_size: 'stat',
            timeout_style: 'dash-t',
          },
        },
      ],
    },
    {
      name: 'bb1211',
      labs: ['busybox'],
      hosts: [
        {
          ip: '198.51.100.5',
          os_type: 'unix',
          command_frame: 'ash',
          board: 'qemu',
          sw_version: '1.21.1',
          valid_terms: ['telnet'],
          valid_transfers: ['shell', 'nc'],
          is_virtual: true,
          hop: 'test1',
          creds: [{ login: 'root', password: 'otto' }],
          userland_options: {
            applet_base64: 'present',
            applet_nc: 'present',
            applet_poweroff: 'present',
            applet_scp: 'absent',
            applet_shutdown: 'absent',
            applet_uudecode: 'present',
            applet_uuencode: 'present',
            base64_flag: '-d',
            checksum: 'md5sum',
            elevation: 'su',
            stat_size: 'stat',
            timeout_style: 'dash-t',
          },
        },
      ],
    },
    {
      name: 'bb1281',
      labs: ['busybox'],
      hosts: [
        {
          ip: '198.51.100.9',
          os_type: 'unix',
          command_frame: 'ash',
          board: 'qemu',
          sw_version: '1.28.1',
          valid_terms: ['telnet'],
          valid_transfers: ['shell', 'nc'],
          is_virtual: true,
          hop: 'test1',
          creds: [{ login: 'root', password: 'otto' }],
          userland_options: {
            applet_base64: 'present',
            applet_nc: 'present',
            applet_poweroff: 'present',
            applet_scp: 'absent',
            applet_shutdown: 'absent',
            applet_uudecode: 'present',
            applet_uuencode: 'present',
            base64_flag: '-d',
            checksum: 'md5sum',
            elevation: 'su',
            stat_size: 'stat',
            timeout_style: 'dash-t',
          },
        },
      ],
    },
    {
      name: 'bb1310',
      labs: ['busybox'],
      hosts: [
        {
          ip: '198.51.100.13',
          os_type: 'unix',
          command_frame: 'ash',
          board: 'qemu',
          sw_version: '1.31.0',
          valid_terms: ['telnet'],
          valid_transfers: ['shell', 'nc'],
          is_virtual: true,
          hop: 'test1',
          creds: [{ login: 'root', password: 'otto' }],
          userland_options: {
            applet_base64: 'present',
            applet_nc: 'present',
            applet_poweroff: 'present',
            applet_scp: 'absent',
            applet_shutdown: 'absent',
            applet_uudecode: 'present',
            applet_uuencode: 'present',
            base64_flag: '-d',
            checksum: 'md5sum',
            elevation: 'su',
            stat_size: 'stat',
            timeout_style: 'coreutils',
          },
        },
      ],
    },
    {
      name: 'bb1350',
      labs: ['busybox'],
      hosts: [
        {
          ip: '198.51.100.17',
          os_type: 'busybox',
          board: 'qemu',
          sw_version: '1.35.0',
          valid_terms: ['telnet', 'ssh', 'console'],
          valid_transfers: ['shell', 'nc'],
          console_options: { server: 'test1', port: 2450, escapes_iac: false },
          is_virtual: true,
          hop: 'test1',
          creds: [{ login: 'root', password: 'otto' }],
          userland_options: {
            applet_base64: 'present',
            applet_nc: 'present',
            applet_poweroff: 'present',
            applet_scp: 'absent',
            applet_shutdown: 'absent',
            applet_uudecode: 'present',
            applet_uuencode: 'present',
            base64_flag: '-d',
            checksum: 'md5sum',
            elevation: 'su',
            stat_size: 'stat',
            timeout_style: 'coreutils',
          },
          interfaces: {
            eth0: { ip: '198.51.100.17', subnet: '198.51.100.16/30' },
          },
        },
      ],
    },
  ],
  links: [
    {
      endpoints: [
        { host: 'test1', interface: 'eth2' },
        { host: 'test2', interface: 'eth2' },
      ],
      protocol: 'udp',
    },
    {
      _comment: "The bb1350 guest's /30 to its TAP on test1 -- the ONLY wire the guest has.",
      endpoints: [
        { host: 'test1', interface: 'bbeth-1350' },
        { host: 'bb1350-qemu', interface: 'eth0' },
      ],
      protocol: 'tcp',
    },
    {
      _comment: "The chaos guest's data plane to its TAP on test4; eth1 (mgmt) is never impaired.",
      endpoints: [
        { host: 'test4', interface: 'ubeth-data' },
        { host: 'ubu2404-qemu', interface: 'eth2' },
      ],
      protocol: 'udp',
    },
  ],
}
