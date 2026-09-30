#!/usr/bin/env bash
# Provision the kernel set `make kmodcov`'s build columns compile against.
#
#   scripts/provision_kmodcov_kernels.sh [<id>...]   # default: every id below
#   scripts/provision_kmodcov_kernels.sh --list      # the pin table, no network, no docker
#
# For each kernel, idempotently, under ${OTTO_KMODCOV_KERNELS_DIR:-/home/vagrant/build/kmodcov-kernels}/<id>/:
#   1. fetch its files into _downloads/ and verify each against the sha256 pinned here;
#   2. build the column's image, otto-kmodcov-kernel:<id>, from a base image pinned by
#      digest -- a container of the kernel's OWN Ubuntu release with that release's gcc,
#      make and kmod, because a kernel tree is coupled to the compilers of its era
#      (docs/cli/cov/instrumenting/kernel-modules.md, "Another kernel, ISA or compiler");
#   3. prepare the tree: `dpkg -x` for a headers pair; for the 2.6.32 source tree
#      `make CC=gcc-4.7 HOSTCC=gcc-4.7 defconfig modules_prepare` INSIDE its image, so its
#      host tools and its configuration come from the compiler that will build against it.
# A prepared tree carries a stamp, `.prepared-<tree>` beside it: `<release> <recipe> <tree_hash>`
# (the tree's release, the recipe hash of the image it was prepared under, and the sha256 of
# tree_pins_for's lines), written only once, atomically, after include/config/kernel.release and
# scripts/mod/modpost both exist (that first file alone lands early on both paths and cannot
# prove the tree is finished). A tree whose stamp is missing, stale (its recipe or tree_hash
# field no longer matches the id's current values) or no longer backed by those two files is
# cleared and redone -- the OLD stamp removed FIRST, before any rm -rf, because the redo's own
# first step recreates the very two files the post-condition reads (the -generic deb; 2.6.32's
# modules_prepare reaching modpost), so an interruption partway through the rest (the _all deb's
# 15-17k entries; modules_prepare's tail) must never be read back as a false "prepared". An
# image carries the sha256 of its recipe -- the Dockerfile text plus context_pins_for's lines
# (today: 2.6.32's nine debs; empty for headers columns) -- as the label otto.kmodcov.recipe, and
# is rebuilt when that label is missing or differs; a headers column's own deb re-pin (that's
# tree_pins_for's, not context_pins_for's) does not touch its recipe, since those debs go into
# the TREE, never the image. A download whose sha256 matches is not fetched again. A file that
# fails its sha256 is deleted and reported: a pin that rots is a failure to re-pin, never a skip.
# Ubuntu's pool (ports.ubuntu.com, the toolchain PPA) drops a build once a newer one supersedes
# it, on no fixed schedule of its own; a fetch that 404s means the same thing and the same
# remedy -- re-pin here, never skip.
#
# 2.6.32 builds in Ubuntu 12.04 (amd64, under the VM's qemu-x86_64 binfmt) with gcc 4.7.3
# from Ubuntu's toolchain team's build of it for that release (the ubuntu-toolchain-r/test
# PPA, nine pinned debs): 12.04's own archive carries gcc 4.4 to 4.6 and only 4.7's base
# package, 12.10/13.04 exist on Docker Hub only as schema-1 manifests docker refuses, and
# Debian wheezy's binaries segfault under qemu-user (vsyscall). The x86_64-cross column is
# not provisioned here: it is the 6.8 source tree at KMODCOV_CROSS_KDIR, prepared by hand as
# the docs describe.
set -euo pipefail

