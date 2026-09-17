# Build and test a Xenomai application on RunsOn

This module provides a [Cobalt example application](cobalt) and a
[workflow](../.github/workflows/run-cobalt.yml) that builds and tests it on a
custom image through RunsOn. The application starts an Alchemy task and checks
that it executes in Cobalt primary mode at priority 50.

Use an installed [RunsOn deployment](../runs-on/README.md) with this repository
authorized for its GitHub App, and a published [custom image](../images/README.md)
containing the Cobalt kernel, SDK at `/usr/xenomai`, C compiler, and CMake 3.28 or
newer. Once the workflow is on the default branch, dispatch it from the
repository checkout with the AMI ID, RunsOn environment, and AWS region:

```sh
gh workflow run run-cobalt.yml \
  -f ami_id=ami-YOUR_IMAGE_ID \
  -f environment=production \
  -f region=us-east-1
```

The environment defaults to `production` and the region to `us-east-1`. The
workflow requests an on-demand `t3.small` with a 16 GiB root disk, checks out the
source, builds the application with CMake, and runs CTest. Results and test output
appear in the GitHub Actions job log.

To build and test directly on a host running the Cobalt kernel with its SDK,
run these commands from the repository root:

```sh
cmake -S execution/cobalt -B execution/.local/cobalt -DXENOMAI_ROOT=/usr/xenomai
cmake --build execution/.local/cobalt
ctest --test-dir execution/.local/cobalt --verbose --output-on-failure
```
