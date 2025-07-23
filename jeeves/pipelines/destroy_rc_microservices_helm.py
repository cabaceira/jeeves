# jeeves/pipelines/destroy_rc_microservices_helm.py

from __future__ import annotations
import pathlib
import subprocess
import time

from botocore.exceptions import ClientError
from ..pipeline import Pipeline
from ..aws_helpers import session

class K8sDestroyHelm(Pipeline):
    pipeline_name        = "Destroy Rocket.Chat Microservices Deployment with Helm Charts "
    pipeline_description = "Destroy the Three-node Deployment. One MongoDB, One Controller Node and one Worker Node"
    docs_path            = pathlib.Path(__file__).parents[2] / "docs" / "destroy_rc_microservices_helm.md"
    """
    Tear down the MicroK8s + Helm deployment:
      0) Clean up k8s objects applied via kubectl
      1) Terraform destroy
      2) Terminate EC2 instances
      3) Delete security groups (with existence checks and retry limit)
    """

    def run(self) -> None:
        tf_dir = pathlib.Path(__file__).parents[2] / "ps-auto-infra"
        kubeconfig = tf_dir / "microk8s.config"

        # 0) Kubernetes cleanup
        print("🔴 Cleaning up Kubernetes resources…")
        yaml_files = [
            "redirect-to-https.yaml",
            "rocketchat-ingress-http.yaml",
            "rocketchat-ingress-https.yaml",
        ]
        for fn in yaml_files:
            path = tf_dir / fn
            if path.exists():
                print(f"  • Deleting {fn}")
                subprocess.run([
                    "kubectl", "--kubeconfig", str(kubeconfig),
                    "delete", "-f", str(path),
                    "--ignore-not-found"
                ], check=False)

        crd_urls = [
            "https://raw.githubusercontent.com/traefik/traefik/v3.3/docs/content/reference/dynamic-configuration/kubernetes-crd-definition-v1.yml",
            "https://raw.githubusercontent.com/traefik/traefik/v3.3/docs/content/reference/dynamic-configuration/kubernetes-crd-rbac.yml",
        ]
        for url in crd_urls:
            print(f"  • Deleting CRDs from {url}")
            subprocess.run([
                "kubectl", "--kubeconfig", str(kubeconfig),
                "delete", "-f", url,
                "--ignore-not-found"
            ], check=False)

        # 1) Terraform destroy
        if tf_dir.exists():
            print(f"🔴 Running terraform destroy in {tf_dir} …")
            subprocess.run(
                ["terraform", "destroy", "-auto-approve", "-var-file=terraform.tfvars"],
                cwd=str(tf_dir), check=True
            )
            print("✔ Terraform destroy complete")
        else:
            print(f"⚠️ Terraform directory not found at {tf_dir}, skipping destroy")

        # 2) Terminate EC2 instances
        sess = session()
        ec2 = sess.client("ec2")
        names = ["jeeves-mongo-master", "jeeves-k8s-controller", "jeeves-k8s-worker"]
        to_terminate: list[str] = []
        for name in names:
            resp = ec2.describe_instances(
                Filters=[
                    {"Name": "tag:Name", "Values": [name]},
                    {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]},
                ]
            )
            for r in resp.get("Reservations", []):
                for inst in r.get("Instances", []):
                    iid = inst["InstanceId"]
                    state = inst["State"]["Name"]
                    print(f"Found instance {name}: {iid} ({state}), scheduling termination")
                    to_terminate.append(iid)
        if to_terminate:
            ec2.terminate_instances(InstanceIds=to_terminate)
            waiter = ec2.get_waiter("instance_terminated")
            print(f"Waiting for {len(to_terminate)} instance(s) to terminate…")
            waiter.wait(InstanceIds=to_terminate)
            print("✔ All instances terminated")
        else:
            print("No Jeeves-managed instances found, skipping termination")

        # 3) Delete security groups safely
        print("🔴 Cleaning up Security Groups…")
        sg_names = ["jeeves-k8s-mongo", "jeeves-k8s-controller", "jeeves-k8s-worker"]
        for name in sg_names:
            try:
                groups = ec2.describe_security_groups(
                    Filters=[{"Name": "group-name", "Values": [name]}]
                )["SecurityGroups"]
            except ClientError as e:
                print(f"⚠️ Could not describe SG '{name}': {e}")
                continue
            if not groups:
                print(f"SG '{name}' not found, skipping")
                continue
            sg = groups[0]
            sg_id = sg["GroupId"]
            print(f"🧹 Cleaning up SG '{name}' ({sg_id})")

            # a) wait for ENIs to detach (max 10 retries)
            max_retries = 10
            for attempt in range(1, max_retries + 1):
                nis = ec2.describe_network_interfaces(
                    Filters=[{"Name": "group-id", "Values": [sg_id]}]
                ).get("NetworkInterfaces", [])
                if not nis:
                    break
                print(f"  • Waiting for {len(nis)} ENI(s) to detach (attempt {attempt}/{max_retries})")
                time.sleep(5)
            else:
                print(f"  ⚠️ Timed out waiting for ENIs to detach from {sg_id}, proceeding anyway")

            # b) revoke cross-SG rules
            for other_name in sg_names:
                if other_name == name:
                    continue
                try:
                    other_list = ec2.describe_security_groups(
                        Filters=[{"Name": "group-name", "Values": [other_name]}]
                    )["SecurityGroups"]
                except ClientError as e:
                    print(f"    ⚠️ Could not describe SG '{other_name}': {e}")
                    continue
                if not other_list:
                    print(f"    • Other SG '{other_name}' not found, skipping")
                    continue
                other = other_list[0]
                other_id = other["GroupId"]

                ingress = [perm for perm in other.get("IpPermissions", [])
                           if any(pair.get("GroupId") == sg_id for pair in perm.get("UserIdGroupPairs", []))]
                if ingress:
                    print(f"    • Revoking ingress in '{other_name}' referencing {sg_id}")
                    try:
                        ec2.revoke_security_group_ingress(GroupId=other_id, IpPermissions=ingress)
                    except ClientError as e:
                        print(f"      ⚠️ Failed revoke ingress on '{other_name}': {e}")
                egress = [perm for perm in other.get("IpPermissionsEgress", [])
                          if any(pair.get("GroupId") == sg_id for pair in perm.get("UserIdGroupPairs", []))]
                if egress:
                    print(f"    • Revoking egress in '{other_name}' referencing {sg_id}")
                    try:
                        ec2.revoke_security_group_egress(GroupId=other_id, IpPermissions=egress)
                    except ClientError as e:
                        print(f"      ⚠️ Failed revoke egress on '{other_name}': {e}")

            # c) delete SG
            try:
                ec2.delete_security_group(GroupId=sg_id)
                print(f"✔ Deleted SG '{name}'")
            except ClientError as e:
                print(f"⚠️ Error deleting SG '{name}': {e}")

        print("\n✅ k8s_deployment_helm destroy complete")

def run(**kwargs):
    K8sDestroyHelm().run()