ROOT="${OTTO_KMODCOV_KERNELS_DIR:-/home/vagrant/build/kmodcov-kernels}"
IMAGE_PREFIX="otto-kmodcov-kernel"
# 2.6.32 builds amd64 under qemu-user emulation; overridable so a unit test can point this at a
# file it controls instead of the real binfmt_misc mount.
PROVISION_BINFMT_CHECK="${OTTO_KMODCOV_BINFMT_CHECK:-/proc/sys/fs/binfmt_misc/qemu-x86_64}"
KERNEL_ORG="https://cdn.kernel.org/pub/linux/kernel/v2.6/longterm/v2.6.32"
PORTS="http://ports.ubuntu.com/pool/main/l/linux"
PPA="https://ppa.launchpadcontent.net/ubuntu-toolchain-r/test/ubuntu/pool/main/g"
OLD_RELEASES="old-releases.ubuntu.com"

# The pin table: id | docker platform | base image by digest | CC | tree relative to <id>/.
# The digests are the RepoDigests docker recorded for the tags on 2026-09-28
# (ubuntu:12.04 and arm64v8/ubuntu:{14.04,16.04,20.04,22.04,25.10}).
COLUMNS=(
  "2.6.32|linux/amd64|ubuntu@sha256:18305429afa14ea462f810146ba44d4363ae76e4c8dfc38288cf73aa07485005|gcc-4.7|src/linux-2.6.32.71"
  "3.13|linux/arm64|arm64v8/ubuntu@sha256:5ed16aa332467821529d451800e6fe599d83e30471e91b096752f8696d9bf6e9|gcc|root/usr/src/linux-headers-3.13.0-170-generic"
  "4.4|linux/arm64|arm64v8/ubuntu@sha256:f4c51ba054967fd4b06715f1b67078efbe9ca152e8be98d8f3c1f4d08c6042f8|gcc|root/usr/src/linux-headers-4.4.0-210-generic"
  "5.4|linux/arm64|arm64v8/ubuntu@sha256:0908a765aeb02fe4e564b543eaed1d77839b7a08de94041e24328b2cb62ac553|gcc|root/usr/src/linux-headers-5.4.0-218-generic"
  "5.15|linux/arm64|arm64v8/ubuntu@sha256:a0c17bf45e5df8a1434cc5501c157fa175def4b86b6db39d97182a403e43d97c|gcc|root/usr/src/linux-headers-5.15.0-198-generic"
  "6.17|linux/arm64|arm64v8/ubuntu@sha256:b04591b69a7431ee0349621b300793b2dbc996d59ccf73bbd227903359790c3c|gcc|root/usr/src/linux-headers-6.17.0-41-generic"
)

# Downloads per id: "<url> <sha256>" lines. Headers columns: the -generic deb, then the _all deb.
downloads_for() {
  case "$1" in
    2.6.32) cat <<EOF
$KERNEL_ORG/linux-2.6.32.71.tar.xz 60a5ffe0206a0ea8997c29ccc595aa7dae55f6cb20c7d92aab88029ca4fef598
$PPA/gcc-4.7/gcc-4.7_4.7.3-2ubuntu1~12.04_amd64.deb c5d4886208a4c971e825141dfe873965ea380680477f3268a21d394ecd7b29b3
$PPA/gcc-4.7/cpp-4.7_4.7.3-2ubuntu1~12.04_amd64.deb b2439897dec6065e6a8330b7e6b1effccf11c6f58b747bb63488fa63de27f493
$PPA/gcc-4.7/gcc-4.7-base_4.7.3-2ubuntu1~12.04_amd64.deb cd09e0dc10a938fdede78cb3df96ed9e6737f7b9a11a3655a5cf6b1878162133
$PPA/gcc-4.7/libgcc-4.7-dev_4.7.3-2ubuntu1~12.04_amd64.deb 8337398c70cfbb8ee596a6da304ddb49a5ae2c8e6727e90a07e1bab78490efc1
$PPA/gcc-9/gcc-9-base_9.3.0-10ubuntu2~12.04_amd64.deb f1e4e760de0484c3c53f4d73a83e6831a8f6b663d39a4c333eb1f467ea34d36f
$PPA/gcc-9/libgcc1_9.3.0-10ubuntu2~12.04_amd64.deb a955cb763f040e317e5cdde16c18d87ce35a285393b12758a3f4746cd30056dd
$PPA/gcc-9/libgomp1_9.3.0-10ubuntu2~12.04_amd64.deb d0ab4a3dfb2f33d9255b6c960069b8c344b10ef54f73412a2c7efd1912e2afc7
$PPA/gcc-9/libitm1_9.3.0-10ubuntu2~12.04_amd64.deb 0015996dfc2a8abae6ca0384a75c7817d2bee612f5b1f3b6bb994a3444c401fd
$PPA/gcc-9/libquadmath0_9.3.0-10ubuntu2~12.04_amd64.deb e34758990eae5688858d9262f34502081c092e0e23407d7dc399e2a783a7fab6
EOF
    ;;
    3.13) cat <<EOF
