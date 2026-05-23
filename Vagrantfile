
# TODO: Add this step to the dev README
# Must install the vagrant hostmanager plugin
# vagrant plugin install vagrant-hostmanager

Vagrant.configure("2") do |config|

  config.vm.box = "bento/ubuntu-24.04"
  config.hostmanager.enabled = true
  config.hostmanager.manage_host = true

    config.vm.provision "shell", name: "global", keep_color: true, inline: <<-SHELL

        # Update package list
        apt update

        # Install helpful packages
        apt install -y  make      \
                        net-tools \
                        tree      \

        # Ensure python points to python3
        ln -s /usr/bin/python3 /usr/bin/python

        # Set CLI mode to vi
        echo 'set -o vi' >> /home/vagrant/.bashrc

        # Set timezone to Chicago
        sudo timedatectl set-timezone America/Chicago

        # Clean up
        apt clean
    SHELL

    config.vm.provision "shell", name: "global", privileged: false, keep_color: true, inline: <<-SHELL

        # Set CLI mode to vi for the default non-root user
        echo 'set -o vi' >> ~/.bashrc
    SHELL

    # Publicly accessible VM
    config.vm.define "dev", primary: true do |dev|

        dev.vm.provider "virtualbox" do |vb|
            vb.cpus = 4
        end

        # Set the hostname for the host machine
        dev.vm.hostname = "otto.dev"

        # Disable the default SSH forwarding rule and enable a unique one
        dev.vm.network "forwarded_port", guest: 22, host: 5678

        # Private network (shared with test VMs)
        dev.vm.network "private_network", ip: "10.10.200.100"

        dev.vm.provision "shell", name: "dev-root", keep_color: true, inline: <<-SHELL

            # Install GitHub client and development tools for coverage testing
            apt install -y  gcc     \
                            gh      \
                            lcov    \

            # Set MTU to 1350 on all ethernet interfaces to support mobile
            # connections that have a smaller MTU size
            printf '[Match]\\nType=ether\\n\\n[Link]\\nMTUBytes=1350\\n' > /etc/systemd/network/10-mtu.link
        SHELL

        dev.vm.provision "shell", name: "dev", privileged: false, keep_color: true, inline: <<-SHELL

            # Set the default branch name to `main` instead of `master`
            git config --global init.defaultBranch main

            # Install uv via the official install script
            curl -LsSf https://astral.sh/uv/install.sh | sh

            # Set up shell completions for uv and uvx
            echo 'eval "$(uv generate-shell-completion bash)"' >> ~/.bashrc
            echo 'eval "$(uvx --generate-shell-completion bash)"' >> ~/.bashrc

            echo 'git config --global core.editor "vim"' >> ~/.bashrc
            echo 'alias gs="git status"' >> ~/.bashrc
        SHELL
    end

    config.vm.define "test1", autostart: false do |test1|
        test1.vm.network "private_network", ip: "10.10.200.11"

        # Apply test provisioning
        provision_test_vm(test1, "test1")
    end

    config.vm.define "test2", autostart: false do |test2|
        test2.vm.network "private_network", ip: "10.10.200.12"

        # Apply test provisioning
        provision_test_vm(test2, "test2")
    end

    config.vm.define "test3", autostart: false do |test3|
        test3.vm.network "private_network", ip: "10.10.200.13"

        # Apply test provisioning (shared with test1, test2)
        provision_test_vm(test3, "test3")

        # Install Docker so otto's docker container hosts can use test3 as
        # their parent. test3 is the only docker-capable VM in the lab data
        # (`docker_capable: true` on `pepper` in tests/lab_data/tech1/hosts.json).
        test3.vm.provision "shell", name: "test3 docker", keep_color: true, inline: <<-SHELL

            # Install docker engine + compose v2 plugin from Ubuntu's repos.
            # `docker-compose-v2` provides `docker compose` (v2 plugin) which
            # is the spelling otto uses; the legacy `docker-compose` binary
            # is intentionally not installed.
            apt -y install  docker.io                \
                            docker-compose-v2

            # Let the `vagrant` user (the credential otto authenticates as)
            # talk to the docker socket without sudo. Otto authenticates as
            # `vagrant` per tests/lab_data/tech1/hosts.json.
            usermod -aG docker vagrant

            systemctl enable --now docker
        SHELL
    end

    # Ubuntu VM that hosts a Zephyr instance under QEMU. otto reaches the
    # Zephyr shell by SSHing to this VM (the hop) and then telnetting to the
    # QEMU-internal IP 192.0.2.1:23.
    #
    # First provisioning is slow (~10+ min): the Zephyr SDK download is large
    # and the toolchain install runs an x86_64-zephyr-elf setup. After that,
    # `vagrant provision zephyr` re-runs only the changed steps.
    config.vm.define "zephyr", autostart: false do |zephyr|
        zephyr.vm.network "private_network", ip: "10.10.200.14"

        # Standard SSH/telnet/FTP baseline (also sets the hostname). Lets otto
        # use this VM as the SSH hop to reach the Zephyr QEMU instance.
        provision_test_vm(zephyr, "zephyr")

        # Override the 1552 MB default — Zephyr SDK install and `west build`
        # need more headroom. Specified AFTER provision_test_vm so the later
        # provider block wins.
        zephyr.vm.provider "virtualbox" do |vb|
            vb.memory = 4096
            vb.cpus = 2
        end

        # QEMU + Zephyr build dependencies. Package list mirrors the Zephyr
        # Getting Started guide for Ubuntu — but with the host's gcc-multilib
        # / g++-multilib DROPPED. Those are only needed when you build Zephyr
        # against system gcc; we install the Zephyr SDK (with its own
        # x86_64-zephyr-elf cross-toolchain) so the host gcc is irrelevant.
        # Dropping them also lets the same Vagrantfile work on arm64 hosts,
        # where the multilib packages have no installation candidate.
        # socat + libpcap-dev are pulled by net-tools' loop-* helper scripts
        # even when we use the Ethernet path.
        zephyr.vm.provision "shell", name: "zephyr-deps", keep_color: true, inline: <<-SHELL

            apt -y install  qemu-system-x86           \
                            socat                     \
                            libpcap-dev               \
                            git                       \
                            cmake                     \
                            ninja-build               \
                            gperf                     \
                            ccache                    \
                            dfu-util                  \
                            device-tree-compiler      \
                            wget                      \
                            xz-utils                  \
                            file                      \
                            libsdl2-dev               \
                            libmagic1                 \
                            python3-dev               \
                            python3-pip               \
                            python3-venv              \
                            python3-setuptools        \
                            python3-tk                \
                            python3-wheel
        SHELL

        # Bootstrap the Zephyr workspace as the unprivileged `vagrant` user —
        # west and the SDK should not be owned by root. Pinned to the v3.7
        # LTS branch; bump these versions to track newer LTS as needed.
        zephyr.vm.provision "shell", name: "zephyr-workspace", privileged: false, keep_color: true, inline: <<-SHELL
            set -e

            ZEPHYR_BRANCH="v3.7-branch"
            ZEPHYR_SDK_VERSION="0.16.8"

            # Isolate west and Zephyr's Python tooling from system Python.
            if [ ! -d ~/zephyr-venv ]; then
                python3 -m venv ~/zephyr-venv
            fi
            source ~/zephyr-venv/bin/activate
            pip install --quiet --upgrade pip
            pip install --quiet west

            # Initialize the Zephyr workspace pinned to the LTS branch via
            # the shallow + narrow fast path. Default `west init`/`west update`
            # clones Zephyr's full history plus every module in the manifest
            # (every vendor HAL — atmel, espressif, nordic, st, xtensa, ...)
            # at full depth, which can take 10+ minutes. The pattern below is
            # what Zephyr's own CI uses and is roughly an order of magnitude
            # faster:
            #   1. shallow-clone Zephyr itself at the LTS branch tip,
            #   2. `west init -l` to register that local clone as the
            #      workspace's manifest source,
            #   3. `west update --narrow -o=--depth=1` to fetch each module
            #      as a shallow clone of just the manifest-pinned commit
            #      (--narrow skips other branches/tags).
            # `.west` presence keeps the step idempotent across re-provisions.
            if [ ! -d ~/zephyrproject/.west ]; then
                mkdir -p ~/zephyrproject
                git clone --depth 1 --branch "${ZEPHYR_BRANCH}" \
                    https://github.com/zephyrproject-rtos/zephyr.git \
                    ~/zephyrproject/zephyr
                west init -l ~/zephyrproject/zephyr
            fi
            cd ~/zephyrproject
            west update --narrow -o=--depth=1
            west zephyr-export
            pip install --quiet -r zephyr/scripts/requirements.txt

            # Install the Zephyr SDK — minimal tarball plus just the
            # x86_64-zephyr-elf toolchain (all we need for qemu_x86). The
            # SDK ships per-host-arch tarballs; pick by `uname -m`.
            SDK_HOST_ARCH="$(uname -m)"
            case "${SDK_HOST_ARCH}" in
                x86_64)  SDK_HOST="linux-x86_64"  ;;
                aarch64) SDK_HOST="linux-aarch64" ;;
                *) echo "Unsupported host arch: ${SDK_HOST_ARCH}" >&2; exit 1 ;;
            esac
            SDK_TARBALL="zephyr-sdk-${ZEPHYR_SDK_VERSION}_${SDK_HOST}_minimal.tar.xz"
            cd ~
            if [ ! -d zephyr-sdk-${ZEPHYR_SDK_VERSION} ]; then
                wget -q "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}/${SDK_TARBALL}"
                tar xf "${SDK_TARBALL}"
                rm "${SDK_TARBALL}"
                cd zephyr-sdk-${ZEPHYR_SDK_VERSION}
                ./setup.sh -t x86_64-zephyr-elf -h -c
            fi

            # net-tools provides net-setup.sh (creates the host-side zeth TAP)
            # and the loop-* helper scripts. Shallow clone — we only need
            # the tip.
            if [ ! -d ~/net-tools ]; then
                git clone --depth 1 https://github.com/zephyrproject-rtos/net-tools.git ~/net-tools
            fi

            # Build a STOCK Zephyr shell sample with otto's Kconfig + DT
            # overlays layered on, once per filesystem config. otto ships no
            # firmware code — not even an empty main.c — so each build is
            # unmodified Zephyr; the only otto contribution is the
            # EXTRA_CONF_FILE / EXTRA_DTC_OVERLAY_FILE that flip standard
            # Kconfig options (telnet shell backend, networking, runtime
            # stats, filesystem). Same shape as `sshd_config` on a Unix host.
            # /vagrant is the synced share of the otto-sh checkout on the host.
            #
            # Three configs build into three independent dirs under ~/build/.
            # `west build -p auto` is incremental within each dir — first
            # provision builds clean, subsequent provisions only rebuild what
            # the overlays changed. The `no_fs` config is the only one without
            # a DT overlay (it has no filesystem at all).
            cd ~/zephyrproject
            source zephyr/zephyr-env.sh
            for cfg in v3_7_fat_ram v3_7_lfs v3_7_no_fs; do
                dt_flag=""
                if [ -f /vagrant/tests/firmware/zephyr/configs/$cfg/app.overlay ]; then
                    dt_flag="-DEXTRA_DTC_OVERLAY_FILE=/vagrant/tests/firmware/zephyr/configs/$cfg/app.overlay"
                fi
                echo "=== building zephyr config: $cfg ==="
                west build -p auto -b qemu_x86 \
                    zephyr/samples/subsys/shell/shell_module \
                    -d /home/vagrant/build/$cfg \
                    -- -DEXTRA_CONF_FILE="/vagrant/tests/firmware/zephyr/common/otto-overlay.conf;/vagrant/tests/firmware/zephyr/configs/$cfg/overlay.conf" \
                       $dt_flag
            done
        SHELL

        # Per-config systemd units running each Zephyr image under QEMU.
        #
        # Why hand-rolled QEMU (vs `west build -t run`): running multiple
        # Zephyr instances concurrently requires each to attach to a distinct
        # TAP. Zephyr's qemu_x86 board.cmake hard-codes the TAP name (`zeth`)
        # in its QEMU args, with no Kconfig knob to override it. So otto
        # invokes qemu-system-i386 directly, parametrizing the TAP name and
        # the build directory per config. The flags below are the qemu_x86
        # invocation Zephyr 3.7 LTS uses: `-cpu qemu32,+nx,+pae` is
        # load-bearing (without PAE the guest triple-faults before the kernel
        # runs); `q35` is the PCIe-capable machine the e1000 NIC requires.
        # **Build-verify these flags on first provision** — the exact set
        # may differ slightly from what `west build -t run` would produce.
        #
        # Each instance gets its own TAP (`zeth-<id>`) created in-band by an
        # `ExecStartPre=ip tuntap add ...` (more compact and easier to
        # parametrize than net-tools' `zeth.conf`), its own host-side IP on
        # 192.0.2.0/24 (.2, .4, .6) matching the Zephyr-side IP set in its
        # config's overlay.conf (.1, .3, .5), and its own systemd unit
        # (`zephyr-qemu-<id>.service`) so any one can be restarted
        # independently — useful when iterating on a single config's overlays.
        zephyr.vm.provision "shell", name: "zephyr-qemu", keep_color: true, inline: <<-SHELL

            # `cfg:short:host_ip` per entry. The `short` is a TAP-friendly id
            # (Linux IFNAMSIZ caps interface names at 15 chars, so the long
            # cfg ids like `v3_7_fat_ram` overflow `zeth-${cfg}` — confirmed
            # by `Error: argument "zeth-v3_7_fat_ram" is wrong: "name" not a
            # valid ifname`). Build dirs and systemd unit names use the long
            # cfg id; only the kernel-visible TAP name uses the short.
            for cfg_entry in "v3_7_fat_ram:fat:192.0.2.2" \
                             "v3_7_lfs:lfs:192.0.2.4" \
                             "v3_7_no_fs:nofs:192.0.2.6"; do
                cfg=$(echo "$cfg_entry" | cut -d: -f1)
                short=$(echo "$cfg_entry" | cut -d: -f2)
                host_ip=$(echo "$cfg_entry" | cut -d: -f3)
                tap_name="zeth-${short}"

                cat > /home/vagrant/run-zephyr-qemu-${cfg}.sh <<EOF
