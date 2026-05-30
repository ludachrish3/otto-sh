
# Overview

I'd like to add support for real time OSes, like Zephyr, to `otto`. The goal is for them to have the same Python API as the Linux hosts we've already defined (for running commands, file transfers, etc.).

## Test bed

One of the first challenges to figure out is how to test this functionality. I did some research about setting up a virtual Zephyr host, and I've included a sample Vagrantfile addition below. Please use it as a guide for forming the actual Vagrantfile changes that should be made, as well as any lab file updates that should accompany it. It seems like an additional Ubuntu VM will be needed, and that VM will be a hop before reaching the Zephyr QEMU instance. But this is all just my initial direction. I'd like to go with whatever is industry standard and best emulates real world usage of a Zephyr host.

```ruby
zephyr.vm.provision "shell", name: "zephyr-tftp", keep_color: true, inline: <<-SHELL
  apt-get install -y tftpd-hpa

  # Bind tftpd to the TAP-side address only — no need to expose it on
  # the private network since Zephyr reaches it directly at 192.0.2.2.
  # --create allows tftp PUT operations to create new files (default is
  # write-only into pre-existing files, which is rarely what you want).
  mkdir -p /srv/tftp
  chmod 777 /srv/tftp

  cat > /etc/default/tftpd-hpa <<EOF
TFTP_USERNAME="tftp"
TFTP_DIRECTORY="/srv/tftp"
TFTP_ADDRESS="192.0.2.2:69"
TFTP_OPTIONS="--secure --create --permissive"
EOF

  # tftpd-hpa needs to be (re)started AFTER zeth0 exists, so make it
  # depend on the zephyr-qemu service (which sets up zeth0 via its
  # ExecStartPre net-setup script).
  mkdir -p /etc/systemd/system/tftpd-hpa.service.d
  cat > /etc/systemd/system/tftpd-hpa.service.d/override.conf <<EOF
[Unit]
After=zephyr-qemu.service
Requires=zephyr-qemu.service
EOF

  systemctl daemon-reload
  systemctl enable tftpd-hpa
SHELL
```

## Class Structure

I really want the API and user interface to match the Unix host API as much as possible. I know that the Unix hosts so far have been `RemoteHost` instances. This poses a couple challenges. First is the trivial but consequential naming aspect. `RemoteHost` might have been too generic for what it is because it's really more of a `UnixHost`, with Linux being the concrete example. I see this new functionality as a `BareMetalHost` (please propose a different name for this host type if you can think of one) with Zephyr being a concrete example. In the future, there could be support for Windows added, but there's no need AT ALL to implement or design for that. Just keep in mind that more OSes could be added.

Lab data is another consideration. It's no longer safe to assume that a single OS applies to each host because it could be Unix or it could be bare metal. Each host entry should require an `osType` field, with `unix` and `bare-metal` being the valid options. An optional `osName` and `osVersion` should also be added as well. "Linux" can be the default name for `unix` hosts, and "Zephyr" can be the default for `bare-metal` hosts. Note that this reflects the KERNEL version. Ubuntu has its own version number, but it's more helpful to track the kernel version because that's what kernel modules are developed against.

## Features

### Command Execution

I'd like the command execution to exactly mirror the Unix interface as close as possible. Telnet can be assumed for the sake of remote command execution. SSH is unlikely to be supported, but it doesn't need to be excluded unless that makes implementation easier. It can always be added later if there becomes a use case for SSH on a bare-metal host.

I really like the sentinel-based approach that also captures return codes. As a sample approach, I believe Zephyr has a printf-like functionality that can effectively wrap a function call and capture its return code to print with the output. Ideally, the return code can be retrieved in some way. If there's no solid way to do this, I can live without the return code. Otherwise, one-shot commands and `send()` and `expect()` should behave the same way as the existing `RemoteHost` class.

### File Transfers

I'd really like to transfer files to and from the `BareMetalHost`s, but I know that we can't really rely on a TFTP or FTP server being around. What optinos do we have for transferring files to/from a bare-metal host? Should "files" be read and written directly from blocks of memory? I believe NASA has an embedded coverage tool (<https://github.com/nasa-jpl/embedded-gcov>) that effectively handles the GCDA format in an embedded system. It might be worth checking that codebase for inspiration. If the file transfer method is entirely based on telnet commands, that's totally fine by me. This would be a new file transfer method that is only valid for bare-metal hosts.

### Code Coverage

Retrieving code coverage data from an embedded program is tablestakes for this implementation. I'd like to use the NASA code coverage tool to accopmlish this. We don't need to necessarily implement this now, but this is on the near roadmap in my mind. Consider this in scope if the complexity is low to implement at this time. I'd also like a test embedded program, similar to the one already used to test coverage on Unix hosts, that can be built with NASA's embedded coverage tool and deployed to the Zephyr host for testing.

## Monitor

What are some standard ways to monitor CPU and memory usage on a Zephyr host? I'm guessing it's **quite** different than Unix, so it makes a lot of sense for the two class types to have a different set of default monitoring commands. Maybe SNMP will need to be enabled?

## CLI

I'd like this to fit in just like all existing hosts in the `otto host` CLI command.

## Docker

There's no need to support Docker within a bare-metal host. Any attempt to define or set up a Docker container on a bare-metal host can raise an exception.
