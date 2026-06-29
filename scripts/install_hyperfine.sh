#!/usr/bin/env bash
# Install a pinned hyperfine binary into a target bin dir. Usage:
#   scripts/install_hyperfine.sh <version> <bin_dir>
set -euo pipefail
VERSION="${1:?version required}"
BIN_DIR="${2:?bin dir required}"

os="$(uname -s)"; arch="$(uname -m)"
case "$os-$arch" in
  Linux-x86_64)  asset="x86_64-unknown-linux-musl" ;;
  Linux-aarch64) asset="aarch64-unknown-linux-gnu" ;;
  Darwin-x86_64) asset="x86_64-apple-darwin" ;;
  Darwin-arm64)  asset="aarch64-apple-darwin" ;;
  *) echo "unsupported platform: $os-$arch" >&2; exit 1 ;;
esac

# Pinned sha256 per asset for hyperfine v1.20.0.
# Computed by downloading each tarball and running sha256sum.
declare -A SHA256=(
  ["x86_64-unknown-linux-musl"]="3285ec7959285288137043dd81dce0dde056227018a8277532d9a364b4f03c2b"
  ["aarch64-unknown-linux-gnu"]="90875cb1db7a1d797c311174d061728361e58fc70e3b62262a00635ac3b1997c"
  ["x86_64-apple-darwin"]="f58d0b90993fadfa122a351428c469ce24afef3865f027f0e6e86f0830d088f1"
  ["aarch64-apple-darwin"]="8ee7067016620447c9d2d6234ec9a4680f958b7ad983549b56334668f63075b5"
)

tarball="hyperfine-v${VERSION}-${asset}.tar.gz"
url="https://github.com/sharkdp/hyperfine/releases/download/v${VERSION}/${tarball}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
echo "Downloading hyperfine v${VERSION} (${asset})..."
curl -fsSL --max-time 120 -o "$tmp/$tarball" "$url"
echo "${SHA256[$asset]}  $tmp/$tarball" | sha256sum -c -
tar xzf "$tmp/$tarball" -C "$tmp"
mkdir -p "$BIN_DIR"
install -m 0755 "$tmp"/hyperfine-*/hyperfine "$BIN_DIR/hyperfine"
"$BIN_DIR/hyperfine" --version
