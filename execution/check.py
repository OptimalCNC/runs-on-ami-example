#!/usr/bin/env python3
"""Check the execution module, optionally compiling and running its Cobalt example."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile


MODULE = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xenomai-prefix", type=Path, help="compile against an installed Cobalt SDK")
    parser.add_argument("--cobalt-runtime", action="store_true", help="execute CTest on a running Cobalt kernel")
    args = parser.parse_args(argv)
    if args.cobalt_runtime and not args.xenomai_prefix:
        parser.error("--cobalt-runtime requires --xenomai-prefix")
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(MODULE / "tests"), "-v"],
                   cwd=MODULE, check=True)
    if args.xenomai_prefix:
        with tempfile.TemporaryDirectory(prefix="cobalt-execution-check-") as temporary:
            build = Path(temporary) / "cobalt"
            subprocess.run(["cmake", "-S", str(MODULE / "cobalt"), "-B", str(build),
                            f"-DXENOMAI_ROOT={args.xenomai_prefix.resolve()}"], check=True)
            subprocess.run(["cmake", "--build", str(build)], check=True)
            if args.cobalt_runtime:
                subprocess.run(["ctest", "--test-dir", str(build), "--output-on-failure"], check=True)
            else:
                print("Cobalt application compiled; execution requires a running Cobalt kernel.", flush=True)
    else:
        print("Cobalt SDK compilation and kernel execution were not requested.", flush=True)
    print("Execution module checks passed.")


if __name__ == "__main__":
    main()
