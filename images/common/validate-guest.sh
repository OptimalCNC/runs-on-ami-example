#!/bin/bash
set -euo pipefail

release="$1" recipe="$2" directory="$3"
[[ "$(id -u)" == 1001 && "$(id -un)" == runner ]]
mkdir -p "$directory/reports"
eval "$(/usr/local/bin/runner-image-env --format shell)"
/usr/local/bin/ami-example-guest-report --platform vm --release "$release" \
    --recipe "$recipe" --environment --output "$directory/reports/guest-report.json"
/usr/bin/cmake -S "$directory/cobalt" -B "$directory/build" -G Ninja \
    -DCMAKE_C_COMPILER=/usr/bin/gcc-13 -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
    -DXENOMAI_ROOT="$XENOMAI_ROOT" 2>&1 | tee "$directory/reports/configure.log"
/usr/bin/cmake --build "$directory/build" 2>&1 | tee "$directory/reports/build.log"
/usr/bin/ctest --test-dir "$directory/build" --output-on-failure \
    --output-junit "$directory/reports/ctest.xml" 2>&1 | tee "$directory/reports/ctest.log"
