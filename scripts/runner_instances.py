"""Adopt explicitly identified test instances after proving their cloud ownership."""
from example import (BUILD_TAG, EXPIRY_TAG, INSTANCE, OWNER_TAG, PURPOSE_TAG, RUNS_ON_TAG,
                     filters_for, match, parse_time, require, resource_tags, tags_of)


def adopt_test_instances(cloud, build, instance_ids, *, instance_type, image=None, expiry=None):
    d = cloud.deployment
    identifiers = [match(value, INSTANCE, "test instance ID") for value in instance_ids]
    require(len(identifiers) == len(set(identifiers)), "test instance identities must be distinct")
    if not identifiers:
        return []
    images = [image] if image else cloud.call("ec2", "describe-images", {
        "Owners": [d.account_id], "Filters": filters_for(d.repository, build)})["Images"]
    observations = []
    for identifier in identifiers:
        instance = cloud.instance(identifier)
        candidates = [candidate for candidate in images if candidate["ImageId"] == instance["ImageId"]]
        require(len(candidates) == 1, "test instance uses an unexpected AMI")
        candidate = candidates[0]
        tags = tags_of(instance)
        require(tags.get(RUNS_ON_TAG) == d.repository, "RunsOn stack must propagate its repository ownership marker")
        require(tags.get(OWNER_TAG) in (None, d.repository) and tags.get(BUILD_TAG) in (None, build)
                and tags.get(PURPOSE_TAG) in (None, "test"), "test instance is already owned by another build")
        require(parse_time(instance["LaunchTime"]) >= parse_time(candidate["CreationDate"]), "test instance predates image")
        require(instance["InstanceType"] == instance_type
                and instance.get("InstanceLifecycle", "on-demand") == "on-demand",
                "RunsOn launched an unqualified instance type or purchasing model")
        if instance["State"]["Name"] != "terminated" and (tags.get(BUILD_TAG) != build or tags.get(OWNER_TAG) != d.repository):
            inherited = resource_tags(d, build, "test", expiry or tags_of(candidate)[EXPIRY_TAG])
            cloud.call("ec2", "create-tags", {"Resources": [instance["InstanceId"]], "Tags": inherited})
        observations.append(instance)
    return observations
