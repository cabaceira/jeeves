# jeeves/pipelines/rc_microservices_helm.py
# jeeves/pipelines/rc_microservices_helm.py

from __future__ import annotations
import os
import json
import pathlib
import shutil
import subprocess
import time
import pathlib
from botocore.exceptions import ClientError
from ..pipeline import Pipeline
from ..aws_helpers import session, latest_ubuntu_ami
from ..config import settings
from datetime import datetime


def wait_for_ssh(host: str, key_path: pathlib.Path, user: str = "ubuntu", timeout: int = 300):
    """
    Wait until 'ssh -i key_path user@host true' succeeds, or timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = subprocess.run([
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-i", str(key_path),
            f"{user}@{host}",
            "true",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode == 0:
            return
        time.sleep(5)
    raise TimeoutError(f"SSH to {user}@{host} with key {key_path} timed out")

class K8sDeploymentHelm(Pipeline):
    pipeline_name        = "Rocket.Chat Microservices Deployment with Helm Charts "
    pipeline_description = "Three-node Deployment. One MongoDB, One Controller Node and one Worker Node"
    docs_path            = pathlib.Path(__file__).parents[2] / "docs" / "rc_microservices_helm.md"
    """
    1. Import SSH keypair into AWS (if missing)
    2. Provision Mongo master, MicroK8s controller & worker EC2 nodes
    3. Copy SSH key into ps-auto-infra/
    4. Write terraform.tfvars with their IPs + all settings (including instance types)
    5. Wait for SSH on worker & re-install public key
    6. terraform init & apply ps-auto-infra
    7. Post-apply: wait for SSH & re-install public key again
    """

    def run(self) -> None:
        env          = os.environ
        deployment_name = env.get("DEPLOYMENT_NAME")
        if not deployment_name:
            deployment_name = datetime.utcnow().strftime("deploy-%Y%m%d%H%M%S")
        print(f"▶ Deployment name: {deployment_name}")
        ssh_key_name = env["SSH_KEY_NAME"]            # e.g. "ps-lab"
        ssh_key_path = pathlib.Path(env["SSH_KEY_PATH"]).expanduser()
        pubkey_path  = pathlib.Path(env["SSH_PUBLIC_KEY_PATH"]).expanduser()
        tf_dir       = pathlib.Path(__file__).parents[2] / "ps-auto-infra"
        tfvars_path  = tf_dir / "terraform.tfvars"

        # derive the K8s instance type (controller+worker) from .env, else default
        k8s_instance_type = env.get("KUBERNETES_INSTANCE_TYPE", settings.default_instance_type)

        # ———————————
        # 1) AWS & KeyPair import
        # ———————————
        sess = session()
        ec2c = sess.client("ec2")
        ec2  = sess.resource("ec2")

        if not pubkey_path.exists():
            raise FileNotFoundError(f"Missing public key: {pubkey_path}")

        try:
            ec2c.describe_key_pairs(KeyNames=[ssh_key_name])
            print(f"✔ KeyPair '{ssh_key_name}' already in AWS")
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidKeyPair.NotFound":
                pub = pubkey_path.read_text()
                ec2c.import_key_pair(KeyName=ssh_key_name, PublicKeyMaterial=pub)
                print(f"✔ Imported KeyPair '{ssh_key_name}'")
            else:
                raise

        # ———————————
        # 2) VPC & Subnet
        # ———————————
        vpcs = ec2c.describe_vpcs(Filters=[{"Name":"isDefault","Values":["true"]}])["Vpcs"]
        if not vpcs:
            raise RuntimeError("No default VPC found")
        vpc_id = vpcs[0]["VpcId"]

        subs = ec2c.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["Subnets"]
        if not subs:
            raise RuntimeError(f"No subnet found in VPC {vpc_id}")
        subnet_id = subs[0]["SubnetId"]

        # ———————————
        # 3) Security Groups
        # ———————————
        def get_sg(name: str, desc: str):
            resp = ec2c.describe_security_groups(
                Filters=[
                    {"Name":"group-name","Values":[name]},
                    {"Name":"vpc-id","Values":[vpc_id]},
                ]
            )["SecurityGroups"]
            if resp:
                sg_id = resp[0]["GroupId"]
                print(f"Reusing SG '{name}' ({sg_id})")
            else:
                sg_id = ec2c.create_security_group(
                    GroupName=name, Description=desc, VpcId=vpc_id
                )["GroupId"]
                print(f"Created SG '{name}' ({sg_id})")
            return sg_id

        mongo_sg      = get_sg("jeeves-k8s-mongo",      "SSH + Mongo access")
        controller_sg = get_sg("jeeves-k8s-controller", "SSH + HTTP/HTTPS")
        worker_sg     = get_sg("jeeves-k8s-worker",     "SSH + k8s-node traffic")

        # a) open SSH on all
        for sg in (mongo_sg, controller_sg, worker_sg):
            try:
                ec2c.authorize_security_group_ingress(
                    GroupId=sg,
                    IpPermissions=[{
                        "IpProtocol":"tcp","FromPort":22,"ToPort":22,
                        "IpRanges":[{"CidrIp":"0.0.0.0/0"}],
                    }]
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise

        # b) HTTP/HTTPS on controller
        for p in (80,443):
            try:
                ec2c.authorize_security_group_ingress(
                    GroupId=controller_sg,
                    IpPermissions=[{
                        "IpProtocol":"tcp","FromPort":p,"ToPort":p,
                        "IpRanges":[{"CidrIp":"0.0.0.0/0"}],
                    }]
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise

        # c) Mongo 27017 only from ctrl+worker
        for src in (controller_sg, worker_sg):
            try:
                ec2c.authorize_security_group_ingress(
                    GroupId=mongo_sg,
                    IpPermissions=[{
                        "IpProtocol":"tcp","FromPort":27017,"ToPort":27017,
                        "UserIdGroupPairs":[{"GroupId":src}],
                    }]
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise

        # d) full k8s traffic ctrl↔worker
        for a,b in ((controller_sg,worker_sg),(worker_sg,controller_sg)):
            try:
                ec2c.authorize_security_group_ingress(
                    GroupId=a,
                    IpPermissions=[{
                        "IpProtocol":"-1",
                        "UserIdGroupPairs":[{"GroupId":b}],
                    }]
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise

        # ———————————
        # 4) Provision helper
        # ———————————
        def provision(tag: str, sg_id: str):
            rs = ec2c.describe_instances(
                Filters=[
                    {"Name":"tag:Name","Values":[tag]},
                    {"Name":"instance-state-name","Values":["pending","running","stopped"]},
                ]
            )["Reservations"]
            inst = None
            if rs:
                data = rs[0]["Instances"][0]
                key  = data.get("KeyName")
                if key != ssh_key_name:
                    # stale-key → terminate & recreate
                    stale = data["InstanceId"]
                    print(f"Terminating stale {tag} {stale} (KeyName={key})")
                    ec2c.terminate_instances(InstanceIds=[stale])
                    ec2c.get_waiter("instance_terminated").wait(InstanceIds=[stale])
                else:
                    inst = ec2.Instance(data["InstanceId"])
                    state = inst.state["Name"]
                    print(f"Reusing {tag} {inst.id} ({state})")
                    if state == "stopped":
                        inst.start(); inst.wait_until_running(); inst.reload()

            if not inst:
                ami = latest_ubuntu_ami(ec2c, settings.default_os_version)
                inst = ec2.create_instances(
                    ImageId=ami,
                    InstanceType=k8s_instance_type,
                    MinCount=1, MaxCount=1,
                    KeyName=ssh_key_name,
                    NetworkInterfaces=[{
                        "SubnetId":subnet_id,
                        "DeviceIndex":0,
                        "AssociatePublicIpAddress":True,
                        "Groups":[sg_id],
                    }],
                    BlockDeviceMappings=[{
                        # 50 GB root volume
                        "DeviceName": "/dev/sda1",
                        "Ebs": {
                            "VolumeSize": 50,
                            "VolumeType": "gp3",
                            "DeleteOnTermination": True,
                        },
                    }],
                    TagSpecifications=[{
                        "ResourceType":"instance",
                        "Tags":[
                            {"Key":"Name",       "Value":tag},
                            {"Key":"Project",    "Value":"jeeves"},
                            {"Key":"Role",       "Value":tag},
                            {"Key":"Deployment", "Value":deployment_name},
                        ],
                    }],
                    UserData="#!/usr/bin/env bash\nexit 0\n",
                )[0]
                print(f"Launched {tag} {inst.id} with InstanceType={k8s_instance_type} and 50 GB root disk")
                inst.wait_until_running(); inst.reload()

            return inst, inst.public_ip_address, inst.private_ip_address

        # ———————————
        # 5) Provision each node
        # ———————————
        mongo_i,  mongo_pub,  mongo_pri  = provision("jeeves-mongo-master",     mongo_sg)
        ctrl_i,   ctrl_pub,   ctrl_pri   = provision("jeeves-k8s-controller",  controller_sg)
        worker_i, worker_pub, worker_pri = provision("jeeves-k8s-worker",      worker_sg)

        print(json.dumps({
            "mongo":      {"id":mongo_i.id,  "public":mongo_pub,  "private":mongo_pri},
            "controller":{"id":ctrl_i.id,   "public":ctrl_pub,   "private":ctrl_pri},
            "worker":    {"id":worker_i.id, "public":worker_pub, "private":worker_pri},
        }, indent=2))

        # ———————————
        # 6) Copy SSH key into Terraform dir
        # ———————————
        tf_dir.mkdir(exist_ok=True)
        key_dest = tf_dir / ssh_key_path.name
        shutil.copy(ssh_key_path, key_dest)
        key_dest.chmod(0o600)
        print(f"Copied SSH key → {key_dest}")


        # ———————————
        # 7) Write terraform.tfvars
        # ———————————
        # Parse WORKERHA/MONGOHA into real bools (default to False)
        workerha_val = env.get("WORKERHA", "false").lower() in ("1", "true", "yes")
        mongoha_val  = env.get("MONGOHA",  "false").lower() in ("1", "true", "yes")

        tfvars = {
            "ssh_key_path":               str(key_dest.resolve()),
            "mongo_master_ip":            mongo_pub,
            "mongo_master_private_ip":    mongo_pri,
            "mongo_master_ssh_key_name":  ssh_key_name,
            "worker_ssh_key_path":        key_dest.name,
            **{f"mongo_read_replica{i}_ip":         "" for i in range(1, 5)},
            **{f"mongo_read_replica{i}_private_ip": "" for i in range(1, 5)},
            "controller_ip":              ctrl_pub,
            "controller_private_ip":      ctrl_pri,
            "worker_ip":                  worker_pub,
            "worker_private_ip":          worker_pri,
            **{f"worker{i}_ip":               "" for i in range(2, 6)},
            **{f"worker{i}_private_ip":       "" for i in range(2, 6)},
            "mongo_username":             env.get("MONGO_USERNAME", ""),
            "mongo_password":             env.get("MONGO_PASSWORD", ""),
            "mongodb_service_db":         env.get("MONGODB_SERVICE_DB", ""),
            "deployment_namespace":       env.get("DEPLOYMENT_NAMESPACE", "psautoinfra"),
            "kube_config_path":           env.get("KUBE_CONFIG_PATH", "/var/snap/microk8s/current/credentials/client.config"),
            "kube_config_context":        env.get("KUBE_CONFIG_CONTEXT", "microk8s"),
            "namespace":                  env.get("NAMESPACE", "psautoinfra"),
            "mongo_url_db":               env.get("MONGO_URL_DB", "rocketchat"),
            "worker_key_name":            env.get("WORKER_KEY_NAME", ""),
            "concurrent_users":           env.get("CONCURRENT_USERS", 1),
            "controller_node_name":       env.get("CONTROLLER_NODE_NAME", ""),
            "workerha":                   workerha_val,
            "mongoha":                    mongoha_val,
            "letsencrypt_email":          env.get("LETSENCRYPT_EMAIL", ""),
            "domain":                     env.get("DOMAIN", ""),
            "publicip":                   ctrl_pub,
            "cert_email":                 env.get("CERT_EMAIL", ""),
            "acme_email":                 env.get("ACME_EMAIL", ""),
        }

        with open(tfvars_path, "w") as f:
            for k, v in tfvars.items():
                if isinstance(v, bool):
                    # write booleans unquoted, lowercase
                    f.write(f"{k} = {str(v).lower()}\n")
                else:
                    f.write(f'{k} = "{v}"\n')

        print(f"Wrote terraform.tfvars to {tfvars_path}")

        # ———————————
        # 8) Pre-apply: ensure SSH is up then re-install public key
        # ———————————
        print("🔑 Waiting for SSH on worker (pre-apply)…")
        wait_for_ssh(worker_pub, ssh_key_path)
        pubkey = pubkey_path.read_text().strip()
        install_cmd = "\n".join([
            "mkdir -p ~/.ssh",
            "chmod 700 ~/.ssh",
            "cat >> ~/.ssh/authorized_keys << 'EOF'",
            pubkey,
            "EOF",
            "chmod 600 ~/.ssh/authorized_keys",
        ])
        subprocess.run([
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-i", str(ssh_key_path),
            f"ubuntu@{worker_pub}",
            install_cmd
        ], check=True)
        print("✔ Public key re-installed on worker (pre-apply)")

        tf_dir = str(tf_dir)
        tfvars_arg = f"-var-file={tfvars_path.name}"

        # ─────────────────────────────────────────────────────── Phase 1 ───────────────────────────────────────────────────────
        print("🚧 Phase 1: spinning up EC2 instances and installing MicroK8s…")
        subprocess.run(["terraform","init"],   cwd=tf_dir, check=True)
        subprocess.run([
            "terraform","apply","-auto-approve",
            tfvars_arg,
            # target only the controller module's null_resource so we get MicroK8s installed
            "-target=module.controller.null_resource.k8s_controller",
        ], cwd=tf_dir, check=True)

        # ─────────────────────────────────────────────────────── Phase 2 Prep ───────────────────────────────────────────────────────
        print("🔌 Opening SSH tunnel to controller for Kubernetes API…")
        # kill any old tunnel
        subprocess.run(
            ["pkill","-f",f"ssh .* -L 16443:127.0.0.1:16443 .*{ctrl_pub}"],
            check=False
        )
        # fire up a new one
        subprocess.Popen([
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-i", str(ssh_key_path),
            "-fN",
            "-L", "16443:127.0.0.1:16443",
            f"ubuntu@{ctrl_pub}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # wait for the tunnel to bind
        for i in range(24):
            if subprocess.run(["nc","-z","127.0.0.1","16443"]).returncode == 0:
                print("✔ SSH tunnel is up (127.0.0.1:16443 → controller)")
                break
            print(f"⏳ Waiting for API tunnel ({i+1}/24)…")
            time.sleep(5)
        else:
            raise RuntimeError("Timed out waiting for SSH tunnel to controller:16443")

        # ─────────────────────────────────────────────────────── Phase 2 ───────────────────────────────────────────────────────
        print("🚀 Phase 2: provisioning Kubernetes resources…")
        subprocess.run([
            "terraform","apply","-auto-approve", tfvars_arg
        ], cwd=tf_dir, check=True)

        print("✅ All done!")




        # ———————————
        # 9) Run Terraform
        # ———————————
        for cmd in (
            ["terraform", "init"],
            ["terraform", "apply", "-auto-approve", f"-var-file={tfvars_path.name}"],
        ):
            print(f"Running {' '.join(cmd)} in {tf_dir}")
            subprocess.run(cmd, cwd=str(tf_dir), check=True)

        # ———————————
        # 10) Post-apply: re-ensure SSH & re-install key
        # ———————————
        print("🔄 Terraform apply done – re-checking SSH on worker…")
        wait_for_ssh(worker_pub, ssh_key_path)
        subprocess.run([
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-i", str(ssh_key_path),
            f"ubuntu@{worker_pub}",
            install_cmd
        ], check=True)
        print("✔ Public key re-installed on worker (post-apply)")

        # 11) Update Route53 A record
        print("🔑 Updating Route 53 A record…")
        domain = settings.domain.strip()
        if "." not in domain:
            raise RuntimeError(f"Invalid DOMAIN '{domain}'")
        parent = ".".join(domain.split(".")[1:]) + "."
        r53 = sess.client("route53")
        hz = r53.list_hosted_zones_by_name(DNSName=parent, MaxItems="1")["HostedZones"]
        if not hz or hz[0]["Name"] != parent:
            raise RuntimeError(f"No hosted zone for '{parent}'")
        zone_id = hz[0]["Id"].split("/")[-1]
        rc = list(ec2.instances.filter(
            Filters=[{"Name":"tag:Name","Values":["jeeves-k8s-controller"]},
                     {"Name":"instance-state-name","Values":["running"]}]
        ))
        if not rc:
            raise RuntimeError("Controller instance not found")
        rec = {
            "Comment": "Upsert by Jeeves rc_microservices_helm",
            "Changes": [{
                "Action":"UPSERT",
                "ResourceRecordSet":{
                    "Name": domain,
                    "Type":"A",
                    "TTL":60,
                    "ResourceRecords":[{"Value": rc[0].public_ip_address}],
                }
            }]
        }
        resp = r53.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch=rec)
        info = resp.get("ChangeInfo",{})
        print(f"Route53 change: ID={info.get('Id')} Status={info.get('Status')}")


        print("✅ ps-auto-infra Terraform deployment complete!")

def run(**kwargs):
    K8sDeploymentHelm().run()



