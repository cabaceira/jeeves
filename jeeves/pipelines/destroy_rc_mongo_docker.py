# jeeves/pipelines/destroy_basic_docker.py

"""
Pipeline: destroy_basic_docker

Terminates the EC2 instances and deletes the Security Group
created by the basic_deployment_docker pipeline.
"""

from __future__ import annotations
import time
import pathlib
from botocore.exceptions import ClientError
from ..pipeline import Pipeline
from ..aws_helpers import session

class DestroyBasicDocker(Pipeline):
    pipeline_name        = "Destroy Rocket.Chat Docker Deployment  "
    pipeline_description = "Destroy the two-node Deployment. One MongoDB, One Rocket.Chat Node"
    docs_path            = pathlib.Path(__file__).parents[2] / "docs" / "destroy_rc_mongo_docker.md"
    def run(self) -> None:
        sess = session()
        ec2 = sess.resource("ec2")
        ec2c = sess.client("ec2")

        # 1) Find instances tagged jeeves-mongo or jeeves-rocketchat
        filters = [
            {"Name": "tag:Name", "Values": ["jeeves-mongo", "jeeves-rocketchat"]},
            {"Name": "instance-state-name", "Values": ["pending","running","stopped","stopping"]},
        ]
        instances = list(ec2.instances.filter(Filters=filters))
        if instances:
            ids = [inst.id for inst in instances]
            print(f"Terminating instances: {ids}")
            ec2.instances.filter(InstanceIds=ids).terminate()
            # wait until all terminated
            waiter = ec2c.get_waiter("instance_terminated")
            waiter.wait(InstanceIds=ids)
            print("✔ Instances terminated")
        else:
            print("No jeeves-mongo or jeeves-rocketchat instances found")

        # 2) Delete security group "jeeves-basic"
        try:
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
        except ClientError as e:
            print(f"Error deleting security group: {e}")

def run(**kwargs):
    DestroyBasicDocker().run()
