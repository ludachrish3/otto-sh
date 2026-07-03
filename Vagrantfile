
# TODO: Add this step to the dev README
# Must install the vagrant hostmanager plugin
# vagrant plugin install vagrant-hostmanager

# Shared Zephyr-SDK install step, used by both the dev VM (arm-zephyr-eabi, for
# building the mps2_an385 coverage product + running the cross-gcov report) and
# the zephyr VM (the cov base's arm toolchain). Returns an inline shell snippet
# that installs the given toolchain from each named SDK version, idempotently:
#   - SDK dir + toolchain present -> skip.
#   - SDK dir absent              -> download + unpack the minimal tarball, then
#                                    `setup.sh -t <toolchain> -h -c` (host tools +
#                                    register the Zephyr-SDK CMake package).
#   - SDK dir present, toolchain absent -> add just that toolchain
#                                    (`setup.sh -t <toolchain> -c`), no re-download.
#                                    This is how the zephyr VM layers
#                                    arm-zephyr-eabi onto a 0.16.8 SDK that already
#                                    carries x86_64-zephyr-elf.
# SDK install dirs are version-named (~/zephyr-sdk-<ver>), so multiple SDKs and
# toolchains coexist. The per-host-arch tarball is resolved at runtime.
def zephyr_sdk_install(toolchain, versions)
  <<-SHELL
    set -euo pipefail
    case "$(uname -m)" in
        x86_64)  SDK_HOST="linux-x86_64"  ;;
        aarch64) SDK_HOST="linux-aarch64" ;;
        *) echo "unsupported host arch $(uname -m) for Zephyr SDK" >&2; exit 1 ;;
    esac
    for ZSDK in #{versions.join(" ")}; do
        SDK_DIR="${HOME}/zephyr-sdk-${ZSDK}"
        # Toolchain layout differs by SDK release: 0.16.x is flat
        # (<sdk>/<toolchain>); 1.0+ nests it under <sdk>/gnu/<toolchain>. Locate
        # it either way so the "already present" skip works across both (else a
        # 1.0+ SDK re-downloads its toolchain on every provision).
        TC_DIR="$(find "${SDK_DIR}" -maxdepth 2 -type d -name "#{toolchain}" 2>/dev/null | head -1)"
        if [ -n "${TC_DIR}" ]; then
            echo "Zephyr SDK ${ZSDK} #{toolchain} already present (${TC_DIR})."
        elif [ ! -d "${SDK_DIR}" ]; then
            echo "=== installing Zephyr SDK ${ZSDK} (#{toolchain}) ==="
            cd "${HOME}"
            SDK_TARBALL="zephyr-sdk-${ZSDK}_${SDK_HOST}_minimal.tar.xz"
            wget -q "https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZSDK}/${SDK_TARBALL}"
            tar xf "${SDK_TARBALL}"
            rm "${SDK_TARBALL}"
            ( cd "${SDK_DIR}" && ./setup.sh -t #{toolchain} -h -c )
        else
            echo "=== adding #{toolchain} toolchain to existing Zephyr SDK ${ZSDK} ==="
            ( cd "${SDK_DIR}" && ./setup.sh -t #{toolchain} -c )
        fi
        GCOV="$(find "${SDK_DIR}" -maxdepth 4 -name "#{toolchain}-gcov" 2>/dev/null | head -1)"
        if [ -n "${GCOV}" ]; then ls -l "${GCOV}"; else echo "WARNING: #{toolchain}-gcov not found under ${SDK_DIR}"; fi
    done
  SHELL
end

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

        # Grow the root LV + filesystem to use the full virtual disk. The
        # bento/ubuntu-24.04 box ships the root LV at only ~half its PV (a ~30 G
        # LV on a ~61 G partition of a 64 G disk), so the per-version Zephyr
        # workspaces below (~3.6 G each, x3) would otherwise run the box out of
        # space. Runs first, before the SDK/workspace downloads. Steps are
        # dynamic (no hard-coded vg/disk names) and idempotent — each is a no-op
        # once at max. growpart + pvresize also cover the case where the VDI is
        # later enlarged (e.g. via the vagrant-disksize plugin): the partition
        # then trails the disk and these grow it; lvextend + resize2fs fill the LV
        # into the free VG. resize2fs grows ext4 online, so no reboot is needed.
        dev.vm.provision "shell", name: "dev-grow-disk", keep_color: true, inline: <<-SHELL
            set -e
            apt-get install -y cloud-guest-utils >/dev/null 2>&1 || true  # growpart
            ROOT_LV="$(findmnt -no SOURCE /)"                       # /dev/mapper/<vg>-<lv>
            PV="$(pvs --noheadings -o pv_name | head -1 | tr -d ' ')" # e.g. /dev/sda3
            DISK="/dev/$(lsblk -no pkname "${PV}" | head -1)"       # e.g. /dev/sda (head -1: skip the LVM child row)
            PARTNUM="$(echo "${PV}" | grep -o '[0-9]*$')"
            echo "root LV=${ROOT_LV} PV=${PV} disk=${DISK} part=${PARTNUM}"
            if command -v growpart >/dev/null; then
                growpart "${DISK}" "${PARTNUM}" || true   # rc 2 == NOCHANGE
            fi
            pvresize "${PV}"
            lvextend -l +100%FREE "${ROOT_LV}" || true    # no-op if no free extents
            resize2fs "${ROOT_LV}"
            df -h / | tail -1
        SHELL

        dev.vm.provision "shell", name: "dev-root", keep_color: true, inline: <<-SHELL

            # GitHub client + coverage tools, plus the Zephyr build dependencies
            # needed to build the repo3 coverage product (.llext) on this VM
            # (see the dev-zephyr-sdk / dev-zephyr-workspace provisioners below).
            # wget + xz-utils fetch/unpack the SDK toolchain; cmake .. python3-*
            # are `west build` deps — the QEMU/networking/GUI packages the zephyr
            # VM also installs are intentionally omitted (this VM only *builds*
            # the product; the coverage instance runs on the zephyr VM).
            # ruby provides `ruby -c Vagrantfile` for syntax-checking edits to
            # this file from inside the dev VM.
            # graphviz provides `dot` for the architecture-docs diagrams
            # (sphinx.ext.graphviz / inheritance_diagram in `make docs`).
            # The libatk/libx*/libgbm/libasound/libatspi block is headless
            # Chromium's runtime for the Playwright dashboard e2e suite (the
            # set `playwright install-deps chromium` reports missing on this
            # box); the browser binary itself comes from `make browsers`.
            apt install -y  gcc                   \
                            gh                    \
                            graphviz              \
                            lcov                  \
                            ruby                  \
                            wget                  \
                            xz-utils              \
                            git                   \
                            cmake                 \
                            ninja-build           \
                            gperf                 \
                            device-tree-compiler  \
                            file                  \
                            libmagic1             \
                            python3-dev           \
                            python3-pip           \
                            python3-venv          \
                            libatk1.0-0t64        \
                            libxcomposite1        \
                            libxdamage1           \
                            libxext6              \
                            libxfixes3            \
                            libxrandr2            \
                            libgbm1               \
                            libasound2t64         \
                            libatspi2.0-0t64      \

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

        # Zephyr SDK arm-zephyr-eabi toolchains for the embedded (repo3) coverage
        # bed: the cross-gcov the coverage *report* runs
        # (tests/lab_data/tech1/hosts.json -> each host's `toolchain`) and the
        # compiler that builds the mps2_an385 coverage product on this VM. The
        # report gcov MUST be the same GCC that compiled the product's .gcno
        # (gcov's on-disk format is a GCC-internal ABI), so each Zephyr version
        # pins its own SDK: 0.16.8 for 2.7/3.7 (gcc 12.2), 1.0.1 for 4.4.
        # Installed via the shared zephyr_sdk_install helper (idempotent; defined
        # at the top of this file and shared with the zephyr VM).
        #
        # NOTE: this installs the toolchains only. *Building* the product .llext
        # additionally needs the per-version Zephyr workspaces below.
        dev.vm.provision "shell", name: "dev-zephyr-sdk", privileged: false, keep_color: true,
            inline: zephyr_sdk_install("arm-zephyr-eabi", ["0.16.8", "1.0.1"])

        # Per-version Zephyr workspaces for *building/testing* against each
        # otto-supported Zephyr version on this VM. 3.7 + 4.4 build the repo3
        # coverage product (.llext); 2.7 is for testing only (it predates LLEXT,
        # so it can't do coverage). One (venv, workspace) per version, mirroring
        # the zephyr VM's ZEPHYR_VERSIONS table — but with no QEMU/firmware build
        # (this VM only builds the product on demand; the instances run on the
        # zephyr VM). Needs the SDKs from dev-zephyr-sdk + the apt build deps
        # above. First run is slow (clones Zephyr + modules per version);
        # idempotent on re-provision.
        dev.vm.provision "shell", name: "dev-zephyr-workspace", privileged: false, keep_color: true, inline: <<-SHELL
            set -e
            # version_id | git branch. Redirected heredoc (not a pipe) so the loop
            # body runs in this shell and `set -e` persists (see the zephyr VM
            # provisioner). 2.7 + 3.7 share SDK 0.16.8; 4.4 uses 1.0.1 — both
            # installed by dev-zephyr-sdk above.
            while IFS='|' read -r ZVER ZBRANCH; do
                [ -n "${ZVER}" ] || continue
                ZWORKSPACE="${HOME}/zephyrproject-${ZVER}"
                ZVENV="${HOME}/zephyr-venv-${ZVER}"
                echo ""
                echo "### Zephyr ${ZVER} (${ZBRANCH}) build env"

                # Neutral ground before any west command: a prior iteration leaves
                # cwd + ZEPHYR_BASE inside its own workspace, which would misdirect
                # west init/discovery for the next version.
                cd "${HOME}"
                unset ZEPHYR_BASE

                # Per-version venv keeps each LTS's west / Python deps isolated.
                if [ ! -d "${ZVENV}" ]; then
                    python3 -m venv "${ZVENV}"
                fi
                # shellcheck disable=SC1091
                source "${ZVENV}/bin/activate"
                pip install --quiet --upgrade pip
                # setuptools for its distutils shim (Py3.12 dropped distutils;
                # 2.7's build scripts still import it). Harmless for 3.7/4.4.
                pip install --quiet setuptools west

                # Shallow + narrow workspace init (Zephyr CI's fast path).
                # Idempotent on .west; a partial checkout self-heals.
                if [ ! -d "${ZWORKSPACE}/.west" ]; then
                    rm -rf "${ZWORKSPACE}/zephyr"
                    mkdir -p "${ZWORKSPACE}"
                    git clone --depth 1 --branch "${ZBRANCH}" \
                        https://github.com/zephyrproject-rtos/zephyr.git \
                        "${ZWORKSPACE}/zephyr"
                    west init -l "${ZWORKSPACE}/zephyr"
                fi
                cd "${ZWORKSPACE}"
                west update --narrow -o=--depth=1
                if grep -q 'zephyr-export' zephyr/scripts/west-commands.yml 2>/dev/null; then
                    west zephyr-export
                fi
                pip install --quiet -r zephyr/scripts/requirements.txt

                # Apply the per-version source patches from
                # tests/firmware/zephyr/patches/ (see its README for the
                # what/why of each). The glob is version-gated, so each
                # iteration picks up only its own ${ZVER}-*.patch set.
                # Idempotent: the reverse-check skips an already-patched tree.
                for patch in /vagrant/tests/firmware/zephyr/patches/${ZVER}-*.patch; do
                    [ -f "${patch}" ] || continue
                    if git -C zephyr apply --reverse --check "${patch}" 2>/dev/null; then
                        echo "=== patch already applied: $(basename "${patch}") ==="
                    else
                        echo "=== applying patch: $(basename "${patch}") ==="
                        git -C zephyr apply "${patch}"
                    fi
                done
            done <<'VERS'
v2_7|v2.7-branch
v3_7|v3.7-branch
v4_4|v4.4-branch
VERS

            echo ""
            echo "Zephyr build envs ready: ~/zephyrproject-{v2_7,v3_7,v4_4}."
            echo "Build the coverage product (3.7/4.4) — see tests/repo3/product/README.md, e.g.:"
            echo "  tests/repo3/product/build.sh ~/build/cov_ext_app_v3_7 v3_7"
        SHELL
    end

    # Three interchangeable Unix test VMs — carrot / tomato / pepper. Identical
    # provisioning (SSH + telnet + FTP via provision_test_vm, plus docker via
    # provision_docker), so the test suite can lease whichever is free; only the
    # name and private-network IP differ. Add a peer = one row here.
    {
        "test1" => "10.10.200.11",  # carrot
        "test2" => "10.10.200.12",  # tomato
        "test3" => "10.10.200.13",  # pepper
    }.each do |name, ip|
        config.vm.define name, autostart: false do |node|
            node.vm.network "private_network", ip: ip
            provision_test_vm(node, name)
            provision_docker(node, name)
        end
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
                            qemu-system-arm           \
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

                # Apply any per-version source patches before building. otto's
                # default is STOCK Zephyr (overlays only, no firmware code — see
                # the build comment below); the deliberate exceptions live in
                # tests/firmware/zephyr/patches/ (README there has the full
                # story per patch):
                #   - v2_7-shell-retcode: 2.7's shell predates `retval`, so a
                #     one-line patch prints `retCode = <n>` after every command
                #     for otto's ZephyrInlineRetcodeFrame to parse.
                #   - v3_7-e1000-rx-ring: multi-descriptor RX ring for the
                #     qemu_x86 e1000 driver.
                #   - v{2_7,3_7}-fs-shell-mount-leak: backport of the upstream
                #     4.x fix — the fs-shell mount commands leaked their
                #     k_malloc'd mount-point buffer on every failed mount
                #     (guard before allocating + free on failure). This is
                #     what slowly drained the FAT instance's 16 KB heap; 4.4
                #     already ships the fix upstream.
                # The glob is version-gated, so each iteration finds only its
                # own version's patches.
                #
                # Idempotent: `git apply --reverse --check` succeeds only when
                # the patch is ALREADY applied, so a re-provision against the
                # persisted source tree skips it rather than failing "reversed".
                # A newly-added patch still lands because the reverse-check
                # fails on an unpatched tree. `-p always` below recompiles the
                # patched source.
                for patch in /vagrant/tests/firmware/zephyr/patches/${ZVER}-*.patch; do
                    [ -f "${patch}" ] || continue
                    if git -C zephyr apply --reverse --check "${patch}" 2>/dev/null; then
                        echo "=== patch already applied: $(basename "${patch}") ==="
                    else
                        echo "=== applying patch: $(basename "${patch}") ==="
                        git -C zephyr apply "${patch}"
                    fi
                done

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
                # The lone exception is the 2.7 source patch applied above
                # (`retCode` shell line) — a deliberate, single-line deviation
                # because 2.7's shell has no exit-code mechanism for otto to
                # read at all. 3.7 / 4.4 remain fully stock.
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

                # The "extra modules" CMake var — which pulls in otto's SNMP-agent
                # module — was renamed across the matrix, exactly like the conf var
                # above: 2.7 only knows ZEPHYR_EXTRA_MODULES; 3.x+ added the
                # reordered EXTRA_ZEPHYR_MODULES. An unknown -D is silently ignored,
                # so passing the wrong one leaves the module unregistered — its
                # OTTO_SNMP_AGENT symbol then stays *undefined*, and the overlay
                # that assigns it aborts the build with "undefined symbol". Pick the
                # name this workspace's CMake actually honors.
                if grep -rq "EXTRA_ZEPHYR_MODULES" zephyr/cmake; then
                    mod_var="EXTRA_ZEPHYR_MODULES"
                else
                    mod_var="ZEPHYR_EXTRA_MODULES"
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
                    # ${mod_var} pulls in otto's out-of-tree SNMP-agent module (a
                    # separate, non-contending monitoring channel — the Zephyr telnet
                    # shell allows only one session, so metrics can't ride the same
                    # console as command execution). Registering the module defines
                    # its CONFIG_OTTO_SNMP_AGENT symbol on every version; the agent is
                    # only *built* where an overlay sets it =y, which the per-version
                    # supplements do for 3.7/4.x. 2.7 leaves it off — the agent C
                    # targets the 3.0+ `zephyr/`-prefixed includes and the newer
                    # SYS_INIT signature — so there the module registers but compiles
                    # nothing.
                    west build -p always -b qemu_x86 \
                        zephyr/samples/subsys/shell/shell_module \
                        -d /home/vagrant/build/$cfg \
                        -- -D${conf_var}="/vagrant/tests/firmware/zephyr/common/otto-overlay.conf;${ver_overlay}/vagrant/tests/firmware/zephyr/configs/$cfg/overlay.conf" \
                           -D${mod_var}=/vagrant/tests/firmware/zephyr/snmp_agent \
                           $dt_flag
                done

                deactivate
            done <<EOF
v2_7|v2.7-branch|0.16.8|v2_7_fat_ram
v3_7|v3.7-branch|0.16.8|v3_7_fat_ram v3_7_lfs
v4_4|v4.4-branch|1.0.1|v4_4_lfs
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
            #   3.7  no_fs:  migrated to ARM serial — see ARM_INSTANCES (no_fs_arm)
            #   2.7  FAT:    .13/.14  (192.0.2.12/30)
            #   4.4  LFS:    .29/.30  (192.0.2.28/30)
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
                             "v2_7_fat_ram:27fat:192.0.2.14"   \
                             "v4_4_lfs:44lfs:192.0.2.30"; do
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
# binary is what 'west build -t run' uses, and is the known-good one.
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
#                      without ACPI; 2.7 is i440FX (qemu's default 'pc'
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
            for cfg in v3_7_fat_ram v3_7_lfs   \
                       v2_7_fat_ram                        \
                       v4_4_lfs; do
                systemctl enable zephyr-qemu-${cfg}.service
                systemctl restart zephyr-qemu-${cfg}.service
            done
        SHELL

        # ARM mps2_an385 Zephyr beds, each driven over a QEMU `-serial telnet:`
        # bridge (NOT the in-guest NIC — the mps2 LAN9118 wedges on a multi-frame
        # `load_hex` line; see tests/repo3/docs/feasibility.md "pivot to
        # serial-telnet"). Two kinds share this provisioning because they share the
        # serial-telnet transport:
        #   * coverage bases — stock LLEXT shell_loader, one per LLEXT-capable
        #     Zephyr version: `cov` (3.7) + `cov44` (4.4) = the `sprout_cov` /
        #     `sprout_cov44` hosts in the `embedded` lab;
        #   * `no_fs_arm` — stock shell_module, the one Cortex-M *contract* bed
        #     (see the 2026-06-06 scope decision in
        #     docs/superpowers/plans/2026-06-05-embedded-arm-bed-migration.md).
        # otto reaches each via the basil SSH hop, then telnets its port. Unlike
        # the x86 net beds they need no TAP / /30 / SNMP relay — the serial bridge
        # carries the whole console. Adding an instance = one row in the
        # ARM_INSTANCES table (shared by the build + unit steps below); the row's
        # `sample` + `overlay` columns select the firmware (loader vs shell_module).
        #
        # Two builds back the coverage flow, on two machines: the dev VM builds
        # the instrumented *extension* (.llext, loaded at runtime, version-matched
        # to each host's Zephyr version) and runs the cross-gcov report; THIS VM
        # builds + runs the stock *base image* the extension loads into. The split
        # mirrors the x86 beds (this VM runs images; the dev VM builds + reports).
        #
        # ARM_INSTANCES columns: id | zver | zsdk | zephyr-board | build-dir | telnet-addr | port | sample | overlay-config
        # NB: telnet-addr must be a *host* address — not the network/broadcast of a
        # /30 owned by a zeth-* TAP. Those route to the (linkdown) TAP rather than
        # the /32 the unit adds to lo, so TCP connects fail "Network unreachable".
        # The cov /30 is 192.0.2.32/30 (.33/.34 = cov/cov44; .35 = its broadcast),
        # so no_fs_arm uses .37, the first host of the otherwise-free 192.0.2.36/30.
        zephyr.vm.provision "shell", name: "zephyr-qemu-cov-build", privileged: false, keep_color: true, inline: <<-SHELL
            set -e

            # Both coverage images are Cortex-M, so each needs the arm-zephyr-eabi
            # toolchain in its SDK (the x86 configs fetched only x86_64-zephyr-elf).
            # The shared helper's "SDK present, toolchain absent" path layers arm
            # onto each existing SDK (0.16.8 for 3.7, 1.0.1 for 4.4). Subshell so
            # the helper's `set -u` / cwd changes don't leak into this script.
            (
            #{zephyr_sdk_install("arm-zephyr-eabi", ["0.16.8", "1.0.1"])}
            )

            # Build each instance's firmware: the `sample` column picks the Zephyr
            # sample (LLEXT shell_loader for the coverage bases, shell_module for
            # the no_fs contract host) and `overlay` picks its config delta under
            # tests/firmware/zephyr/configs/ — serial shell either way; cov_an385
            # adds MPU-off + large LLEXT buffers, v3_7_no_fs_arm the net-less
            # contract surface. No DT overlay, no SNMP, no networking: the serial
            # bridge carries the console. The Zephyr board name differs by version
            # (HWMv2 renamed it): 3.7 is `mps2_an385`, 4.4 is `mps2/an385`.
            # `-p always` for the same reason the x86 loop uses it (overlay
            # *content* edits don't move cmake args).
            while IFS='|' read -r id zver zsdk board build_dir addr port sample overlay; do
                [ -z "$id" ] && continue
                echo "=== cov base: ${id} (zephyr ${zver}, sdk ${zsdk}, ${board}) ==="
                (
                    # shellcheck disable=SC1091
                    source "${HOME}/zephyr-venv-${zver}/bin/activate"
                    cd "${HOME}/zephyrproject-${zver}"
                    # shellcheck disable=SC1091
                    source zephyr/zephyr-env.sh
                    export ZEPHYR_SDK_INSTALL_DIR="${HOME}/zephyr-sdk-${zsdk}"
                    west build -p always -b "${board}" \
                        "zephyr/${sample}" \
                        -d "${build_dir}" \
                        -- -DEXTRA_CONF_FILE=/vagrant/tests/firmware/zephyr/configs/${overlay}/overlay.conf
                )
            done <<'ARM_INSTANCES'
cov|v3_7|0.16.8|mps2_an385|/home/vagrant/build/cov_base|192.0.2.33|2323|samples/subsys/llext/shell_loader|cov_an385
cov44|v4_4|1.0.1|mps2/an385|/home/vagrant/build/cov_base_v4_4|192.0.2.34|2324|samples/subsys/llext/shell_loader|cov_an385
no_fs_arm|v3_7|0.16.8|mps2_an385|/home/vagrant/build/no_fs_arm|192.0.2.37|2325|samples/subsys/shell/shell_module|v3_7_no_fs_arm
ARM_INSTANCES
        SHELL

        # Root step: write each cov QEMU run-script + systemd unit (the build
        # above runs unprivileged, like zephyr-workspace; unit files need root).
        zephyr.vm.provision "shell", name: "zephyr-qemu-cov-unit", keep_color: true, inline: <<-SHELL

            # apt's qemu-system-arm (8.2.2) is the binary the feasibility gate
            # proved for the serial mps2 instance. The SDK qemu was needed only
            # on x86 (its e1000 emulation differs); the serial bridge uses no
            # NIC, so the system qemu is fine here. Both versions share the QEMU
            # machine name `mps2-an385` (independent of the Zephyr board string).
            QEMU_ARM=$(command -v qemu-system-arm || true)
            if [ -z "$QEMU_ARM" ]; then
                echo "ERROR: qemu-system-arm not found (add it to zephyr-deps)" >&2
                exit 1
            fi

            # QEMU bridges each guest's UART0 to a telnet listener. otto reaches
            # it via the basil hop, then telnets <addr>:<port> (ports use 23xx
            # because 23 is privileged + already taken). Each listen address lives
            # on this VM's loopback (added by ExecStartPre) so the hop's in-VM
            # telnet resolves it; nothing outside this VM needs the address.
            while IFS='|' read -r id zver zsdk board build_dir addr port sample overlay; do
                [ -z "$id" ] && continue
                cat > /home/vagrant/run-zephyr-qemu-${id}.sh <<EOF
#!/usr/bin/env bash
# Launch the ${id} instance (zephyr ${zver}, ${board}) under QEMU, bridging UART
# to a telnet listener on ${addr}:${port}. Serial-telnet (no NIC): the mps2
# LAN9118 can't receive a multi-frame load_hex line, and a serial console needs
# no in-guest networking anyway. See tests/repo3/docs/feasibility.md
# ("pivot to serial-telnet").
set -euo pipefail
exec ${QEMU_ARM} \\
    -machine mps2-an385 \\
    -display none \\
    -monitor none \\
    -serial telnet:${addr}:${port},server,nowait \\
    -kernel ${build_dir}/zephyr/zephyr.elf
EOF
                chown vagrant:vagrant /home/vagrant/run-zephyr-qemu-${id}.sh
                chmod +x /home/vagrant/run-zephyr-qemu-${id}.sh

                cat > /etc/systemd/system/zephyr-qemu-${id}.service <<EOF
[Unit]
Description=Zephyr ${zver} serial-telnet instance (${board}) under QEMU
After=network.target

[Service]
Type=simple
User=vagrant
# Put the telnet listen address on lo so the hop's in-VM telnet can reach it.
ExecStartPre=+/bin/sh -c 'ip addr add ${addr}/32 dev lo 2>/dev/null; true'
ExecStart=/home/vagrant/run-zephyr-qemu-${id}.sh
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
                systemctl enable zephyr-qemu-${id}.service
            done <<'ARM_INSTANCES'
cov|v3_7|0.16.8|mps2_an385|/home/vagrant/build/cov_base|192.0.2.33|2323|samples/subsys/llext/shell_loader|cov_an385
cov44|v4_4|1.0.1|mps2/an385|/home/vagrant/build/cov_base_v4_4|192.0.2.34|2324|samples/subsys/llext/shell_loader|cov_an385
no_fs_arm|v3_7|0.16.8|mps2_an385|/home/vagrant/build/no_fs_arm|192.0.2.37|2325|samples/subsys/shell/shell_module|v3_7_no_fs_arm
ARM_INSTANCES

            systemctl daemon-reload
            # `restart` (not just `enable --now`) so a re-provision picks up a
            # regenerated unit/run-script, matching the x86 loop's behavior.
            for id in cov cov44 no_fs_arm; do
                systemctl restart zephyr-qemu-${id}.service
            done
        SHELL

        # SNMP UDP relay — bridges otto's SNMP manager to each Zephyr
        # instance's agent (UDP/161) over a channel that does NOT contend with
        # the single telnet shell session.
        #
        # Topology note: the dev VM (10.10.200.100) and this zephyr VM
        # (10.10.200.14) share the private /24, but the Zephyr instances live
        # on TAP-side /30s (192.0.2.x) that are reachable ONLY from inside this
        # VM. So a socat relay bound to this VM's private-network address lets
        # otto's pysnmp reach each agent directly over 10.10.200.0/24 — no
        # SSH-UDP tunnelling (SSH forwards TCP only). Each instance gets a
        # distinct relay port on 10.10.200.14; the host's lab data points its
        # `snmp` block at 10.10.200.14:<port>. (A general "UDP over an SSH hop"
        # otto feature — for real targets genuinely behind an SSH-only hop — is
        # tracked in todo/udp_hop_forwarding.md and would retire this relay.)
        #
        # `cfg:zephyr_ip:relay_port` — zephyr_ip is the device side of each
        # /30 (host_ip - 1; see the zephyr-qemu /30 table above).
        zephyr.vm.provision "shell", name: "zephyr-snmp-relay", keep_color: true, inline: <<-SHELL
            for relay_entry in "v3_7_fat_ram:192.0.2.1:16101"   \
                               "v3_7_lfs:192.0.2.5:16102"        \
                               "v2_7_fat_ram:192.0.2.13:16104"   \
                               "v4_4_lfs:192.0.2.29:16108"; do
                cfg=$(echo "$relay_entry" | cut -d: -f1)
                zephyr_ip=$(echo "$relay_entry" | cut -d: -f2)
                relay_port=$(echo "$relay_entry" | cut -d: -f3)

                cat > /etc/systemd/system/zephyr-snmp-relay-${cfg}.service <<EOF
[Unit]
Description=SNMP UDP relay for Zephyr ${cfg} (10.10.200.14:${relay_port} -> ${zephyr_ip}:161)
# The relay only makes sense while the instance is up; pull it along with the
# QEMU unit so a restart of the instance re-establishes the relay's peer.
After=zephyr-qemu-${cfg}.service
Requires=zephyr-qemu-${cfg}.service

[Service]
Type=simple
# UDP4-LISTEN,fork spawns a child per client so concurrent pollers don't
# serialize; -T15 reaps idle children. reuseaddr survives quick restarts.
ExecStart=/usr/bin/socat -T15 UDP4-LISTEN:${relay_port},bind=10.10.200.14,fork,reuseaddr UDP4:${zephyr_ip}:161
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
            done

            systemctl daemon-reload
            for cfg in v3_7_fat_ram v3_7_lfs   \
                       v2_7_fat_ram                        \
                       v4_4_lfs; do
                systemctl enable zephyr-snmp-relay-${cfg}.service
                systemctl restart zephyr-snmp-relay-${cfg}.service
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

    # Install Docker engine + compose v2 so otto's docker container hosts can
    # use this VM as their parent. All three Unix test VMs (carrot/tomato/pepper)
    # get docker, so the docker e2e suite can lease whichever is free and run
    # against its own daemon — spreading the docker chain off a single host.
    # A host advertises itself as a docker parent via `docker_capable: true` in
    # tests/_fixtures/lab_data/tech1/hosts.json (flipped on for carrot+tomato
    # alongside the docker-e2e pooling test work).
    def provision_docker(vm, name)
        vm.vm.provision "shell", name: "#{name} docker", keep_color: true, inline: <<-SHELL

            # Install docker engine + compose v2 plugin from Ubuntu's repos.
            # `docker-compose-v2` provides `docker compose` (v2 plugin) which
            # is the spelling otto uses; the legacy `docker-compose` binary
            # is intentionally not installed.
            apt -y install  docker.io                \
                            docker-compose-v2

            # Let the `vagrant` user (the credential otto authenticates as)
            # talk to the docker socket without sudo. Otto authenticates as
            # `vagrant` per tests/_fixtures/lab_data/tech1/hosts.json.
            usermod -aG docker vagrant

            systemctl enable --now docker
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
