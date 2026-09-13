#!/bin/bash
set -euo pipefail
: "${EXPECTED_KERNEL_RELEASE:?}" "${EXPECTED_RECIPE_ID:?}" "${BUILD_ID:?}" "${SMOKE_STAGE:?}"
report_dir="${RUNNER_TEMP:?}/ami-example-smoke"
mkdir -p "$report_dir"
args=(--release "$EXPECTED_KERNEL_RELEASE" --recipe "$EXPECTED_RECIPE_ID" --build-id "$BUILD_ID" --stage "$SMOKE_STAGE")
case "${1:-}" in
  identity)
    ami-example-guest-report "${args[@]}" --output "$report_dir/identity.json"
    ;;
  test)
    ami-example-guest-report "${args[@]}" --environment --output "$report_dir/environment.json"
    /usr/bin/cmake -S "${GITHUB_WORKSPACE:?}/tests/cobalt" -B "$report_dir/build" -G Ninja -DCMAKE_C_COMPILER=/usr/bin/gcc-13 -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja -DXENOMAI_ROOT="${XENOMAI_ROOT:?}" 2>&1 | tee "$report_dir/configure.log"
    /usr/bin/cmake --build "$report_dir/build" 2>&1 | tee "$report_dir/build.log"
    /usr/bin/ctest --test-dir "$report_dir/build" --output-on-failure --output-junit "$report_dir/ctest.xml" 2>&1 | tee "$report_dir/ctest.log"
    ;;
  *) echo 'usage: ami-example-smoke identity|test' >&2; exit 2 ;;
esac
