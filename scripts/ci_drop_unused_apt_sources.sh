#!/usr/bin/env bash
# Drop the third-party apt sources the GitHub runner image ships but that otto's
# CI never needs, so `playwright install --with-deps` — which shells out to
# `apt-get update` as root — only talks to the Ubuntu archive.
#
# Why (issue #129): `apt-get update` refreshes EVERY configured source and fails
# the whole invocation if ANY one of them is inconsistent. The ubuntu-latest
# image preconfigures Google's Chrome repo, and on 2026-07-11 that repo's CDN
# edge was caught mid-mirror-sync — its Release file declared Packages.gz as
# 1215 bytes while the edge served 1216:
#
#   Err:32 https://dl.google.com/linux/chrome-stable/deb stable/main amd64 Packages
#     File has unexpected size (1216 != 1215). Mirror sync in progress?
#   E: Some index files failed to download.
#
# apt exits 100, playwright reports "Failed to install browsers", and a job that
# had not yet run a single test goes red. The webkit leg lost that race by ~2s;
# firefox and chromium hit the same URL seconds later, after the sync settled,
# and passed — so the red leg was decided by scheduling jitter, not by anything
# browser-specific.
#
# Google's repo is dead weight for us, and so are Microsoft's: the engines we
# install (chromium, firefox, webkit) are playwright's own builds, fetched from
# cdn.playwright.dev, and every system library `--with-deps` pulls (libwoff1,
# libsoup-3.0-0, libgstreamer*, …) comes from the Ubuntu archive. Checked
# against the run that failed: of the packages apt actually installed, 31/31
# came from azure.archive.ubuntu.com and 0 from either third party — they are
# touched ONLY to refresh indexes we never install a byte from. (Google's repo
# is read solely by `playwright install chrome`, the branded-channel path otto
# does not use.) Dropping them is a strict reduction in failure surface.
#
# Deliberately NOT a retry loop (cf. web-install's npm ci retry in the Makefile,
# issue #107): a retry would also paper over a genuine apt breakage. Here we can
# delete the exposure outright, so we do. A transient failure on the Ubuntu
# archive itself stays possible, but has not bitten in 3 months of CI.
set -euo pipefail

# This rm targets system paths. Refuse to run anywhere but a CI runner so a
# stray local invocation can't uninstall a developer's Chrome apt source.
if [[ "${CI:-}" != "true" ]]; then
  echo "ci_drop_unused_apt_sources: refusing to touch apt sources outside CI (CI != 'true')" >&2
  exit 1
fi

# Match on the host each source points AT, not on its filename: the runner image
# ships these as one-line `.list` today but is migrating to deb822 `.sources`,
# and either layout (or a rename) would silently defeat a hardcoded path — the
# script would still exit 0 while quietly protecting nothing. Only
# sources.list.d/ is scanned; /etc/apt/sources.list is the Ubuntu archive (via
# the image's apt-mirrors.txt mirrorlist) and must stay.
unused_hosts=(dl.google.com packages.microsoft.com)

for host in "${unused_hosts[@]}"; do
  while IFS= read -r src; do
    echo "ci_drop_unused_apt_sources: removing ${src} (points at ${host})"
    sudo rm -f "${src}"
  done < <(grep -rlsF "${host}" /etc/apt/sources.list.d/ || true)
done

# Leave a record of what apt will actually hit, so the next mirror failure names
# a source that is visible in this job's log.
echo "ci_drop_unused_apt_sources: remaining apt sources:"
grep -rhs -E '^(deb |URIs:)' /etc/apt/sources.list /etc/apt/sources.list.d/ || true