#!/usr/bin/env bash
# Launch the ${cfg} Zephyr image under QEMU on TAP ${tap_name}. Hand-rolled
# QEMU invocation (vs west build -t run) so each instance can take a
# distinct TAP; see the Vagrantfile comment for the rationale.
set -euo pipefail
exec qemu-system-i386 \\
    -m 9 \\
    -cpu qemu32,+nx,+pae \\
    -machine q35 \\
    -device isa-debug-exit,iobase=0xf4,iosize=0x04 \\
    -no-reboot \\
    -nographic \\
    -serial mon:stdio \\
    -nic tap,ifname=${tap_name},script=no,downscript=no,id=net0,model=e1000 \\
    -kernel /home/vagrant/build/${cfg}/zephyr/zephyr.elf
EOF
                chown vagrant:vagrant /home/vagrant/run-zephyr-qemu-${cfg}.sh
                chmod +x /home/vagrant/run-zephyr-qemu-${cfg}.sh

                cat > /etc/systemd/system/zephyr-qemu-${cfg}.service <<EOF
[Unit]
Description=Zephyr shell sample (${cfg}) under QEMU on ${tap_name}
After=network.target

[Service]
Type=simple
User=vagrant
ExecStartPre=+/bin/sh -c 'ip link del ${tap_name} 2>/dev/null; ip tuntap add ${tap_name} mode tap user vagrant && ip link set ${tap_name} up && ip addr add ${host_ip}/24 dev ${tap_name}'
ExecStart=/home/vagrant/run-zephyr-qemu-${cfg}.sh
ExecStopPost=+/bin/sh -c 'ip link set ${tap_name} down 2>/dev/null; ip tuntap del ${tap_name} mode tap 2>/dev/null; true'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
            done

            systemctl daemon-reload

            # Clean up the legacy single-instance unit from a prior provision
            # (it was named `zephyr-qemu.service` before the multi-config
            # refactor). Safe to run when it doesn't exist.
            systemctl stop zephyr-qemu.service 2>/dev/null || true
            systemctl disable zephyr-qemu.service 2>/dev/null || true
            rm -f /etc/systemd/system/zephyr-qemu.service /home/vagrant/run-zephyr-qemu.sh

            # And its TAP. The legacy unit's ExecStopPost did not always run
            # cleanly on transitions — if `zeth` lingers with 192.0.2.2/24,
            # the new FAT instance's ExecStartPre fails to add the same /24
            # to `zeth-v3_7_fat_ram` (the kernel rejects duplicate addrs).
            ip link del zeth 2>/dev/null || true

            for cfg in v3_7_fat_ram v3_7_lfs v3_7_no_fs; do
                systemctl enable --now zephyr-qemu-${cfg}.service
            done
        SHELL

        # Optional TFTP path — deferred per the embedded plan but provisioned
        # for future use. Bound to zeth-v3_7_fat_ram's host side (192.0.2.2)
        # only, so it is reachable from the FAT Zephyr instance but not
        # exposed on the private network. Ordered After=/Requires= the FAT
        # service so tftpd binds after that TAP exists. (Future: a parallel
        # set of unit drop-ins per Zephyr instance, if TFTP becomes the
        # default transfer for any of them.)
        zephyr.vm.provision "shell", name: "zephyr-tftp", keep_color: true, inline: <<-SHELL

            apt -y install tftpd-hpa

            mkdir -p /srv/tftp
            chmod 777 /srv/tftp

            cat > /etc/default/tftpd-hpa <<EOF
