#!/usr/bin/env bash
# Build the live tree (build/) and a stale second tree of the same project
# (build-old/, compiled with -DSTALE=ON so its code and stamps differ). Only
# build/'s artifacts are deployed; build-old/ is there to prove it changes
# nothing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmake -S "$HERE" -B "$HERE/build" -DSTALE=OFF >/dev/null
cmake --build "$HERE/build" >/dev/null
cmake -S "$HERE" -B "$HERE/build-old" -DSTALE=ON >/dev/null
cmake --build "$HERE/build-old" >/dev/null
