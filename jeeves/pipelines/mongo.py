# jeeves/pipelines/basic_deployment_docker.py

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import time
import pathlib
from botocore.exceptions import ClientError
from ..pipeline import Pipeline
from ..aws_helpers import session, latest_ubuntu_ami
from ..config import settings


def wait_for_port(host: str, port: int = 22, timeout: int = 300) -> None:
    """
    Wait until the given TCP port on host is accepting connections, or
    timeout in `timeout` seconds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                return
        except Exception:
            time.sleep(5)
    raise TimeoutError(f"Timeout waiting for {host}:{port}")


class BasicDeploymentDocker(Pipeline):
    pipeline_name        = "MongoDb Deploy"
    pipeline_description = "Deploys standalone MongoDB "
    docs_path            = pathlib.Path(__file__).parents[2] / "docs" / "mongo.md"

    def run(self) -> None:
        # ────────────────────────────────────────────────────
        # 1) SSH key validation/import (omitted for brevity)
        #    Assumes SSH_KEY_NAME, SSH_KEY_PATH, SSH_PUBLIC_KEY_PATH already handled
        # ────────────────────────────────────────────────────
        key_name    = os.environ["SSH_KEY_NAME"]
        key_path    = pathlib.Path(os.environ["SSH_KEY_PATH"]).expanduser()
        pubkey_path = pathlib.Path(os.environ["SSH_PUBLIC_KEY_PATH"]).expanduser()
        assert key_name and key_path.exists() and pubkey_path.exists()

        sess = session()
        ec2c = sess.client("ec2")
        ec2  = sess.resource("ec2")

        # import keypair if missing...
        try:
            ec2c.describe_key_pairs(KeyNames=[key_name])
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidKeyPair.NotFound":
                ec2c.import_key_pair(KeyName=key_name,
                                     PublicKeyMaterial=pubkey_path.read_bytes())
            else:
                raise

        # ────────────────────────────────────────────────────
        # 2) Find-or-create MongoDB instance
        # ────────────────────────────────────────────────────
        # Look for any instance tagged Name=jeeves-mongo in any state
        resp = ec2c.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": ["jeeves-mongo"]},
                {"Name": "instance-state-name", "Values": ["pending","running","stopped"]},
            ]
        )
        mongo_inst = None
        for r in resp.get("Reservations", []):
            for inst in r.get("Instances", []):
                mongo_inst = ec2.Instance(inst["InstanceId"])
                state = inst["State"]["Name"]
                print(f"Found existing MongoDB instance {mongo_inst.id} ({state})")
                if state == "stopped":
                    print("Starting stopped instance...")
                    mongo_inst.start()
                # wait until running
                mongo_inst.wait_until_running()
                mongo_inst.reload()
                break
            if mongo_inst:
                break

        if not mongo_inst:
            # create new
            ami        = latest_ubuntu_ami(ec2c, settings.default_os_version)
            # default VPC & subnet
            vpc = ec2c.describe_vpcs(Filters=[{"Name":"isDefault","Values":["true"]}])["Vpcs"][0]
            subnet = ec2c.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc["VpcId"]]}])["Subnets"][0]
            # SG reused from before
            sg = ec2c.describe_security_groups(
                Filters=[
                    {"Name":"group-name","Values":["jeeves-basic"]},
                    {"Name":"vpc-id",     "Values":[vpc["VpcId"]]},
                ]
            )["SecurityGroups"][0]
            # build user-data stub (we will SSH-install instead)
            user_data = "#!/usr/bin/env bash\nexit 0\n"

            print("Creating new MongoDB instance…")
            mongo_inst = ec2.create_instances(
                ImageId=ami,
                InstanceType=settings.default_instance_type,
                MinCount=1, MaxCount=1,
                KeyName=key_name,
                NetworkInterfaces=[{
                    "SubnetId": subnet["SubnetId"],
                    "DeviceIndex": 0,
                    "AssociatePublicIpAddress": True,  # public IP so we can SSH in
                    "Groups": [sg["GroupId"]],
                }],
                TagSpecifications=[{
                    "ResourceType": "instance",
                    "Tags": [{"Key": "Name", "Value": "jeeves-mongo"}],
                }],
                UserData=user_data,
            )[0]
            print(f"Waiting for MongoDB instance {mongo_inst.id} to run…")
            mongo_inst.wait_until_running()
            mongo_inst.reload()

        mongo_ip = mongo_inst.public_ip_address
        print(f"MongoDB instance is {mongo_inst.id} @ {mongo_ip}")

        # ────────────────────────────────────────────────────
        # 3) Wait for SSH & install MongoDB via SSH
        # ────────────────────────────────────────────────────
        print("Waiting for SSH on MongoDB node…")
        wait_for_port(mongo_ip, 22, timeout=300)

        script = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "mongodb_bootstrap.sh"
        if not script.exists():
            raise FileNotFoundError(f"Missing script: {script}")

        # ────────────────────────────────────────────────────
        # Prepare & inject env vars so the script runs non-interactively
        # ────────────────────────────────────────────────────
        env = os.environ
        mongo_username = env.get("MONGO_USERNAME")
        mongo_password = env.get("MONGO_PASSWORD")
        port            = env.get("MONGO_PORT", "27017")
        replset_name    = env.get("REPLSET_NAME", "rs0")
        if not mongo_username or not mongo_password:
            raise RuntimeError("MONGO_USERNAME and MONGO_PASSWORD must be set in your .env")

        # build an export header for the remote script
        exports = "\n".join([
            f"export MONGO_PORT={port}",
            f"export REPLSET_NAME={replset_name}",
            f"export MONGO_USERNAME={mongo_username}",
            f"export MONGO_PASSWORD={mongo_password}",
        ]) + "\n"
        full_script = exports + script.read_text()

        print("Running MongoDB bootstrap over SSH as root…")
        # stream the combined script into sudo bash on the remote host
        subprocess.run([
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-i", str(key_path),
            f"ubuntu@{mongo_ip}", "sudo", "bash", "-s"
        ], check=True, input=full_script, text=True)

        print("✔ MongoDB installation complete.")

        # … rocket-chat provisioning would follow here …
        print("Running Rocket.Chat  Installation …")
        # Final summary
        summary = {
            "mongodb": {"id": mongo_inst.id, "public_ip": mongo_ip},
        }
        print(json.dumps(summary, indent=2))


def run(**kwargs):
    BasicDeploymentDocker().run()
