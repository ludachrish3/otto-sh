// otto's own test bed: four Ubuntu VMs, five BusyBox guests, seven Zephyr
// targets. Each family's hosts share one shape, so one function writes it,
// and each element below states only what makes it different. otto renders
// this file into the same document a lab.json holds.

// doc: begin ubuntu-vm
// Ubuntu VM n: 10.10.200.(10+n) on the management network, 192.168.1.(10+n)
// on the test network.
local vmIp(n) = '10.10.200.' + (10 + n);

local ubuntuVm(n, labs, host={}, element={}) = {
  name: 'test' + n,
  labs: labs,
  hosts: [{
    ip: vmIp(n),
    os_type: 'unix',
    valid_terms: ['ssh', 'telnet'],
    valid_transfers: ['scp', 'sftp', 'ftp', 'nc'],
    is_virtual: true,
    creds: [
      { login: 'vagrant', password: 'vagrant' },
      { login: 'test', password: 'Password1' },
    ],
    interfaces: {
      eth2: { ip: '192.168.1.' + (10 + n), subnet: '192.168.1.0/24' },
    },
  } + host],
} + element;

// The docker-capable VMs, racked in lab-a.
local racked(shelf) = { site: 'lab-a', rack: 1, shelf: shelf, docker_capable: true };
// doc: end ubuntu-vm

// doc: begin zephyr-target
// A Zephyr target, reached through test4.
local zephyr(name, ip, version, filesystem, host={}) = {
  name: name,
  labs: ['embedded'],
  hosts: [{
    ip: ip,
    os_type: 'zephyr',
    os_version: version,
    valid_transfers: ['console'],
    filesystem: filesystem,
    max_filename_len: 32,
    is_virtual: true,
    hop: 'test4',
  } + host],
};

// The target's SNMP agent, forwarded to a port on test4.
local snmp(port) = {
  snmp: {
    address: vmIp(4),
    port: port,
    community: 'public',
    oids: [
      '1.3.6.1.2.1.1.3.0',
      '1.3.6.1.4.1.63245.1.1.0',
      '1.3.6.1.4.1.63245.1.2.0',
      '1.3.6.1.4.1.63245.1.3.0',
      '1.3.6.1.4.1.63245.1.4.0',
    ],
  },
};

// A UART shell, served by a telnet server on test4.
local uart(port) = {
  command_frame: 'zephyr-serial',
  valid_terms: ['console'],
  console_options: { server: 'test4', port: port, write_chunk_size: 64, write_chunk_delay: 0.015 },
};

// Loadable extensions, built with the toolchain under *sysroot*.
local llext(sysroot) = {
  loader: 'llext-hex',
  toolchain: { sysroot: sysroot, gcov: 'bin/arm-zephyr-eabi-gcov', lcov: '/usr/bin/lcov' },
};
// doc: end zephyr-target

// doc: begin busybox
// The probe's answers on a BusyBox 1.21.1 guest, pinned so otto asks nothing.
// A guest whose answers differ pins this with its differences merged over it.
local busyboxPin = {
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
};

// BusyBox guest k, a QEMU guest behind test1. Its network is the /30
// 198.51.100.(4k)/30, and the guest is that network's first address,
// 198.51.100.(4k+1). Its element is named after its version, so 1.16.1 is
// bb1161. *pin* is its userland_options; a guest given none has every
// capability probed.
local busybox(k, version, pin=null, host={}, element={}) = {
  name: 'bb' + std.strReplace(version, '.', ''),
  labs: ['busybox'],
  hosts: [{
    ip: '198.51.100.' + (4 * k + 1),
    os_type: 'unix',
    command_frame: 'ash',
    board: 'qemu',
    sw_version: version,
    valid_terms: ['telnet'],
    valid_transfers: ['shell', 'nc'],
    is_virtual: true,
    hop: 'test1',
    creds: [{ login: 'root', password: 'otto' }],
    [if pin != null then 'userland_options']: pin,
  } + host],
} + element;
// doc: end busybox

// doc: begin labs
local labs = {
  unix: {
    resources: ['unix-bench'],
    metadata: { description: 'unix regression bed' },
  },
  busybox: { resources: ['bb-bench'] },
  embedded: { resources: ['embedded-bench'] },
  pyrepl: { resources: ['unix-bench'] },
};
// doc: end labs

// The elements the case study's pages show, each by name. The document at
// the end lists every element, these and the rest, in order.

