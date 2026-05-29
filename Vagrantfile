
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
        # need more headroom, and one QEMU process runs per built config.
        # Sized for the multi-LTS matrix: three workspaces (2.7 / 3.7 / 4.4)
        # across two SDKs (0.16.8 for 2.7+3.7, 1.0.1 for 4.4; ~1 GB each on
        # disk, ~1.5 GB resident peak during `west build`), and 9 concurrent
        # QEMU instances (3 LTSes x 3 configs) at 256 MB apiece. 8 GB / 4 vCPU
        # gives comfortable headroom and keeps the build phase parallelizable.
        # Specified AFTER provision_test_vm so
        # the later provider block wins.
        zephyr.vm.provider "virtualbox" do |vb|
            vb.memory = 8192
            vb.cpus = 4
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

        # Bootstrap the Zephyr workspace(s) as the unprivileged `vagrant`
        # user — west and the SDK should not be owned by root.
        #
        # Multi-LTS layout: one independent (venv, west workspace, SDK) tuple
        # per Zephyr version, driven by the ZEPHYR_VERSIONS table below.
        # Older LTSes pin different west / Python-tooling versions, so each
        # gets its own venv to avoid cross-version dependency conflicts; the
        # SDK is per-version because the contemporary SDK shipped with each
        # LTS is the known-good one for that branch's compiler flags and
        # toolchain expectations.
        #
        # Adding a new Zephyr LTS = one row in ZEPHYR_VERSIONS plus a matching
        # set of overlay configs under tests/firmware/zephyr/configs/. No
        # structural changes to this provisioner.
        zephyr.vm.provision "shell", name: "zephyr-workspace", privileged: false, keep_color: true, inline: <<-SHELL
            set -e

            # Per-version table — heredoc-fed at the bottom of the version
            # loop. Fields, pipe-separated:
            #
            #   version_id | git branch | SDK version | configs (space-sep)
            #
            # version_id  drives the workspace dir (~/zephyrproject-${ver}) and
            #             the venv dir (~/zephyr-venv-${ver}). Use an
            #             underscore-only id so it composes safely into systemd
            #             unit names downstream.
            # SDK version pins the Zephyr-SDK release used for that branch.
            #             2.7 and 3.7 share 0.16.8 — the SDK is just an
            #             x86_64-zephyr-elf cross-toolchain that's forward-
            #             compatible across those branches for `qemu_x86`, and
            #             the contemporary SDK for 2.7 (0.13.x) doesn't ship the
            #             aarch64 `_minimal.tar.xz` format this loop downloads.
            #             4.4 pins 1.0.1: it declares SDK_VERSION 1.0.1 and
            #             won't accept 0.16.x. Per-version SDKs coexist (install
            #             dirs are version-named); bump here if a build surfaces
            #             a real version-incompatibility.
            #
            #             (1.14 was evaluated and dropped: its shell/fs/kernel
            #             command interface is byte-identical to 2.7 from
            #             otto's side, so it added no test coverage, and the
            #             2019-era kernel miscompiles under the SDK's gcc 12 —
            #             the era gcc 8 has no aarch64 build. See git history.)
            # configs     names the build-config directories under
            #             tests/firmware/zephyr/configs/ to build for that
            #             version. Each builds into /home/vagrant/build/${cfg}/.
            #
            # The while loop reads from a heredoc *redirected* into the loop
            # rather than from a pipe (`echo … | while read`). The redirected
            # form runs the loop body in the current shell, so `set -e` and
            # any variables set inside persist — the piped form spawns a
            # subshell that silently drops both.

            # One-time migration from the single-version layout that predates
            # the ZEPHYR_VERSIONS table (~/zephyrproject and ~/zephyr-venv were
            # 3.7-only). Renames in place to the per-version paths so the
            # 10-min `west init`+`west update` doesn't re-run on the first
            # multi-LTS provision. Safe no-op once the new paths exist.
            if [ -d ~/zephyrproject ] && [ ! -d ~/zephyrproject-v3_7 ]; then
                echo "=== migrating ~/zephyrproject -> ~/zephyrproject-v3_7 ==="
                mv ~/zephyrproject ~/zephyrproject-v3_7
            fi
            if [ -d ~/zephyr-venv ] && [ ! -d ~/zephyr-venv-v3_7 ]; then
                echo "=== migrating ~/zephyr-venv -> ~/zephyr-venv-v3_7 ==="
                mv ~/zephyr-venv ~/zephyr-venv-v3_7
            fi

            # net-tools provides net-setup.sh (creates the host-side zeth TAP)
            # and the loop-* helper scripts. Shared across all Zephyr versions
            # — shallow clone, we only need the tip.
            if [ ! -d ~/net-tools ]; then
                git clone --depth 1 https://github.com/zephyrproject-rtos/net-tools.git ~/net-tools
            fi

            # SDK tarballs are per-host-arch; resolve once for the loop.
            SDK_HOST_ARCH="$(uname -m)"
            case "${SDK_HOST_ARCH}" in
                x86_64)  SDK_HOST="linux-x86_64"  ;;
                aarch64) SDK_HOST="linux-aarch64" ;;
                *) echo "Unsupported host arch: ${SDK_HOST_ARCH}" >&2; exit 1 ;;
            esac

            while IFS='|' read -r ZVER ZBRANCH ZSDK ZCFGS; do
                ZWORKSPACE="${HOME}/zephyrproject-${ZVER}"
                ZVENV="${HOME}/zephyr-venv-${ZVER}"
                echo ""
                echo "############################################################"
                echo "### Zephyr ${ZVER} (${ZBRANCH}, SDK ${ZSDK})"
                echo "############################################################"

                # Reset to neutral ground before any west command. `west
                # init` aborts with "already initialized in <dir>" if the
                # *current directory* is inside an existing workspace — and
                # the prior iteration ends with cwd inside that version's
                # workspace (the build step cd's there). west also honors
                # ZEPHYR_BASE for workspace discovery, which the prior
                # iteration's `source zephyr-env.sh` sets. Clear both so each
                # version initializes against its own workspace only.
                cd "${HOME}"
                unset ZEPHYR_BASE

                # Per-version venv keeps west / Zephyr Python deps isolated.
                # Older LTSes pin older west releases with their own dep trees;
                # sharing one venv would force every version onto whichever
                # west happened to be installed last.
                if [ ! -d "${ZVENV}" ]; then
                    python3 -m venv "${ZVENV}"
                fi
                # shellcheck disable=SC1091
                source "${ZVENV}/bin/activate"
                pip install --quiet --upgrade pip
                # setuptools is required for its vendored `distutils` shim:
                # Python 3.12 (Ubuntu 24.04) removed distutils from the
                # stdlib (PEP 632), and a fresh 3.12 venv no longer bundles
                # setuptools. Older Zephyr build scripts still
                # `from distutils.version import LooseVersion`
                # (e.g. 2.7's scripts/gen_kobject_list.py), which fails with
                # ModuleNotFoundError unless setuptools provides the shim.
                # Harmless for 3.7, whose scripts no longer use distutils.
                pip install --quiet setuptools west

                # Initialize the Zephyr workspace via the shallow + narrow
                # fast path. Default `west init`/`west update` clones Zephyr's
                # full history plus every module in the manifest (every vendor
                # HAL — atmel, espressif, nordic, st, xtensa, ...) at full
                # depth, which can take 10+ minutes. The pattern below is what
                # Zephyr's own CI uses and is roughly an order of magnitude
                # faster:
                #   1. shallow-clone Zephyr itself at the LTS branch tip,
                #   2. `west init -l` to register that local clone as the
                #      workspace's manifest source,
                #   3. `west update --narrow -o=--depth=1` to fetch each
                #      module as a shallow clone of just the manifest-pinned
                #      commit (--narrow skips other branches/tags).
                # `.west` presence keeps the step idempotent across re-provisions.
                if [ ! -d "${ZWORKSPACE}/.west" ]; then
                    # The guard keys on .west, which `west init` only creates
                    # on success. A prior provision that died between
                    # `git clone` and `west init` (e.g. the cwd/ZEPHYR_BASE
                    # bug) leaves a populated zephyr/ checkout but no .west —
                    # the clone below would then fail with "destination path
                    # already exists and is not an empty directory". Wipe the
                    # partial checkout first so re-provisioning self-heals.
                    # Only the re-fetchable git clone is removed; this branch
                    # never runs against a healthy workspace (.west present).
                    rm -rf "${ZWORKSPACE}/zephyr"
                    mkdir -p "${ZWORKSPACE}"
                    git clone --depth 1 --branch "${ZBRANCH}" \
                        https://github.com/zephyrproject-rtos/zephyr.git \
                        "${ZWORKSPACE}/zephyr"
                    west init -l "${ZWORKSPACE}/zephyr"
                fi
                cd "${ZWORKSPACE}"
                west update --narrow -o=--depth=1
                # `west zephyr-export` writes the Zephyr CMake package to
                # ~/.cmake/packages/Zephyr so find_package(Zephyr) resolves
                # without ZEPHYR_BASE. It's a Zephyr *extension* command from
                # the 2.x CMake-package era; both LTSes built here register it.
                # Guarded on the workspace's command registry so that adding a
                # future pre-2.0 LTS row (which lacks the command and instead
                # resolves Zephyr via the ZEPHYR_BASE that zephyr-env.sh sets in
                # the build step below) self-heals rather than aborting with
                # "unknown command zephyr-export".
                if grep -q 'zephyr-export' zephyr/scripts/west-commands.yml 2>/dev/null; then
                    west zephyr-export
                fi
                pip install --quiet -r zephyr/scripts/requirements.txt

                # Install the Zephyr SDK — minimal tarball plus just the
                # x86_64-zephyr-elf toolchain (all we need for qemu_x86).
                # SDK install dirs are version-named, so multiple SDKs
                # coexist without aliasing.
                SDK_TARBALL="zephyr-sdk-${ZSDK}_${SDK_HOST}_minimal.tar.xz"
                if [ ! -d "${HOME}/zephyr-sdk-${ZSDK}" ]; then
                    cd "${HOME}"
                    wget -q "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZSDK}/${SDK_TARBALL}"
                    tar xf "${SDK_TARBALL}"
                    rm "${SDK_TARBALL}"
                    cd "zephyr-sdk-${ZSDK}"
                    ./setup.sh -t x86_64-zephyr-elf -h -c
                fi

                # Build a STOCK Zephyr shell sample with otto's Kconfig + DT
                # overlays layered on, once per filesystem config. otto ships
                # no firmware code — not even an empty main.c — so each build
                # is unmodified Zephyr; the only otto contribution is the
                # EXTRA_CONF_FILE / EXTRA_DTC_OVERLAY_FILE that flip standard
                # Kconfig options (telnet shell backend, networking, runtime
                # stats, filesystem). Same shape as `sshd_config` on a Unix
                # host. /vagrant is the synced share of the otto-sh checkout
                # on the host.
                #
                # Each config builds into its own dir under ~/build/. The
                # `no_fs` configs are the only ones without a DT overlay
                # (they have no filesystem at all).
                #
                # `-p always` (pristine rebuild every provision), not
                # `-p auto`, because `auto` compares the cmake configure-time
                # *arguments* — the overlay file paths — not the file
                # *contents*. An overlay edit changes the bytes inside
                # `overlay.conf` but the cmake args stay the same, so
                # `-p auto` reports "no work to do" and the binary is stale
                # (Phase 5.5 found this the hard way: IP assignments moved in
                # the overlays but the running QEMU kept the old IPs).
                # `-p always` is slower but correct. The toolchain and
                # west-managed source caches persist across runs, so the
                # rebuild is ~30 s per config on a warm VM.
                cd "${ZWORKSPACE}"
                # shellcheck disable=SC1091
                source zephyr/zephyr-env.sh

                # Pin the toolchain to THIS version's declared SDK. Multiple
                # SDKs coexist (2.7/3.7 on 0.16.8, 4.4 on 1.0.1), and Zephyr's
                # auto-detection otherwise picks the *highest* installed SDK
                # meeting the board minimum. On a first provision that happens
                # to be correct only by install order (1.0.1 isn't present yet
                # when 2.7/3.7 build); on a re-provision (all SDKs already
                # installed) 2.7/3.7 would silently build against 1.0.1 — a far
                # newer gcc than those vintages expect. Pinning makes every
                # build deterministic and matches the ZSDK column above.
                export ZEPHYR_SDK_INSTALL_DIR="${HOME}/zephyr-sdk-${ZSDK}"

                # The CMake vars that *append* otto's Kconfig + DT overlays to
                # the sample's own prj.conf/board overlay were renamed in
                # Zephyr 3.4: EXTRA_CONF_FILE / EXTRA_DTC_OVERLAY_FILE replaced
                # the older OVERLAY_CONFIG / DTC_OVERLAY_FILE. 3.7 takes the new
                # names; 2.7 predates them. Critically, the old CMake
                # *silently ignores* an unknown -D var — the build still
                # succeeds but with NONE of otto's overlays applied (no telnet
                # backend, no networking), so the QEMU instance boots a stock
                # serial shell otto can't reach. Pick the names the workspace's
                # CMake actually honors. Both varieties accept the same
                # "a;b" semicolon list, so only the var name changes.
                # (OVERLAY_CONFIG layers on top of CONF_FILE just like
                # EXTRA_CONF_FILE; the shell_module sample ships no auto-applied
                # board overlay on qemu_x86, so setting DTC_OVERLAY_FILE
                # outright is equivalent to the additive EXTRA_ form here.)
                if grep -rq "EXTRA_CONF_FILE" zephyr/cmake; then
                    conf_var="EXTRA_CONF_FILE"
                    dtc_var="EXTRA_DTC_OVERLAY_FILE"
                else
                    conf_var="OVERLAY_CONFIG"
                    dtc_var="DTC_OVERLAY_FILE"
                fi

                # Optional per-version supplement layered between the shared
                # overlay and the per-config overlay. Holds symbols that exist
                # on this LTS but not the oldest in the matrix (e.g. the
                # SCHED_THREAD_USAGE* CPU-stat detail and the ETH_DRIVER gate on
                # 3.7/4.x). Keeps common/otto-overlay.conf a true cross-version
                # intersection so 2.7 doesn't abort on undefined symbols. Absent
                # for a version => no supplement (that's the intended 2.7 case).
                ver_overlay=""
                if [ -f /vagrant/tests/firmware/zephyr/common/otto-overlay-${ZVER}.conf ]; then
                    ver_overlay="/vagrant/tests/firmware/zephyr/common/otto-overlay-${ZVER}.conf;"
                fi

                for cfg in ${ZCFGS}; do
                    dt_flag=""
                    if [ -f /vagrant/tests/firmware/zephyr/configs/$cfg/app.overlay ]; then
                        dt_flag="-D${dtc_var}=/vagrant/tests/firmware/zephyr/configs/$cfg/app.overlay"
                    fi
                    echo "=== building zephyr config: $cfg ==="
                    west build -p always -b qemu_x86 \
                        zephyr/samples/subsys/shell/shell_module \
                        -d /home/vagrant/build/$cfg \
                        -- -D${conf_var}="/vagrant/tests/firmware/zephyr/common/otto-overlay.conf;${ver_overlay}/vagrant/tests/firmware/zephyr/configs/$cfg/overlay.conf" \
                           $dt_flag
                done

                deactivate
            done <<EOF