$PORTS/linux-headers-3.13.0-170-generic_3.13.0-170.220_arm64.deb 8043465263c948da7bed82c541720e295fd0f586d9957b595f1f7ac3c48055d1
$PORTS/linux-headers-3.13.0-170_3.13.0-170.220_all.deb 32d8b5f1b41b0b15c56f98511f3e8ea2f5c510a1cd093d8bf95fe57a17ae7717
EOF
    ;;
    4.4) cat <<EOF
$PORTS/linux-headers-4.4.0-210-generic_4.4.0-210.242_arm64.deb 58dfb40f7bfef072f7bc8ced29657709a83cbc244d52b4d03a367d0bc615d543
$PORTS/linux-headers-4.4.0-210_4.4.0-210.242_all.deb 813c33c988531545ef3762fee6b472df8c9d38b74d3f969102e7679cd684bd2f
EOF
    ;;
    5.4) cat <<EOF
$PORTS/linux-headers-5.4.0-218-generic_5.4.0-218.238_arm64.deb ecba222861fcc1fd5d90b8cd4349fa116064a6da68e4c9ecaeec89d360d0ed16
$PORTS/linux-headers-5.4.0-218_5.4.0-218.238_all.deb e67d9b22a425c2519536813908bd5c2cf80aa609c83f17d5c43875cf01549838
EOF
    ;;
    5.15) cat <<EOF
$PORTS/linux-headers-5.15.0-198-generic_5.15.0-198.208_arm64.deb 1cce5a97c3ef98694e6ffe4790636b4994aa3d17f35212a8b21f091de9c87720
$PORTS/linux-headers-5.15.0-198_5.15.0-198.208_all.deb 7ad14fd344c5fbc8b7b250747ac700a3e6be08937f7a5d38a0a6c7b8d4c00287
EOF
    ;;
    6.17) cat <<EOF
$PORTS/linux-headers-6.17.0-41-generic_6.17.0-41.41_arm64.deb 740b75c38e80eea384d070795970939b3fe441fd300f980ae7b186279b6c35e8
$PORTS/linux-headers-6.17.0-41_6.17.0-41.41_all.deb f94c2e09446c652f481e4f68ceab39cf486e4b41de3ad0683969521050719f10
EOF
    ;;
  esac
}

context_pins_for() { # <id>: the downloads_for lines copied into the id's build context (today:
                      # 2.6.32's nine .deb lines; empty for headers columns, whose debs feed the
                      # TREE, never the image, so their own re-pin must not touch the recipe)
  local id=$1
  if [ "$id" = "2.6.32" ]; then downloads_for "$id" | grep '\.deb '; fi
}

tree_pins_for() { # <id>: the downloads_for lines the tree is made from -- every line
                   # context_pins_for does not claim (the headers pair; 2.6.32's tarball).
                   # Every downloads_for line belongs to exactly one of the two (provision()
                   # asserts it).
  local id=$1 context
  context=$(context_pins_for "$id")
  if [ -z "$context" ]; then downloads_for "$id"; return; fi
  grep -vFxf <(printf '%s\n' "$context") <(downloads_for "$id")
}

all_ids() { local c; for c in "${COLUMNS[@]}"; do printf '%s\n' "${c%%|*}"; done; }

