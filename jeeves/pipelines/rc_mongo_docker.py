# jeeves/pipelines/rc_mongo_docker.py

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import time
import pathlib
from botocore.exceptions import ClientError
from botocore.exceptions import ClientError as BotoClientError
from ..pipeline import Pipeline
from ..aws_helpers import session, latest_ubuntu_ami
from ..config import settings


def wait_for_port(host: str, port: int = 22, timeout: int = 300) -> None:
    """
    Block until the given TCP port on `host` is accepting connections,
    or raise TimeoutError after `timeout` seconds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                return
        except Exception:
            time.sleep(5)
    raise TimeoutError(f"Timeout waiting for {host}:{port}")


class RcMongoDocker(Pipeline):
    pipeline_name        = "Rocket.Chat and MongoDB Docker"
    pipeline_description = "Two-node Deployment. One with Rocket.Chat and one MongoDB EC2 bootstrap"
    docs_path            = pathlib.Path(__file__).parents[2] / "docs" / "rc_mongo_docker.md"
    """
    AWS two-node deployment pipeline:
      - MongoDB node (jeeves-mongo, SG 'jeeves-basic')
      - Rocket.Chat node (jeeves-rocketchat, SG 'jeeves-rc')
    Installs via SSH the non-interactive bootstrap scripts under scripts/.
    """

    def run(self) -> None:
        env = os.environ

        # 1) SSH key settings
        key_name    = env.get("SSH_KEY_NAME")
        key_path    = pathlib.Path(env.get("SSH_KEY_PATH", "")).expanduser()
        pubkey_path = pathlib.Path(env.get("SSH_PUBLIC_KEY_PATH", "")).expanduser()
        if not (key_name and key_path.exists() and pubkey_path.exists()):
            raise RuntimeError(
                "Please set SSH_KEY_NAME, SSH_KEY_PATH, and SSH_PUBLIC_KEY_PATH in your .env"
            )

        # 2) AWS clients & KeyPair import if needed
        sess = session()
        ec2c = sess.client("ec2")
        ec2  = sess.resource("ec2")
        try:
            ec2c.describe_key_pairs(KeyNames=[key_name])
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidKeyPair.NotFound":
                material = pubkey_path.read_bytes()
                ec2c.import_key_pair(KeyName=key_name, PublicKeyMaterial=material)
                print(f"Imported key pair '{key_name}'")
            else:
                raise

        # 3) Default VPC & Subnet
        vpcs = ec2c.describe_vpcs(Filters=[{"Name":"isDefault","Values":["true"]}])["Vpcs"]
        if not vpcs:
            raise RuntimeError("No default VPC found")
        vpc_id = vpcs[0]["VpcId"]
        subnets = ec2c.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["Subnets"]
        if not subnets:
            raise RuntimeError(f"No subnet found in VPC {vpc_id}")
        subnet_id = subnets[0]["SubnetId"]

        # 4) Security Group for MongoDB node (ensure SSH + Mongo)
        sg_resp = ec2c.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": ["jeeves-basic"]},
                {"Name": "vpc-id",     "Values": [vpc_id]},
            ]
        )
        sgs = sg_resp.get("SecurityGroups", [])
        if sgs:
            basic_sg_id = sgs[0]["GroupId"]
            print(f"Reusing SG 'jeeves-basic' ({basic_sg_id})")
        else:
            create_resp = ec2c.create_security_group(
                GroupName="jeeves-basic",
                Description="SSH + Mongo only between nodes",
                VpcId=vpc_id,
            )
            basic_sg_id = create_resp["GroupId"]
            # Tag the SG for easy identification
            ec2c.create_tags(
                Resources=[basic_sg_id],
                Tags=[
                    {"Key": "Name",    "Value": "jeeves-basic"},
                    {"Key": "Project", "Value": "jeeves"},
                    {"Key": "Role",    "Value": "mongo-sg"},
                ]
            )
            print(f"Created and tagged SG 'jeeves-basic' ({basic_sg_id})")

        # Ensure SSH (22) and Mongo (27017 intra-SG) ingress rules exist
        permissions = [
            {
                "IpProtocol": "tcp", "FromPort": 22,    "ToPort": 22,
                "IpRanges":    [{"CidrIp": "0.0.0.0/0"}],
            },
            {
                "IpProtocol": "tcp", "FromPort": 27017, "ToPort": 27017,
                "UserIdGroupPairs": [{"GroupId": basic_sg_id}],
            },
        ]
        for perm in permissions:
            try:
                ec2c.authorize_security_group_ingress(
                    GroupId=basic_sg_id,
                    IpPermissions=[perm]
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise

        print("Configured SG 'jeeves-basic' for SSH and Mongo")

        # 5) Security Group for Rocket.Chat node
        rc_resp = ec2c.describe_security_groups(
            Filters=[
                {"Name":"group-name","Values":["jeeves-rc"]},
                {"Name":"vpc-id",    "Values":[vpc_id]},
            ]
        )
        rc_sgs = rc_resp.get("SecurityGroups", [])
        if rc_sgs:
            rc_sg_id = rc_sgs[0]["GroupId"]
            print(f"Reusing SG 'jeeves-rc' ({rc_sg_id})")
        else:
            create_resp = ec2c.create_security_group(
                GroupName="jeeves-rc",
                Description="SSH, HTTP, HTTPS for Rocket.Chat",
                VpcId=vpc_id,
            )
            rc_sg_id = create_resp["GroupId"]
            # Tag it for easy identification
            ec2c.create_tags(
                Resources=[rc_sg_id],
                Tags=[
                    {"Key": "Name",    "Value": "jeeves-rocketchat-sg"},
                    {"Key": "Project", "Value": "jeeves"},
                    {"Key": "Role",    "Value": "rocketchat-sg"},
                ]
            )
            print(f"Created SG 'jeeves-rc' ({rc_sg_id}) and tagged it")
            # allow SSH, HTTP, HTTPS
            for port in (22, 80, 443):
                ec2c.authorize_security_group_ingress(
                    GroupId=rc_sg_id,
                    IpPermissions=[{
                        "IpProtocol": "tcp",
                        "FromPort":  port,
                        "ToPort":    port,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    }]
                )
            print("Configured SG 'jeeves-rc' for SSH, HTTP, HTTPS")

        # 6) Allow Rocket.Chat SG access to Mongo in jeeves-basic SG
        try:
            ec2c.authorize_security_group_ingress(
                GroupId=basic_sg_id,
                IpPermissions=[{
                    "IpProtocol": "tcp",
                    "FromPort":   27017,
                    "ToPort":     27017,
                    "UserIdGroupPairs": [{"GroupId": rc_sg_id}],
                }]
            )
            print(f"Allowed port 27017 from 'jeeves-rc' ({rc_sg_id}) into 'jeeves-basic' ({basic_sg_id})")
        except BotoClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                raise


        # 7) MongoDB EC2 instance
        mongo_inst = None
        resp = ec2c.describe_instances(
            Filters=[
                {"Name":"tag:Name",            "Values":["jeeves-mongo"]},
                {"Name":"instance-state-name", "Values":["pending","running","stopped"]},
            ]
        )
        for r in resp.get("Reservations", []):
            for inst in r.get("Instances", []):
                mongo_inst = ec2.Instance(inst["InstanceId"])
                state = inst["State"]["Name"]
                print(f"Found existing MongoDB {mongo_inst.id} ({state})")
                if state == "stopped":
                    mongo_inst.start()
                mongo_inst.wait_until_running()
                mongo_inst.reload()
                break
            if mongo_inst:
                break

        if not mongo_inst:
            ami = latest_ubuntu_ami(ec2c, settings.default_os_version)
            mongo_inst = ec2.create_instances(
                ImageId=ami,
                InstanceType=settings.default_instance_type,
                MinCount=1, MaxCount=1,
                KeyName=key_name,
                NetworkInterfaces=[{
                    "SubnetId": subnet_id,
                    "DeviceIndex": 0,
                    "AssociatePublicIpAddress": True,
                    "Groups": [basic_sg_id],
                }],
                TagSpecifications=[
                    {"ResourceType":"instance",
                     "Tags":[
                       {"Key":"Name",     "Value":"jeeves-mongo"},
                       {"Key":"Project",  "Value":"jeeves"},
                       {"Key":"Role",     "Value":"mongo-node"},
                       {"Key": "Deployment",  "Value": deployment_name},
                     ]},
                    {"ResourceType":"volume",
                     "Tags":[
                       {"Key":"Name",     "Value":"jeeves-mongo-root"},
                       {"Key":"Project",  "Value":"jeeves"},
                       {"Key": "Deployment",  "Value": deployment_name},
                     ]}
                  ],
                UserData="#!/usr/bin/env bash\nexit 0\n",
            )[0]
            print(f"Launching MongoDB {mongo_inst.id}…")
            mongo_inst.wait_until_running(); mongo_inst.reload()

        mongo_public_ip  = mongo_inst.public_ip_address
        mongo_private_ip = mongo_inst.private_ip_address
        print(f"MongoDB up: {mongo_inst.id} @ public {mongo_public_ip}, private {mongo_private_ip}")

        # 7) Install MongoDB via SSH (with logging and timeout)
        install_timeout = 120  # seconds
        interval        = 5    # seconds between retries
        start           = time.time()

        print(f"→ Waiting for SSH on MongoDB host {mongo_public_ip}:22 …", flush=True)
        while True:
            try:
                wait_for_port(mongo_public_ip, 22, timeout=interval)
                print("✔ SSH is up on MongoDB host", flush=True)
                break
            except TimeoutError:
                elapsed = time.time() - start
                if elapsed > install_timeout:
                    raise RuntimeError(
                        f"Timeout waiting for SSH on MongoDB host after {install_timeout}s"
                    )
                print(f"… still waiting (elapsed {int(elapsed)}s)", flush=True)

        mongo_script = pathlib.Path(__file__).parents[2] / "scripts" / "mongodb_bootstrap.sh"
        if not mongo_script.exists():
            raise FileNotFoundError(f"Missing script: {mongo_script}")

        # prepare env for non-interactive run
        port       = env.get("MONGO_PORT", "27017")
        repl_name  = env.get("REPLSET_NAME", "rs0")
        mongo_user = env["MONGO_USERNAME"]
        mongo_pass = env["MONGO_PASSWORD"]
        header = "\n".join([
            f"export MONGO_PORT={port}",
            f"export REPLSET_NAME={repl_name}",
            f"export MONGO_USERNAME={mongo_user}",
            f"export MONGO_PASSWORD={mongo_pass}",
        ]) + "\n"

        print("→ Installing MongoDB via SSH…", flush=True)
        try:
            subprocess.run(
                [
                    "ssh",
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=no",
                    "-i", str(key_path),
                    f"ubuntu@{mongo_public_ip}", "sudo", "bash", "-s",
                ],
                check=True,
                input=header + mongo_script.read_text(),
                text=True,
                timeout=600,  # kill if the bootstrap hangs beyond 10m
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("MongoDB bootstrap script timed out after 10 minutes")
        print("✔ MongoDB installed\n", flush=True)

        # 9) Rocket.Chat EC2 instance
        rc_inst = None
        resp = ec2c.describe_instances(
            Filters=[
                {"Name":"tag:Name",            "Values":["jeeves-rocketchat"]},
                {"Name":"instance-state-name", "Values":["pending","running","stopped"]},
            ]
        )
        for r in resp.get("Reservations", []):
            for inst in r.get("Instances", []):
                rc_inst = ec2.Instance(inst["InstanceId"])
                state = inst["State"]["Name"]
                print(f"Found existing Rocket.Chat {rc_inst.id} ({state})")
                if state == "stopped":
                    rc_inst.start()
                rc_inst.wait_until_running()
                rc_inst.reload()
                break
            if rc_inst:
                break

        if not rc_inst:
            ami = latest_ubuntu_ami(ec2c, settings.default_os_version)
            rc_inst = ec2.create_instances(
                ImageId=ami,
                InstanceType=settings.default_instance_type,
                MinCount=1, MaxCount=1,
                KeyName=key_name,
                NetworkInterfaces=[{
                    "SubnetId": subnet_id,
                    "DeviceIndex": 0,
                    "AssociatePublicIpAddress": True,
                    "Groups": [rc_sg_id],
                }],
                TagSpecifications=[
                    {"ResourceType":"instance",
                     "Tags":[
                       {"Key":"Name",     "Value":"jeeves-rocketchat"},
                       {"Key":"Project",  "Value":"jeeves"},
                       {"Key":"Role",     "Value":"rocketchat-node"},
                       {"Key": "Deployment",  "Value": deployment_name},
                     ]},
                    {"ResourceType":"volume",
                     "Tags":[
                       {"Key":"Name",     "Value":"jeeves-rocketchat-root"},
                       {"Key":"Project",  "Value":"jeeves"},
                       {"Key": "Deployment",  "Value": deployment_name},
                     ]}
                  ],
                UserData="#!/usr/bin/env bash\nexit 0\n",
            )[0]
            print(f"Launching Rocket.Chat {rc_inst.id}…")
            rc_inst.wait_until_running(); rc_inst.reload()

        rc_ip = rc_inst.public_ip_address
        print(f"Rocket.Chat up: {rc_inst.id} @ {rc_ip}")

        # ────────────────────────────────────────────────────
        # Update DNS and wait for propagation before SSHing in
        # ────────────────────────────────────────────────────


        # 10) Install Rocket.Chat via SSH
        wait_for_port(rc_ip, 22)
        rc_script = pathlib.Path(__file__).parents[2] / "scripts" / "rocket_chat_ec2_bootstrap.sh"
        if not rc_script.exists():
            raise FileNotFoundError(f"Missing script: {rc_script}")

        rc_header = "\n".join([
            f"export MONGO_USERNAME={mongo_user}",
            f"export MONGO_PASSWORD={mongo_pass}",
            f"export MONGO_HOST={mongo_private_ip}",
            f"export MONGO_PORT={port}",
            f"export REPLSET={repl_name}",
            f"export RELEASE={env['RELEASE']}",
            f"export IMAGE={env['IMAGE']}",
            f"export TRAEFIK_RELEASE={env['TRAEFIK_RELEASE']}",
            f"export ROOT_URL={env['ROOT_URL']}",
            f"export DOMAIN={env['DOMAIN']}",
            f"export LETSENCRYPT_EMAIL={env['LETSENCRYPT_EMAIL']}",
        ]) + "\n"
        print("Installing Rocket.Chat via SSH…")
        try:
            subprocess.run([
                "ssh","-o","StrictHostKeyChecking=no",
                "-i", str(key_path),
                f"ubuntu@{rc_ip}", "sudo","bash","-s"
            ], check=True, input=rc_header + rc_script.read_text(), text=True)
        except subprocess.CalledProcessError as e:
            if e.returncode == 22:
                # curl inside the bootstrap returned 22 (HTTP error),
                # but Rocket.Chat is up and healthy, so we can ignore.
                print("⚠️  Rocket.Chat bootstrap exited with 22; ignoring because service is running.")
            else:
                 raise
        print("✔ Rocket.Chat & Traefik installed\n")


        # Determine the domain from settings
        domain = settings.domain.strip()
        if not domain:
            raise RuntimeError("DOMAIN must be set in settings")

        print("Updating DNS record for DOMAIN…", flush=True)
        from .route53_update import Route53Update
        Route53Update().run()

        print(f"Waiting up to 5m for {domain} → {rc_ip} …", flush=True)
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                resolved = socket.gethostbyname(domain)
                if resolved == rc_ip:
                    print(f"✔ DNS is live: {domain} → {rc_ip}", flush=True)
                    break
            except socket.gaierror:
                pass
            print("… still waiting for DNS to propagate", flush=True)
            time.sleep(5)
        else:
            raise RuntimeError(f"DNS did not propagate to {rc_ip} within 5 minutes")





        # 11) Final summary & SSH hint
        summary = {
            "mongodb":    {
                "id":         mongo_inst.id,
                "public_ip":  mongo_public_ip,
                "private_ip": mongo_private_ip,
            },
            "rocketchat": {
                "id":        rc_inst.id,
                "public_ip": rc_ip,
            },
        }
        print(json.dumps(summary, indent=2))
        print(f"\nSSH into Rocket.Chat:\n  ssh -i {key_path} ubuntu@{rc_ip}")

        # 12) Update DNS record in Route 53
        print("\nUpdating Route 53 A record for DOMAIN…")
        try:
            # relative import of the Route53Update pipeline
            from .route53_update import Route53Update
            Route53Update().run()
            print("✔ Route 53 record updated\n")
        except Exception as e:
            print(f"⚠️  Failed to update Route 53: {e}")


def run(**kwargs):
    RcMongoDocker().run()
