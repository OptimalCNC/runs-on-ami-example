def installation_value():
    return {
        "schema_version": 1,
        "kind": "runs-on-installation",
        "name": "runs-on-platform",
        "account_id": "123456789012",
        "region": "us-east-1",
        "environment": "production",
        "github_organization": "ExampleOrg",
        "setup_url": "https://example.execute-api.us-east-1.amazonaws.com/prod",
        "versions": {"terraform_module": "3.3.1", "app": "v3.3.1", "bootstrap": "v0.1.12"},
        "network": {"vpc_id": "vpc-0123456789abcdef0", "public_subnet_ids": ["subnet-0123456789abcdef0"]},
        "runtime": {
            "cluster_name": "runs-on-platform", "service_name": "flexd",
            "task_role_arn": "arn:aws:iam::123456789012:role/runs-on-platform-flex-role",
            "runner_role_arn": "arn:aws:iam::123456789012:role/runs-on-platform-ec2-instance-role",
            "runner_profile_arn": "arn:aws:iam::123456789012:instance-profile/runs-on-platform-ec2-instance-profile",
            "runner_max_runtime_minutes": 60,
        },
    }


def image_value():
    return {
        "schema_version": 1, "kind": "published-image", "status": "available",
        "publication_id": "a" * 32,
        "target": {
            "name": "runs-on-platform", "account_id": "123456789012", "region": "us-east-1",
            "publisher_role_arn": "arn:aws:iam::123456789012:role/runs-on-platform-image-publisher",
            "encrypted": False,
            "required_tags": {"runs-on-installation": "runs-on-platform"},
        },
        "account_id": "123456789012", "region": "us-east-1",
        "artifact": {"sha256": "b" * 64, "size_bytes": 16 * 1024**3,
                     "recipe_id": "c" * 64, "manifest_sha256": "d" * 64},
        "compatibility": {
            "architecture": "x86_64", "boot_mode": "uefi", "secure_boot": False,
            "minimum_root_volume_gib": 16, "ena_support": True, "runs_on_bootstrap_version": "0.1.12",
        },
        "ami_id": "ami-0123456789abcdef0", "snapshot_id": "snap-0123456789abcdef0",
        "ami_name": "runs-on-platform-bbbbbbbbbbbb-aaaaaaaaaaaa",
        "created_at": "2026-09-16T02:00:00+00:00",
    }
