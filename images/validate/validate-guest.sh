#!/bin/bash
set -euo pipefail

directory="$1"
[[ "$(id -u)" == 1001 && "$(id -un)" == runner ]]
/usr/bin/cmake -S "$directory/cobalt" -B "$directory/build" -G Ninja \
    -DCMAKE_C_COMPILER=/usr/bin/gcc-13 -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
    -DXENOMAI_ROOT=/usr/xenomai
/usr/bin/cmake --build "$directory/build"
timeout 20s "$directory/build/cobalt"
