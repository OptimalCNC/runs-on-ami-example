#!/bin/bash
set -euo pipefail

usage() {
    echo 'usage: ami-example-smoke identity|test --release RELEASE --recipe RECIPE --build-id ID --stage a|b --output DIR [--source DIR] [--xenomai-root DIR]'
}

fail() {
    echo "$1" >&2
    usage >&2
    exit 2
}

command="${1:-}"
case "$command" in
    identity|test) shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail 'expected identity or test command' ;;
esac
release='' recipe='' build_id='' stage='' report_dir='' source_dir=''
xenomai_root='/usr/xenomai'
while (( $# )); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --release|--recipe|--build-id|--stage|--output|--source|--xenomai-root)
            (( $# >= 2 )) || fail "missing value for $1"
            [[ -n "$2" ]] || fail "empty value for $1"
            case "$1" in
                --release) release="$2" ;;
                --recipe) recipe="$2" ;;
                --build-id) build_id="$2" ;;
                --stage) stage="$2" ;;
                --output) report_dir="$2" ;;
                --source) source_dir="$2" ;;
                --xenomai-root) xenomai_root="$2" ;;
            esac
            shift 2
            ;;
        *) fail "unknown argument: $1" ;;
    esac
done
[[ -n "$release" && -n "$recipe" && -n "$build_id" && -n "$stage" && -n "$report_dir" ]] || fail 'release, recipe, build ID, stage, and output are required'
[[ "$stage" == a || "$stage" == b ]] || fail 'stage must be a or b'
if [[ "$command" == test && -z "$source_dir" ]]; then
    fail 'test requires --source pointing to the CMake source directory'
fi

mkdir -p "$report_dir"
args=(--release "$release" --recipe "$recipe" --build-id "$build_id" --stage "$stage")
case "$command" in
  identity)
    ami-example-guest-report "${args[@]}" --output "$report_dir/identity.json"
    ;;
  test)
    ami-example-guest-report "${args[@]}" --environment --output "$report_dir/environment.json"
    /usr/bin/cmake -S "$source_dir" -B "$report_dir/build" -G Ninja -DCMAKE_C_COMPILER=/usr/bin/gcc-13 -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja -DXENOMAI_ROOT="$xenomai_root" 2>&1 | tee "$report_dir/configure.log"
    /usr/bin/cmake --build "$report_dir/build" 2>&1 | tee "$report_dir/build.log"
    /usr/bin/ctest --test-dir "$report_dir/build" --output-on-failure --output-junit "$report_dir/ctest.xml" 2>&1 | tee "$report_dir/ctest.log"
    ;;
esac
