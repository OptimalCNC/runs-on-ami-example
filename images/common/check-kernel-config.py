#!/usr/bin/env python3
"""Compare semantic Kconfig values; comments/order carry no configuration."""
import re
import sys
from pathlib import Path


def parse_config(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            if key in result:
                raise ValueError(f"duplicate Kconfig symbol: {key}")
            result[key] = value
        elif match := re.fullmatch(r"# (CONFIG_\w+) is not set", line):
            if match[1] in result:
                raise ValueError(f"duplicate Kconfig symbol: {match[1]}")
            result[match[1]] = "n"
    return result


def main():
    expected, actual = map(parse_config, sys.argv[1:3])
    changes = {key: [expected.get(key), actual.get(key)] for key in expected.keys() | actual.keys()
               if expected.get(key) != actual.get(key)}
    if changes:
        raise SystemExit(f"effective kernel config differs from lock: {changes}")
    print(f"Effective kernel configuration matches all {len(expected)} symbols")


if __name__ == "__main__":
    main()