column_field() { # <id> <n>: the n-th |-separated field of the id's row; pure bash, like --list
  local c f
  for c in "${COLUMNS[@]}"; do
    IFS='|' read -r -a f <<<"$c"
    if [ "${f[0]}" = "$1" ]; then printf '%s\n' "${f[$(($2 - 1))]}"; return; fi
  done
  return 1
}

usage_text() { printf 'usage: %s [--list] [--help] [<id>...]   ids: %s\n' "$0" "$(all_ids | tr '\n' ' ')"; }
usage() { usage_text >&2; exit 2; }

list_table() { # pure bash: no external command, so a machine without docker can read it
  local c id cc tree
  for c in "${COLUMNS[@]}"; do
    IFS='|' read -r id _ _ cc tree <<<"$c"
    printf '%s %s:%s %s %s\n' "$id" "$IMAGE_PREFIX" "$id" "$cc" "$tree"
  done
}

require() { # <tool> <package>
  command -v "$1" >/dev/null 2>&1 || { echo "$1 is not on PATH; apt install $2 (needed by $0)" >&2; exit 1; }
}

fetch() { # <dir> <url> <sha256>: download once, verify always
  local dir=$1 url=$2 sum=$3 file
  file="$dir/$(basename "$url")"
  mkdir -p "$dir"
  if [ -f "$file" ] && printf '%s  %s\n' "$sum" "$file" | sha256sum -c --quiet - 2>/dev/null; then
    return
  fi
  echo "fetching $url"
  curl -fsSL --retry 3 -o "$file.part" "$url" \
    || { rm -f "$file.part"; echo "$(basename "$url"): could not fetch $url -- a pin has rotted: Ubuntu's pool drops a kernel deb once a newer one supersedes it (every published build stays under https://launchpad.net/ubuntu/+source/linux), and a PPA or kernel.org file can move; re-pin in $0, never skip" >&2; exit 1; }
  if ! printf '%s  %s\n' "$sum" "$file.part" | sha256sum -c --quiet -; then
    rm -f "$file.part"
    echo "$(basename "$url"): sha256 does not match the pin in $0 -- re-pin, never skip" >&2
    exit 1
  fi
  mv "$file.part" "$file"
}

dockerfile_for() { # <id> <base image>: the image recipe on stdout
  case "$1" in
    2.6.32) cat <<EOF