v3_7|v3.7-branch|0.16.8|v3_7_fat_ram v3_7_lfs v3_7_no_fs
v2_7|v2.7-branch|0.16.8|v2_7_fat_ram v2_7_lfs v2_7_no_fs
v4_4|v4.4-branch|1.0.1|v4_4_fat_ram v4_4_lfs v4_4_no_fs
EOF
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
            #
            # Each instance gets its own /30 subnet so the host's routing
            # table has a distinct route per TAP — instances on a shared
            # /24 produce overlapping routes, the kernel picks one, and the
            # others are unreachable. Multi-LTS /30 layout (Zephyr/host):
            #   3.7  FAT:    .1/.2    (192.0.2.0/30)
            #   3.7  LFS:    .5/.6    (192.0.2.4/30)
            #   3.7  no_fs:  .9/.10   (192.0.2.8/30)
            #   2.7  FAT:    .13/.14  (192.0.2.12/30)
            #   2.7  LFS:    .17/.18  (192.0.2.16/30)
            #   2.7  no_fs:  .21/.22  (192.0.2.20/30)
            #   4.4  FAT:    .25/.26  (192.0.2.24/30)
            #   4.4  LFS:    .29/.30  (192.0.2.28/30)
            #   4.4  no_fs:  .33/.34  (192.0.2.32/30)
            # Resolve the Zephyr-SDK qemu binary ONCE here and bake the
            # absolute path into each wrapper script below. An earlier draft
            # tried to defer this to wrapper-run time (``SDK_QEMU=\$(ls
            # ...)`` inside the heredoc), but Ruby's <<-SHELL heredoc strips
            # the backslash from ``\$`` before bash ever sees it — the ``ls``
            # then ran at provision time anyway *and* the ``\$SDK_QEMU``
            # expansion in ``exec`` became ``$SDK_QEMU`` (empty), yielding
            # ``exec ""`` in the wrapper.
            SDK_QEMU=$(ls /home/vagrant/zephyr-sdk-*/sysroots/*/usr/bin/qemu-system-i386 | head -1)
            if [ -z "$SDK_QEMU" ]; then
                echo "ERROR: zephyr-sdk qemu-system-i386 not found under /home/vagrant/zephyr-sdk-*/" >&2
                exit 1
            fi

            for cfg_entry in "v3_7_fat_ram:fat:192.0.2.2"      \
                             "v3_7_lfs:lfs:192.0.2.6"          \
                             "v3_7_no_fs:nofs:192.0.2.10"      \
                             "v2_7_fat_ram:27fat:192.0.2.14"   \
                             "v2_7_lfs:27lfs:192.0.2.18"       \
                             "v2_7_no_fs:27nofs:192.0.2.22"    \
                             "v4_4_fat_ram:44fat:192.0.2.26"   \
                             "v4_4_lfs:44lfs:192.0.2.30"       \
                             "v4_4_no_fs:44nofs:192.0.2.34"; do
                cfg=$(echo "$cfg_entry" | cut -d: -f1)
                short=$(echo "$cfg_entry" | cut -d: -f2)
                host_ip=$(echo "$cfg_entry" | cut -d: -f3)
                tap_name="zeth-${short}"

                # qemu_x86's emulated machine model differs by Zephyr era: the
                # board moved from i440FX (qemu's default `pc` machine) to `q35`
                # in Zephyr 3.0. 2.7 page-faults in early boot if launched on
                # q35 (its kernel/page-tables assume i440FX), so it must use the
                # default machine + `-no-acpi`; 3.7 and 4.4 use `q35,acpi=off`.
                # These mirror each version's own `west build -t run` flags —
                # hardcoding q35 for all silently crash-loops 2.7 at boot. (The
                # version is the `cfg` prefix; see the wiki principle
                # "delegate to the build system's run target".)
                case "$cfg" in
                    v2_7_*) machine_flags="-no-acpi" ;;
                    *)      machine_flags="-machine q35,acpi=off" ;;
                esac

                cat > /home/vagrant/run-zephyr-qemu-${cfg}.sh <<EOF
