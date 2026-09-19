# Publish raw disks to AWS

The Python package provides three commands: `publish.publish` uploads a raw disk
and registers an AMI, `publish.prune` retains the newest matching publication,
and `publish.delete` deletes one recorded publication. They share AWS access,
configuration parsing, and recovery through relative imports from `core.py`.

Copy the `publish/` directory wherever needed and run the commands from its
parent directory. They need no other repository files or builder metadata.

## Inputs

Publishing and pruning share a JSON configuration file. For example,
`publishing.json` contains:

```json
{
  "name": "<image-name>",
  "account_id": "123456789012",
  "region": "us-east-1",
  "tags": {}
}
```

Replace `<image-name>` with the stable name for the image series. The account
and region identify the destination. `tags` is optional and defaults to an empty
object. Supply up to 45 caller-chosen tags for organization or IAM requirements.
Every supplied tag must match when selecting resources for pruning or deletion.
With no caller tags, pruning selects by account, region, and exact image name.
`Name`, `image-family`, `image-publication-id`, `image-sha256`, and `image-retired`
are reserved for publication identity and retention. AWS-reserved `aws:` keys
and keys containing `,Value=` are not supported.

AWS CLI handles credentials; follow the
[AWS CLI configuration guide](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-configure.html)
to set them up. The optional `--profile <name>` is passed directly to AWS CLI.

Publish a disk:

```sh
python3 -m publish.publish --config publishing.json \
  --image /path/to/disk.raw --output publication
```

The disk must be a readable, nonempty raw disk file containing a complete
bootable disk. Keep it unchanged throughout hashing and upload. The script
derives its SHA-256 and rounds its logical size up to whole GiB for EBS.
Supply a writable output directory without an existing `published-image.json`.
Relative paths resolve against the current working directory.

The configuration's `name` accepts 1-102 letters, digits, dots, underscores,
or hyphens, starting with a letter or digit.
Each AMI gets a unique `<name>-<digest-prefix>-<publication-prefix>` name;
the AMI and snapshot carry the exact stable name in `Name` and `image-family`
tags. Repeating publication creates another version, including for the same disk.

Keep the newest matching available publication and its backing snapshot:

```sh
python3 -m publish.prune --config publishing.json
```

Delete a particular publication, including an incomplete one:

```sh
python3 -m publish.delete --record publication/published-image.json
```

Deletion uses the destination, ownership tags, and resource identity saved in the
record. It needs neither the disk nor a publishing configuration, so later
configuration changes cannot redirect deletion. Any authorized credentials for
the recorded account can be used.

## Prerequisites

Use Python 3.11 or newer and AWS CLI v2 with `aws configure export-credentials`
available on `PATH`. Publishing also needs
[AWS Labs coldsnap](https://github.com/awslabs/coldsnap) 0.12.0 on `PATH`.
Python uses only its standard library. Cleanup needs no `coldsnap`.

The host needs working AWS connectivity. Credentials must belong to the
destination account and remain valid throughout the upload.

AWS permissions must cover caller-identity inspection, `ec2:DescribeImages`,
`ec2:DescribeSnapshots`, `ec2:GetEbsEncryptionByDefault`, `ebs:StartSnapshot`,
`ebs:PutSnapshotBlock`, `ebs:CompleteSnapshot`, `ec2:CreateTags`,
`ec2:RegisterImage`, `ec2:DeregisterImage`, and `ec2:DeleteSnapshot` as needed
for the selected operation and its recovery. The destination needs sufficient
quotas and EBS encryption by default disabled for unencrypted publication.

Supported disks are compatible with an unencrypted x86-64 HVM AMI using UEFI
and ENA, one backing EBS snapshot, and a `gp3` root-device mapping at `/dev/sda1`
with deletion on instance termination. The caller establishes disk bootability
and application correctness before publication.

Serialize operations for the same account, region, ownership scope, and image
name, and do not share a publication output directory between invocations.
The script provides no distributed locking. Keep recovery records while their
resources exist or cleanup remains incomplete.

## Results and guarantees

Publication writes `published-image.json` atomically before uploading and updates
it as resources become known. The record contains the destination, stable and
generated image names, publication ID, source disk digest, snapshot and AMI IDs,
and status. `upload.log` contains upload diagnostics. Credentials are not written
to these outputs.

A successful publication exits with status 0 and prints the record path. Its
record has `status: available`. The script has observed a completed snapshot
and an available AMI with the expected account, ownership, backing snapshot,
volume size, encryption, architecture, boot mode, and ENA support. It reads the
source disk without modification. AWS availability does not establish guest
boot or application success, or an independent checksum readback of the upload.

Publication leaves older successful versions alone. Pruning keeps the newest
matching available AMI by AWS creation time, breaking ties by AMI ID. Only
publications with the exact stable name, ownership tags, and expected versioned
AMI name are selected. Pruning also retries snapshots marked for retirement.
Deletion deregisters AMIs before deleting their snapshots, tolerates resources
already removed, and updates a successfully cleaned record to `status: deleted`.

Runtime failures return a nonzero status and diagnostics; invalid command-line
usage exits with status 2. Logs and records do not remove AWS resources when
deleted locally. The current upload includes zero blocks, so a sparse disk still
transfers its full logical size. Upload and retained snapshots incur
[AWS charges](https://aws.amazon.com/ebs/pricing/). Publication does not grant
public or cross-account launch access.

## Failure and recovery

On a handled publication failure, the script attempts to clean up that
publication's resources and preserves its record. `cleanup-needed` means cleanup
must be retried with the recorded identity and destination. A pending record
left by abrupt termination can also be supplied to `publish.delete`. Resources
whose IDs were not saved can be discovered through their publication and ownership
tags.

A failed invocation does not guarantee that no AWS resources remain. Network
failures, expired credentials, hard termination, or unavailable local storage
can interrupt recovery. AWS discovery may lag resource creation; retain the
record and retry when cleanup reports that no snapshot is visible yet.

Retirement marks each old snapshot before deregistering its AMI, allowing a
later prune to retry snapshot deletion after the AMI is gone. A failed prune
leaves the newest matching publication available. Retry `publish.prune` without
rebuilding or republishing. Failed publication is not resumable; after cleanup,
use a new output directory to publish again.

## Local checks

Run from the directory containing `publish/`:

```sh
python3 -B -S -m unittest discover -s publish/tests -v
```

The tests exercise destination parsing, credential handling, publication,
ownership isolation, retention, and interrupted-operation recovery without AWS.
