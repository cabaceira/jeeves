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

        # ———————————
        # 8.1) Establish SSH tunnel for Kubernetes API
        # ———————————
        # Configuration
        ssh_key_path = ssh_key_path
        controller_pub = ctrl_pub
        local_port = 16443

        def tunnel_exists(port):
            """Check if any process is listening on the given local port."""
            result = subprocess.run(
                ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            return result.returncode == 0

        def kill_existing_tunnel(port):
            """Kill any existing listener on the given port (use with caution)."""
            subprocess.run(
                f"lsof -tiTCP:{port} | xargs -r kill -9",
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        print(f"🔌 Preparing to start SSH tunnel for K8s API (localhost:{local_port})…")
        if tunnel_exists(local_port):
            print(f"⚠️  Tunnel already exists on localhost:{local_port}, skipping setup.")
        else:
            print(f"🔧 No tunnel found. Cleaning up stale listeners and opening new SSH tunnel to {controller_pub}…")
            kill_existing_tunnel(local_port)  # optional: if stale tunnels might exist
            try:
                subprocess.run([
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-i", ssh_key_path,
                    "-fN",  # go to background, no remote command
                    "-L", f"{local_port}:127.0.0.1:{local_port}",
                    f"ubuntu@{controller_pub}"
                ], check=True)
                print(f"✅ SSH tunnel established on localhost:{local_port} → {controller_pub}")
            except subprocess.CalledProcessError:
                print("❌ Failed to establish SSH tunnel. Continuing anyway — controller may not be up yet.")
            # Give some time before anything depends on it
            time.sleep(5)

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

        # ———————————
        # 9) Run Terraform (infra + k8s install, then full apply)
        # ———————————
        os.environ["KUBE_INSECURE_SKIP_TLS_VERIFY"] = "true"
        print("📦 Running Terraform (infra stage only)...")
        subprocess.run(["terraform", "init"], cwd=str(tf_dir), check=True)

        # Stage 1: Infra + MicroK8s installation (no K8s resources yet)
        infra_targets = [
            "aws_instance.jeeves-mongo-master",
            "aws_instance.jeeves-k8s-controller",
            "aws_instance.jeeves-k8s-worker",
            "module.rocketchat.null_resource.check_existing_pvs",
            "null_resource.microk8s_install",  # <—— this must install MicroK8s!
            # anything else that sets up the controller
        ]
        infra_cmd = [
            "terraform", "apply", "-auto-approve", f"-var-file={tfvars_path.name}"
        ] + sum([["-target", t] for t in infra_targets], [])

        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                subprocess.run(infra_cmd, cwd=str(tf_dir), check=True)
                print("✅ Infra-only Terraform apply succeeded.")
                break
            except subprocess.CalledProcessError:
                if attempt == max_attempts:
                    print("❌ Infra apply failed after retries.")
                    raise
                print(f"⚠️ Infra apply failed on attempt {attempt}, retrying in 20 seconds…")
                time.sleep(20)

        # 🕒 Wait for MicroK8s install to complete on controller
        print("🕒 Giving MicroK8s 10s to settle before fetching kubeconfig…")
        time.sleep(10)

        # 🧾 Fetch MicroK8s kubeconfig from controller
        print("📥 Fetching MicroK8s kubeconfig from controller...")
        remote_cmd = "microk8s config"
        result = subprocess.run([
            "ssh", "-o", "StrictHostKeyChecking=no", "-i", str(ssh_key_path),
            f"ubuntu@{ctrl_pub}", remote_cmd
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"❌ Failed to fetch kubeconfig:\n{result.stderr}")

        kubeconfig_path = tf_dir / "microk8s.config"
        with open(kubeconfig_path, "w") as f:
            f.write(result.stdout)
        print(f"✅ Wrote kubeconfig to {kubeconfig_path}")
        tfvars["kube_config_path"] = str(kubeconfig_path.resolve())

        # ----------------------------------------
        # PATCH kubeconfig to use localhost for SSH tunnel
        # ----------------------------------------
        kubeconfig_path = tf_dir / "microk8s.config"
        controller_private_ip = ctrl_pri  # already available earlier

        print("🩹 Patching microk8s.config to use localhost (for SSH tunnel)...")
        with open(kubeconfig_path, "r+") as f:
            content = f.read()
            before_patch = f"https://{controller_private_ip}:16443"
            after_patch = "https://127.0.0.1:16443"
            if before_patch in content:
                print(f"🔁 Replacing '{before_patch}' → '{after_patch}'")
                content = content.replace(before_patch, after_patch)
                f.seek(0)
                f.write(content)
                f.truncate()
                print("✅ microk8s.config patched for local access")
            else:
                print("✅ No need to patch, already pointing to localhost")



        # Run just the MicroK8s wait resource
        subprocess.run([
            "terraform", "apply", "-auto-approve",
            "-target=null_resource.wait_for_microk8s_ready",
            f"-var-file={tfvars_path.name}"
        ], cwd=str(tf_dir), check=True)

        # ✅ Double-check API is actually ready using kubectl (from kubeconfig)
        print("🩺 Confirming kube-apiserver is truly accepting requests...")
        for i in range(30):
            result = subprocess.run([
                "kubectl",
                "--kubeconfig", tfvars["kube_config_path"],
                "get", "namespace", "kube-system"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode == 0:
                print("✅ kube-apiserver is responsive.")
                break
            print(f"⌛ Attempt {i+1}/30: kube-apiserver still warming up...")
            time.sleep(5)
        else:
            raise RuntimeError("❌ kube-apiserver did not become ready in time")


        # Stage 2: Full apply including Kubernetes resources, with retry on failure
        print("🚀 Running full Terraform apply (K8s stage, with retry)...")
        apply_cmd = ["terraform", "apply", "-auto-approve", f"-var-file={tfvars_path.name}"]

        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                subprocess.run(apply_cmd, cwd=str(tf_dir), check=True)
                print("✅ Full Terraform apply succeeded.")
                break
            except subprocess.CalledProcessError as e:
                if attempt == max_attempts:
                    print("❌ Final Terraform apply attempt failed.")
                    raise
                print(f"⚠️ Terraform apply failed on attempt {attempt}, retrying in 20 seconds…")
                time.sleep(20)

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




        print("✅ ps-auto-infra Terraform deployment complete!")

def run(**kwargs):
    K8sDeploymentHelm().run()



