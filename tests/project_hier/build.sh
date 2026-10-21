#!/usr/bin/env bash
# Build the live tree (build/) and a stale second tree of the same project
# (build-old/, compiled with -DSTALE=ON so its code and stamps differ). Only
# build/'s artifacts are deployed; build-old/ is there to prove it changes
# nothing. The unit tests are gtest: the build machine needs libgtest-dev.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmake -S "$HERE" -B "$HERE/build" -DSTALE=OFF >/dev/null
cmake --build "$HERE/build" >/dev/null
# Run the unit tests once, from cleared counters, so build/tests/unit holds the
# unit tier's .gcda with exactly one run's counts.
find "$HERE/build/tests/unit" -name '*.gcda' -delete
"$HERE/build/tests/unit/unit_tests" >/dev/null
cmake -S "$HERE" -B "$HERE/build-old" -DSTALE=ON >/dev/null
cmake --build "$HERE/build-old" >/dev/null
