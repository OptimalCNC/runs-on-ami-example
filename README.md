# Ubuntu 24.04 Xenomai Cobalt AMIs with RunsOn Flex

The repository separates three responsibilities:

- [RunsOn installation](runs-on/README.md) provisions the runner platform and
  image-publishing IAM access.
- [Image build, validation, and publishing](images/README.md) builds a disk,
  validates it in a VM, and publishes it as an AMI. Build and validation run on
  normal Linux hosts without AWS credentials.
- [RunsOn execution](execution/README.md) provides a Xenomai Cobalt example
  application and a workflow that builds and tests it on a custom image.

The image recipe builds Ubuntu 24.04 with a Dovetail-enabled Linux kernel,
Xenomai 3 Cobalt integration, and matching userspace development tools. The
Cobalt application test establishes functional execution without a latency target.

## Get started

1. Follow the [RunsOn installation guide](runs-on/README.md), including
   [GitHub setup and the installation check](runs-on/README.md#finish-the-github-setup).
2. Follow the [image guide](images/README.md) to build, validate, and publish an
   image. The installation guide explains how to
   [configure publishing from its exported contract](runs-on/README.md#updates-exports-and-removal).
3. Follow the [execution guide](execution/README.md) to run the Cobalt example
   with the installed RunsOn environment and AWS region. New publications are
   selected automatically through the stable `image=ubuntu2404-xenomai-cobalt`
   selector.

## Validate locally

Run the validation commands documented by the module you are changing:

- [Images](images/README.md): Publishing tests and full disk build and VM
  validation.
- [RunsOn installation](runs-on/README.md#prerequisites): Python tests and
  isolated Terraform validation and mock tests.
- [Execution](execution/README.md): CMake build and CTest commands for a
  Cobalt SDK and kernel.

The [validation workflow](.github/workflows/validate.yml) runs publication and
installation tests and workflow linting as independent jobs.
Generated files stay in each module's ignored `.local/` directory.

For workflow changes, have [actionlint](https://github.com/rhysd/actionlint)
available on `PATH` and run:

```sh
actionlint -shellcheck= -config-file=.github/actionlint.yaml
```

## License

Repository code uses the [MIT license](LICENSE); the pinned Linux and Xenomai
sources retain their upstream licenses.
