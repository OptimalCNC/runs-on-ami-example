#!/bin/bash
set -euo pipefail

release="$1" xenomai_version="$2" directory="$3"
[[ "$(id -u)" == 1001 && "$(id -un)" == runner ]]
[[ "$(uname -r)" == "$release" ]]
[[ "$(/usr/xenomai/bin/xeno-config --version)" == "$xenomai_version" ]]
/usr/bin/cmake -S "$directory/cobalt" -B "$directory/build" -G Ninja \
    -DCMAKE_C_COMPILER=/usr/bin/gcc-13 -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
    -DXENOMAI_ROOT=/usr/xenomai
/usr/bin/cmake --build "$directory/build"
timeout 20s "$directory/build/cobalt"