FROM $2
RUN sed -i 's|archive.ubuntu.com|$OLD_RELEASES|g; s|security.ubuntu.com|$OLD_RELEASES|g' /etc/apt/sources.list \\
 && apt-get update \\
 && apt-get install -y --no-install-recommends make module-init-tools libc6-dev binutils libgmp10 libmpc2 libmpfr4 zlib1g \\
 && rm -rf /var/lib/apt/lists/*
COPY *.deb /tmp/debs/
RUN dpkg -i /tmp/debs/*.deb && rm -rf /tmp/debs
EOF
    ;;
    # libdw1: kbuild from 6.14 on links modules through gendwarfksyms, which needs libdw.so.1.
    *) cat <<EOF
FROM $2
RUN apt-get update \\
 && apt-get install -y --no-install-recommends gcc make kmod libc6-dev libdw1 \\
 && rm -rf /var/lib/apt/lists/*
EOF
    ;;
  esac
}

recipe_for() { # <id>: sha256 of the Dockerfile text plus context_pins_for's lines -- so a
                # headers column's own deb re-pin (tree_pins_for's, not context_pins_for's)
                # leaves its image recipe, and thus its image, alone.
  local id=$1 base result
  base=$(column_field "$id" 3)
  result=$( { dockerfile_for "$id" "$base"; context_pins_for "$id"; } | sha256sum )
  printf '%s\n' "${result%% *}"
}

build_image() { # <id> <recipe>
  local id=$1 recipe=$2 platform base image ctx current url old_id new_id rmi_err
  platform=$(column_field "$id" 2); base=$(column_field "$id" 3); image="$IMAGE_PREFIX:$id"
  # The recipe (computed once in provision() and shared with prepare_tree, so the image and the
  # stamp that names it always agree) is compared against the otto.kmodcov.recipe label the image
  # was built with, never just the tag's presence, so a changed pin rebuilds instead of reading
  # as done.
  if current=$(docker image inspect --format '{{index .Config.Labels "otto.kmodcov.recipe"}}' "$image" 2>/dev/null) \
      && [ "$current" = "$recipe" ]; then
    echo "$image: present"
    return
  fi
  old_id=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null) || old_id=
  if [ -n "$old_id" ]; then
    echo "rebuilding $image (recipe changed)"
  else
    echo "building $image from $base ($platform)"
  fi
  ctx="$ROOT/$id/image"; rm -rf "$ctx"; mkdir -p "$ctx"
  dockerfile_for "$id" "$base" > "$ctx/Dockerfile"
  while read -r url _; do cp "$ROOT/$id/_downloads/$(basename "$url")" "$ctx/"; done < <(context_pins_for "$id")
  docker build --platform "$platform" --label "otto.kmodcov.recipe=$recipe" -t "$image" "$ctx" > "$ROOT/$id/image-build.log" 2>&1 \
    || { tail -30 "$ROOT/$id/image-build.log" >&2; echo "$image: docker build failed (log: $ROOT/$id/image-build.log)" >&2; exit 1; }
  # A rebuild retires precisely the id it replaced -- never `docker image prune`, which would
  # also sweep other sessions' dangling images that are not ours to remove.
  if [ -n "$old_id" ]; then
    new_id=$(docker image inspect --format '{{.Id}}' "$image")
    if [ "$new_id" != "$old_id" ]; then
      rmi_err=$(docker rmi "$old_id" 2>&1) \
        || echo "warning: could not remove the previous $image image ($old_id): $rmi_err" >&2
    fi
  fi
}

prepare_tree() { # <id> <recipe>
  local id=$1 recipe=$2 dir tree stamp platform image url downloads tarball release
  local stamped_recipe stamped_tree_hash tree_hash valid
  dir="$ROOT/$id"; tree="$dir/$(column_field "$id" 5)"
  stamp="$dir/.prepared-$(basename "$tree")"
  tree_hash=$(tree_pins_for "$id" | sha256sum); tree_hash=${tree_hash%% *}
  # The stamp (`<release> <recipe> <tree_hash>`, written atomically) is the idempotency proof,
  # and it counts only while everything it asserts still holds: its own content non-empty, both
  # include/config/kernel.release and scripts/mod/modpost still present (the stamp lives beside
  # root/ or src/, so it outlives either being deleted to free space), its recipe field matching
  # the id's CURRENT recipe (so the tree redoes under the compiler it now pairs with), AND its
  # tree_hash field (the sha256 of tree_pins_for's lines) matching the id's CURRENT tree_hash (so
  # a re-pinned headers deb or tarball -- same path, new sha -- redoes the tree instead of
  # leaving the old extraction in place). kernel.release alone lands early on both paths and
  # cannot prove the tree is finished. Anything else -- missing, stale, or no longer backed by
  # those files -- is cleared and redone, stamp removed FIRST (before any rm -rf): the redo's
  # first step (the -generic deb; 2.6.32's modules_prepare reaching modpost) recreates the very
  # two files the post-condition reads, so an interruption partway through the rest of the redo
  # (the _all deb's 15-17k entries; modules_prepare's tail) must never be read back as done.
  valid=0
  if [ -s "$stamp" ] && [ -f "$tree/include/config/kernel.release" ] && [ -f "$tree/scripts/mod/modpost" ]; then
    read -r release stamped_recipe stamped_tree_hash < "$stamp" || true
    if [ "$stamped_recipe" = "$recipe" ] && [ "$stamped_tree_hash" = "$tree_hash" ]; then valid=1; fi
  fi
  if [ "$valid" = 1 ]; then echo "$tree: prepared ($release)"; return; fi
  echo "$tree: no valid stamp; clearing and redoing"
  rm -f "$stamp"
  if [ "$id" = "2.6.32" ]; then
    rm -rf "$dir/src"
    downloads=$(tree_pins_for "$id")
    tarball=${downloads%%$'\n'*}; tarball=${tarball%% *}
    mkdir -p "$dir/src"
    tar -xJf "$dir/_downloads/$(basename "$tarball")" -C "$dir/src"
    platform=$(column_field "$id" 2); image="$IMAGE_PREFIX:$id"
    echo "preparing $tree inside $image (emulated; about a minute)"
    docker run --rm --platform "$platform" --user "$(id -u):$(id -g)" -v "$dir/src:$dir/src" -w "$tree" "$image" \
      make CC=gcc-4.7 HOSTCC=gcc-4.7 defconfig modules_prepare > "$dir/prepare.log" 2>&1 \
      || { tail -30 "$dir/prepare.log" >&2; echo "$tree: modules_prepare failed (log: $dir/prepare.log)" >&2; exit 1; }
  else
    rm -rf "$dir/root"
    mkdir -p "$dir/root"
    while read -r url _; do dpkg -x "$dir/_downloads/$(basename "$url")" "$dir/root"; done < <(tree_pins_for "$id")
  fi
  [ -f "$tree/include/config/kernel.release" ] || { echo "$tree: no include/config/kernel.release after preparation" >&2; exit 1; }
  [ -f "$tree/scripts/mod/modpost" ] || { echo "$tree: no scripts/mod/modpost after preparation" >&2; exit 1; }
  release=$(cat "$tree/include/config/kernel.release")
  printf '%s %s %s\n' "$release" "$recipe" "$tree_hash" > "$stamp.part"
  mv "$stamp.part" "$stamp"
  echo "$tree: prepared ($release)"
}

provision() { # <id>
  local id=$1 url sum recipe total ctx_n tree_n
  echo "== $id"
  while read -r url sum; do fetch "$ROOT/$id/_downloads" "$url" "$sum"; done < <(downloads_for "$id")
  # Every downloads_for line must belong to exactly one of context_pins_for/tree_pins_for
  # (recipe_for and build_image trust the first; prepare_tree trusts the second): a line that
  # belonged to neither, or to both, would silently drop a file from the image or the tree, or
  # double-count it in the recipe hash.
  total=$(downloads_for "$id" | wc -l); ctx_n=$(context_pins_for "$id" | wc -l); tree_n=$(tree_pins_for "$id" | wc -l)
  [ "$((ctx_n + tree_n))" -eq "$total" ] \
    || { echo "$id: context_pins_for ($ctx_n) + tree_pins_for ($tree_n) != downloads_for ($total) -- every pin line must belong to exactly one" >&2; exit 1; }
  recipe=$(recipe_for "$id")
  build_image "$id" "$recipe"
  prepare_tree "$id" "$recipe"
}

main() {
  local ids=() arg
  for arg in "$@"; do
    case "$arg" in
      --list) list_table; exit 0 ;;
      --help) usage_text; exit 0 ;;
      -*) usage ;;
      *) ids+=("$arg") ;;
    esac
  done
  [ ${#ids[@]} -gt 0 ] || mapfile -t ids < <(all_ids)
  for arg in "${ids[@]}"; do
    column_field "$arg" 1 >/dev/null || { echo "$arg is not a kernel id; the set is: $(all_ids | tr '\n' ' ')" >&2; exit 1; }
  done
  for arg in "${ids[@]}"; do
    if [ "$arg" = "2.6.32" ]; then
      [ -e "$PROVISION_BINFMT_CHECK" ] \
        || { echo "2.6.32 builds amd64 under emulation: install qemu-user-static and binfmt-support (no $PROVISION_BINFMT_CHECK)" >&2; exit 1; }
      break
    fi
  done
  require docker docker.io
  require curl curl
  require sha256sum coreutils
  require dpkg dpkg
  require tar tar
  require xz xz-utils
  for arg in "${ids[@]}"; do provision "$arg"; done
  echo "provisioned under $ROOT: ${ids[*]}"
}

main "$@"
