# Destroy Pipeline: `destroy_rc_microservices_helm`

This document describes the steps performed by the `destroy_rc_microservices_helm.py` pipeline in Jeeves. Its goal is to tear down the entire MicroK8s + Helm–deployed Rocket.Chat stack and related AWS resources in a safe, idempotent, and error-tolerant way.

---

## Overview of Steps

1. **Kubernetes Cleanup**  
   Deletes Traefik CRDs, Middleware, and IngressRoute objects applied via `kubectl`.
2. **Terraform Destroy**  
   Invokes `terraform destroy` in the `ps-auto-infra` directory to remove all Terraform-managed infra.
3. **Terminate EC2 Instances**  
   Finds and terminates EC2 instances tagged:  
   - `jeeves-mongo-master`  
   - `jeeves-k8s-controller`  
   - `jeeves-k8s-worker`
4. **Security Group Cleanup**  
   Safely removes AWS Security Groups (`jeeves-k8s-mongo`, `jeeves-k8s-controller`, `jeeves-k8s-worker`) by:  
   - Waiting for attached ENIs to detach (up to 10 retries)  
   - Revoking any cross-SG ingress/egress rules  
   - Deleting the SG (skipping or logging errors on dependency violations)

---

## 1. Kubernetes Resource Deletion

```python
yaml_files = [
    "redirect-to-https.yaml",
    "rocketchat-ingress-http.yaml",
    "rocketchat-ingress-https.yaml",
]
for fn in yaml_files:
    subprocess.run([
        "kubectl", "--kubeconfig", str(kubeconfig),
        "delete", "-f", str(tf_dir / fn),
        "--ignore-not-found"
    ], check=False)