TFTP_USERNAME="tftp"
TFTP_DIRECTORY="/srv/tftp"
TFTP_ADDRESS="192.0.2.2:69"
TFTP_OPTIONS="--secure --create --permissive"
EOF

            mkdir -p /etc/systemd/system/tftpd-hpa.service.d
            cat > /etc/systemd/system/tftpd-hpa.service.d/override.conf <<EOF
[Unit]
After=zephyr-qemu-v3_7_fat_ram.service
Requires=zephyr-qemu-v3_7_fat_ram.service
EOF

            systemctl daemon-reload
            systemctl enable tftpd-hpa
        SHELL
    end

    # Dynamically set hostname based on VM name
    def set_hostname(vm, name, domain = nil)
        base = name.gsub("_", "-")
        vm.vm.hostname = domain ? "#{base}.#{domain}" : base
    end

    # Run common test VM provisioning steps
    def provision_test_vm(vm, name)
        set_hostname(vm, name)

        # The test VMs idle well under 2 GB, so reduce the amount per VM
        # to ease memory pressure on the host.
        vm.vm.provider "virtualbox" do |vb|
            vb.memory = 1552
        end

        vm.vm.provision "shell", name: "common test", keep_color: true, inline: <<-SHELL

            # install SSH and Telnet server
            apt -y install  net-tools \
                            telnetd   \
                            vsftpd    \

            # Uncomment telnet from the inetd config file, then start and enable inetd
            sed -i 's/#<off># *//' /etc/inetd.conf
            systemctl enable --now inetutils-inetd.service

            # Enable FTP write access (needed for uploads) and disable the seccomp
            # sandbox (vsftpd 3.0.5+ on Ubuntu 24.04 blocks writes by default),
            # then start and enable vsftpd. ``restart`` (not ``enable --now``)
            # is required because the package installer above already started
            # the service with the unedited default config — ``--now`` is a
            # no-op on an already-running service, so without an explicit
            # restart the conf edits don't take effect and FTP uploads return
            # 550 Permission denied.
            sed -i 's/#write_enable=YES/write_enable=YES/' /etc/vsftpd.conf
            grep -q seccomp_sandbox /etc/vsftpd.conf || echo 'seccomp_sandbox=NO' >> /etc/vsftpd.conf
            systemctl enable vsftpd
            systemctl restart vsftpd

            # Set the vagrant user's password to 'vagrant' for SSH/telnet access
            echo 'vagrant:vagrant' | sudo chpasswd

            # Create an additional user 'test' for SSH and telnet access.
            useradd -m -s /bin/bash test
            echo 'test:Password1' | chpasswd
        SHELL
    end

    # Build and install guest additions if the kernel version changes
    # NOTE: This function is currently not used, but could be useful in the future
    # if a box's kernel updates and causes the guest additions kernel modules to
    # no longer insert into the guest OS.
    def apply_guest_additions(vm)
        vm.vm.provision "shell", name: "guest additions", keep_color: true, inline: <<-SHELL

            # Ensure packages for vbguest plugins are available
            apt -y install  build-essential           \
                            dkms                      \
                            gcc                       \
                            linux-headers-$(uname -r) \
                            make                      \
                            perl                      \

            # NOTE: The version used should match the version of your VirtualBox software
            # Possibly parameterize the provision script by grabbing the first argument and defaulting to 7.2.6
            # See https://developer.hashicorp.com/vagrant/docs/provisioning/shell#args for how to parameterize
            vbox_ver="${1:-7.2.6}"

            wget http://download.virtualbox.org/virtualbox/${vbox_ver}/VBoxGuestAdditions_${vbox_ver}.iso -P /tmp
            mount -o loop /tmp/VBoxGuestAdditions_${vbox_ver}.iso /mnt

            # Run the appropriate Linux Additions script based on the ISA
            case "$(uname -m)" in
            x86_64|i?86)
                sh -x /mnt/VBoxLinuxAdditions.run
                ;;
            aarch64|arm64)
                sh -x /mnt/VBoxLinuxAdditions-arm64.run
                ;;
            *)
                echo "Unknown or unsupported architecture: $(uname -m)"
                ;;
            esac

            /opt/VBoxGuestAdditions*/init/vboxadd setup
        SHELL
    end

end
