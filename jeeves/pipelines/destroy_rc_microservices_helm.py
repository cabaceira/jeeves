from __future__ import annotations
import pathlib
import subprocess
import time
import threading
from botocore.exceptions import ClientError
from ..pipeline import Pipeline
from ..aws_helpers import session
import shlex

def run_with_timeout(cmd: list[str], timeout: int, cwd: str | None = None) -> bool:
    """Run command with timeout, return True if successful, False if timed out or errored."""
    def target(proc_result):
        try:
            subprocess.run(cmd, cwd=cwd, check=True)
            proc_result.append(True)
        except subprocess.CalledProcessError:
            proc_result.append(False)

    result = []
    thread = threading.Thread(target=target, args=(result,))
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        print(f"⚠️ Command timed out: {' '.join(cmd)}")
        return False
    return result[0]

class K8sDestroyHelm(Pipeline):
    pipeline_name        = "Destroy Rocket.Chat Microservices Deployment with Helm Charts"
    pipeline_description = (
        "Destroy the Three-node Deployment: One MongoDB, One Controller Node and One Worker Node"
    )
    docs_path = pathlib.Path(__file__).parents[2] / "docs" / "destroy_rc_microservices_helm.md"

    def run(self) -> None:
        tf_dir = pathlib.Path(__file__).parents[2] / "ps-auto-infra"
        kubeconfig = tf_dir / "microk8s.config"




        try:
            result = subprocess.run(
                ["helm", "--kubeconfig", str(kubeconfig), "list", "--all-namespaces", "--short", "--output", "json"],
                capture_output=True, text=True, check=True
            )

            # If you want namespace info, parse JSON instead of relying on --short
            list_result = subprocess.run(
                ["helm", "--kubeconfig", str(kubeconfig), "list", "--all-namespaces", "--output", "json"],
                capture_output=True, text=True, check=True
            )
            import json
            releases = json.loads(list_result.stdout)

            for release in releases:
                name = release["name"]
                namespace = release["namespace"]
                print(f"  • Uninstalling Helm release '{name}' in namespace '{namespace}'")
                subprocess.run([
                    "helm", "--kubeconfig", str(kubeconfig), "uninstall", name, "--namespace", namespace
                ], check=False)

        except subprocess.CalledProcessError as e:
            print(f"⚠️ Helm release listing failed: {e}")

        print("🔴 Cleaning up Kubernetes resources…")
        yaml_files = [
            "redirect-to-https.yaml",
            "rocketchat-ingress-http.yaml",
            "rocketchat-ingress-https.yaml",
        ]
        for fn in yaml_files:
            path = tf_dir / fn
            if path.exists():
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
            subprocess.run([
                "kubectl", "--kubeconfig", str(kubeconfig),
                "delete", "-f", url,
                "--ignore-not-found"
            ], check=False)


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

        print("🔴 Cleaning up Security Groups…")
        sg_names = ["jeeves-k8s-mongo", "jeeves-k8s-controller", "jeeves-k8s-worker"]
        for name in sg_names:
            try:
                groups = ec2.describe_security_groups(
                    Filters=[{"Name": "group-name", "Values": [name]}]
                )["SecurityGroups"]
            except ClientError as e:
                print(f"️ Could not describe SG '{name}': {e}")
                continue
            if not groups:
                print(f"SG '{name}' not found, skipping")
                continue

            sg = groups[0]
            sg_id = sg["GroupId"]
            print(f"🧹 Cleaning up SG '{name}' ({sg_id})")

            for attempt in range(1, 11):
                nis = ec2.describe_network_interfaces(
                    Filters=[{"Name": "group-id", "Values": [sg_id]}]
                ).get("NetworkInterfaces", [])
                if not nis:
                    break
                print(f"  • Waiting for {len(nis)} ENI(s) to detach (attempt {attempt}/10)")
                time.sleep(5)

            for other_name in sg_names:
                if other_name == name:
                    continue
                try:
                    other = ec2.describe_security_groups(
                        Filters=[{"Name": "group-name", "Values": [other_name]}]
                    )["SecurityGroups"]
                    if not other:
                        continue
                    other_id = other[0]["GroupId"]

                    ingress = [p for p in other[0].get("IpPermissions", [])
                               if any(g.get("GroupId") == sg_id for g in p.get("UserIdGroupPairs", []))]
                    if ingress:
                        ec2.revoke_security_group_ingress(GroupId=other_id, IpPermissions=ingress)

                    egress = [p for p in other[0].get("IpPermissionsEgress", [])
                              if any(g.get("GroupId") == sg_id for g in p.get("UserIdGroupPairs", []))]
                    if egress:
                        ec2.revoke_security_group_egress(GroupId=other_id, IpPermissions=egress)
                except ClientError:
                    continue

            try:
                ec2.delete_security_group(GroupId=sg_id)
                print(f" Deleted SG '{name}'")
            except ClientError as e:
                print(f" Error deleting SG '{name}': {e}")

        print("\n✅ k8s_deployment_helm destroy complete")

        # ─────────────────────────────────────────────────────────────
        # Final cleanup: Remove Terraform state and cached files
        # ─────────────────────────────────────────────────────────────
        print("🧹 Removing Terraform state files…")
        for fname in ["terraform.tfstate", "terraform.tfstate.backup", ".terraform.lock.hcl"]:
            f = tf_dir / fname
            if f.exists():
                f.unlink()
                print(f"  • Deleted {fname}")

        terraform_dir = tf_dir / ".terraform"
        if terraform_dir.exists() and terraform_dir.is_dir():
            import shutil
            shutil.rmtree(terraform_dir)
            print("  • Deleted .terraform/ directory")


def run(**kwargs):
    K8sDestroyHelm().run()

