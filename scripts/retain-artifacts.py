#!/usr/bin/env python3
"""Retain diagnostic files in a versioned artifact bucket and write their index."""
import argparse
from pathlib import Path, PurePosixPath

from example import Cloud, load_cleanup_context, require, write_json


def retain(cloud, directory, prefix, output=None):
    directory = Path(directory)
    output = Path(output) if output is not None else directory / "artifact-index.json"
    key = PurePosixPath(prefix)
    require(prefix and str(key) == prefix and not key.is_absolute()
            and all(part not in (".", "..") for part in key.parts), "artifact prefix must be a relative object key")
    require(directory.is_dir(), f"artifact directory is missing: {directory}")
    locations = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if (path.is_file() and path.resolve() != output.resolve() and path.suffix in (".json", ".xml", ".log")
                and "build" not in relative.parts):
            locations[str(relative)] = cloud.retain(path, f"{prefix}/{relative.as_posix()}")
    write_json(output, locations)
    cloud.retain(output, f"{prefix}/artifact-index.json")
    return locations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup-context", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--prefix", required=True, help="object key prefix within the configured repository")
    parser.add_argument("--output", type=Path, required=True, help="artifact index file")
    args = parser.parse_args()
    locations = retain(Cloud(load_cleanup_context(args.cleanup_context)), args.directory, args.prefix, args.output)
    print(f"Retained {len(locations)} diagnostic files.")


if __name__ == "__main__":
    main()