#!/usr/bin/env bash
# Launch the ${cfg} Zephyr image under QEMU on TAP ${tap_name}. Hand-rolled
# QEMU invocation (vs west build -t run) so each instance can take a
# distinct TAP — Zephyr's run target hard-codes ifname=zeth.
#
# Uses the Zephyr-SDK-bundled qemu-system-i386 (v7.0.0), not the
# apt-installed system one (v8.2.2). Something in e1000 emulation
# changed between those versions that breaks Zephyr 3.7's e1000 driver:
# the guest boots and shell_telnet inits, but ARP replies never make it
# back through to the host (TX increments, RX never moves). The SDK
# binary is what \`west build -t run\` uses, and is the known-good one.
#
# The SDK qemu path is resolved at provision time (above) and baked in
# here so we never have to wrangle backslash escaping across Ruby /
# bash / heredoc layers.
#
# Flags mirror Zephyr's own qemu_x86 build verbatim, with only the TAP
# ifname diverging:
#   -m 256             Zephyr kernel + network stack + shell need ~10 MB;
#                      the rest is headroom for the FAT/RAM and LittleFS
#                      flash-sim partitions, which are both RAM-backed
#                      and sized at 100 MiB (see configs/v3_7_fat_ram/
#                      app.overlay and configs/v3_7_lfs/app.overlay).
#                      qemu_x86 is 32-bit (4 GiB max); 256 MiB is well
#                      under that and leaves room for future growth.
#   machine model    Per-version (baked in above): 3.x+ qemu_x86 is q35
#                      without ACPI; 2.7 is i440FX (qemu's default `pc`
#                      machine) and faults on q35, so it gets -no-acpi only.
#   -chardev/-serial/-mon  Zephyr's preferred serial+monitor multiplex
#                      on stdio.
set -euo pipefail
exec ${SDK_QEMU} \\
    -m 256 \\
    -cpu qemu32,+nx,+pae \\
    ${machine_flags} \\
    -device isa-debug-exit,iobase=0xf4,iosize=0x04 \\
    -no-reboot \\
    -nographic \\
    -chardev stdio,id=con,mux=on \\
    -serial chardev:con \\
    -mon chardev=con,mode=readline \\
    -nic tap,model=e1000,script=no,downscript=no,ifname=${tap_name} \\
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
ExecStartPre=+/bin/sh -c 'ip link del ${tap_name} 2>/dev/null; ip tuntap add ${tap_name} mode tap user vagrant && ip link set ${tap_name} up && ip addr add ${host_ip}/30 dev ${tap_name}'
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

            # Stale TAPs from prior provisions — both the legacy single-
            # instance `zeth` and the pre-shortening long names from before
            # the IFNAMSIZ fix. Each `ip link del` is a no-op if absent.
            # Without this, a previously-running service holds the OLD TAP
            # name (since `systemctl enable --now` does not restart already-
            # running units to pick up an updated unit file).
            for iface in zeth                                                       \
                         zeth-v3_7_fat_ram zeth-v3_7_lfs zeth-v3_7_no_fs            \
                         zeth-fat zeth-lfs zeth-nofs                                \
                         zeth-27fat zeth-27lfs zeth-27nofs                          \
                         zeth-44fat zeth-44lfs zeth-44nofs; do
                ip link del "$iface" 2>/dev/null || true
            done

            # `restart` (not just `enable --now`) so already-running services
            # actually pick up the regenerated unit file, wrapper script, and
            # — critically — the fresh TAPs the new ExecStartPre creates.
            for cfg in v3_7_fat_ram v3_7_lfs v3_7_no_fs   \
                       v2_7_fat_ram v2_7_lfs v2_7_no_fs   \
                       v4_4_fat_ram v4_4_lfs v4_4_no_fs; do
                systemctl enable zephyr-qemu-${cfg}.service
                systemctl restart zephyr-qemu-${cfg}.service
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