// doc: begin test1
local test1 = ubuntuVm(1, ['unix', 'busybox'], host=racked(3) {
  session_setup: { type: 'provision-app', env: 'lab' },
  creds+: [{ login: 'root', proxy: 'sudo-root', via: 'vagrant' }],
  interfaces+: {
    'bbeth-1350': { ip: '198.51.100.18', subnet: '198.51.100.16/30' },
  },
}, element={
  resources: ['test1-chassis'],
  metadata: { role: 'hub' },
});
// doc: end test1

// doc: begin pyrepl
local test1Py = {
  name: 'test1-py',
  labs: ['pyrepl'],
  hosts: [{
    ip: vmIp(1),
    os_type: 'unix',
    valid_terms: ['ssh'],
    valid_transfers: ['scp', 'sftp'],
    is_virtual: true,
    creds: [{ login: 'vagrant', password: 'vagrant' }],
    landing_frame: 'bash',
    command_frame: 'pyrepl',
    session_setup: 'enter-python',
  }],
};
// doc: end pyrepl

// doc: begin test2
local test2 = ubuntuVm(2, ['unix'], host=racked(3) {
  valid_terms: ['telnet', 'ssh', 'console'],
  valid_transfers: ['nc', 'scp', 'sftp', 'ftp'],
  console_options: { server: 'test1', port: 4001 },
  resources: ['test2-console'],
});
// doc: end test2

// doc: begin zephyr37_fat
local zephyr37Fat = zephyr('zephyr37_fat', '192.0.2.1', '3.7', 'fat-ram', snmp(16101));
// doc: end zephyr37_fat

// doc: begin zephyr37_lfs
local zephyr37Lfs = zephyr('zephyr37_lfs', '192.0.2.5', '3.7', 'littlefs', snmp(16102) {
  landing_frame: 'raw',
  session_setup: 'land-on-zephyr',
});
// doc: end zephyr37_lfs

// doc: begin zephyr27_fat
local zephyr27Fat = zephyr('zephyr27_fat', '192.0.2.13', '2.7', 'fat-ram', {
  command_frame: 'zephyr-inline',
});
// doc: end zephyr27_fat

// doc: begin bb1161
local bb1161 = busybox(0, '1.16.1', pin=busyboxPin {
  applet_base64: 'absent',
  base64_flag: 'absent',
});
// doc: end bb1161

// doc: begin bb1350
local bb1350 = busybox(4, '1.35.0', pin=busyboxPin { timeout_style: 'coreutils' }, host={
  valid_terms: ['telnet', 'ssh', 'console'],
  console_options: { server: 'test1', port: 2450, escapes_iac: false },
  resources: ['bb1350-slot'],
  interfaces: { eth0: { ip: '198.51.100.17', subnet: '198.51.100.16/30' } },
}, element={
  resources: ['bb1350-chassis'],
});
// doc: end bb1350

// doc: begin document
{
  labs: labs,
  elements: [
    test1,
    test1Py,
    test2,
    ubuntuVm(3, ['unix'], host=racked(4)),
    ubuntuVm(4, ['embedded']),
    zephyr37Fat,
    zephyr37Lfs,
    zephyr('zephyr37_nofs', '192.0.2.37', '3.7', 'none', uart(2325)),
    zephyr27Fat,
    zephyr('zephyr44_lfs', '192.0.2.29', '4.4', 'littlefs', snmp(16108)),
    zephyr('zephyr37_llext', '192.0.2.33', '3.7', 'none',
           uart(2323) + llext('/home/vagrant/zephyr-sdk-0.16.8/arm-zephyr-eabi')),
    zephyr('zephyr44_llext', '192.0.2.34', '4.4', 'none',
           uart(2324) + llext('/home/vagrant/zephyr-sdk-1.0.1/gnu/arm-zephyr-eabi')),
    bb1161,
    busybox(1, '1.21.1', pin=busyboxPin),
    busybox(2, '1.28.1', pin=busyboxPin),
    busybox(3, '1.31.0', pin=busyboxPin { timeout_style: 'coreutils' }),
    bb1350,
  ],
  links: [
    {
      endpoints: [
        { host: 'test1', interface: 'eth2' },
        { host: 'test2', interface: 'eth2' },
      ],
      protocol: 'udp',
    },
    // The bb1350 guest's /30 to its TAP on test1: the only wire the guest has.
    {
      endpoints: [
        { host: 'test1', interface: 'bbeth-1350' },
        { host: 'bb1350-qemu', interface: 'eth0' },
      ],
      protocol: 'tcp',
    },
  ],
}
// doc: end document
