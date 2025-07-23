
# Destroy Pipeline: `destroy_basic_docker`

This document describes the `destroy_basic_docker` pipeline in Jeeves. Its purpose is to tear down the standalone Docker-based Rocket.Chat + MongoDB deployment by terminating the EC2 instances and removing the associated security group.

---

## Overview of Steps

1. **EC2 Instance Termination**  
   Find and terminate any instances tagged `jeeves-mongo` or `jeeves-rocketchat`.
2. **Security Group Deletion**  
   Delete the AWS security group named `jeeves-basic`.

Each step is written to be **idempotent** and **error-tolerant**, so you can safely re-run the pipeline even if resources are already gone.

---

## 1. EC2 Instance Termination

### 1.1. Instance Selection

We look for any EC2 instances in the following states:

- **Tags**  
  - `tag:Name = jeeves-mongo`  
  - `tag:Name = jeeves-rocketchat`
- **States**  
  - `pending`, `running`, `stopped`, `stopping`

```
filters = [
    {"Name": "tag:Name",            "Values": ["jeeves-mongo", "jeeves-rocketchat"]},
    {"Name": "instance-state-name", "Values": ["pending", "running", "stopped", "stopping"]},
]
instances = list(ec2.instances.filter(Filters=filters))
```

* If **no** instances are found, the pipeline logs:

  > No jeeves-mongo or jeeves-rocketchat instances found
  > and moves on.

### 1.2. Termination & Wait

If matching instances exist, we:

1. **Terminate** them in bulk.
2. Use the EC2 client’s **`instance_terminated` waiter** to block until all are fully shut down.
3. Log success when complete.

```python
if instances:
    ids = [inst.id for inst in instances]
    print(f"Terminating instances: {ids}")
    ec2.instances.filter(InstanceIds=ids).terminate()

    waiter = ec2c.get_waiter("instance_terminated")
    print("Waiting for instances to terminate…")
    waiter.wait(InstanceIds=ids)
    print("✔ Instances terminated")
```

---

## 2. Security Group Deletion

The pipeline attempts to delete the **`jeeves-basic`** security group:

1. **Describe** the SG by name.
2. If found, **delete** it.
3. If not found, log that it’s already gone.

```python
resp = ec2c.describe_security_groups(
    Filters=[{"Name": "group-name", "Values": ["jeeves-basic"]}]
)
sgs = resp.get("SecurityGroups", [])
if sgs:
    sg_id = sgs[0]["GroupId"]
    print(f"Deleting security group 'jeeves-basic' ({sg_id})")
    ec2c.delete_security_group(GroupId=sg_id)
    print("✔ Security group deleted")
else:
    print("No security group 'jeeves-basic' found")
```

* Any AWS **`ClientError`** during deletion is caught and logged, so the pipeline continues cleanly.

---

## Error Handling & Idempotency

* **Filtering** by tag and state ensures we only act on relevant instances.
* **Empty-result checks** (`if not instances`, `if not sgs`) skip steps gracefully when resources are missing.
* **AWS waiters** guarantee that instance termination has fully completed before proceeding.
* **`try/except ClientError`** around the SG delete call prevents unhandled exceptions on dependency violations or already-deleted groups.
* This design lets you **re-run** the destroy pipeline without manual cleanup.

---

## Summary

The `destroy_basic_docker` pipeline provides a simple, reliable teardown of the basic Rocket.Chat + MongoDB Docker deployment by:

1. **Finding & terminating** the two tagged instances.
2. **Removing** the `jeeves-basic` security group.

Thanks to its idempotent design and error handling, it can be executed repeatedly with predictable results.
