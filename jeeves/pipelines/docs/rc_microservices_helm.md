# Rocket.Chat Microservices Helm “Jeeves” Pipeline

A self-service pipeline for deploying Rocket.Chat and its supporting microservices onto a MicroK8s cluster, fully automated from infrastructure provisioning to DNS updates.

---

## 🖥️ Machines & Kubernetes Cluster

* **Controller Node (EC2)**

  * Runs MicroK8s in “single-node” master mode
  * Installs Kubernetes tools, Helm, Traefik, and serves as the kubeconfig source
  * Exposes ports 30080 & 30443 via a Traefik NodePort Service

* **Worker Nodes (EC2 Auto Scaling Group)**

  * Each joins the MicroK8s cluster on bootstrap
  * Labeled for scheduling Rocket.Chat microservices

* **Networking**

  * **Traefik** in `kube-system`

    * EntryPoints: `web` (80) & `websecure` (443) via NodePort
    * Routes based on Host header to Rocket.Chat and metrics
  * **Route53 A Record**

    * Points `rocketchat.<your-domain>` → controller public IP

---

## 📦 Terraform Modules

1. **modules/controller**

   * Provision EC2, install MicroK8s, snapd, Helm
   * Generate `microk8s.config` & establish local SSH tunnel for Terraform’s Kubernetes provider

2. **modules/worker**

   * Provision EC2 workers, install MicroK8s, join cluster via controller token

3. **modules/traefik**

   * Install Traefik CRDs & RBAC
   * Create ConfigMap (ACME resolver, entryPoints)
   * HostPath PV/PVC for ACME storage
   * Deployment + NodePort Service

4. **modules/rocketchat\_release**

   * Helm release of `rocketchat` chart (v6.25.1) into namespace `psautoinfra`
   * Exposes HTTP/HTTPS routes via Traefik IngressRoutes

5. **modules/route53**

   * (In Jeeves pipeline) Python script using Boto3 to UPSERT Route53 A record

---

## 🔍 Architecture Diagram

```text
┌──────────┐        ┌─────────┐        ┌───────────────────┐
│ Route 53 │────────▶ Traefik │────────▶ MicroK8s Cluster │
└──────────┘        └─────────┘        └───────────────────┘
                                              │
      ┌────────────────────────────────────────────────────────┐
      │                Rocket.Chat Microservices             │
      │ ┌────────┐ ┌────────────┐ ┌──────────────┐ ┌───────┐ │
      │ │ NATS   │ │ DDP        │ │ Authorization│ │Account│ │
      │ │ Stream │ │ Stream     │ │ Service      │ │Service│ │
      │ └────────┘ └────────────┘ └──────────────┘ └───────┘ │
      │ ┌──────────┐ ┌───────────┐ ┌───────────────┐         │
      │ │ Presence │ │ Metrics   │ │ Rocket.Chat   │         │
      │ │ Service  │ │ Exporter  │ │ Application   │         │
      │ └──────────┘ └───────────┘ └───────────────┘         │
      └──────────────────────────────────────────────────────┘
```

---

## 🚀 Jeeves Pipelines

### 1. **rc\_microservices\_helm**

* **Purpose:**

  * Builds (if needed) Docker images for custom microservices
  * Applies Terraform to provision infra & Kubernetes objects
  * Runs `helm upgrade --install` for Rocket.Chat chart

* **Key Steps:**

  1. **Terraform Apply**

     * Provisions EC2s, MicroK8s, Traefik, and Helm release
  2. **Helm Upgrade**

     * Ensures idempotent rollout of Rocket.Chat & dependencies

### 2. **route53\_update**

* **Purpose:**

  * Ensures DNS A record for `rocketchat.<domain>` points at controller’s public IP
* **Implementation:**

  * Python (Boto3) script:

    ```python
    # Read DOMAIN from settings
    # Discover EC2 by tag "jeeves-rocketchat"
    # Get public IP, then
    r53.change_resource_record_sets(
      HostedZoneId=zone_id,
      ChangeBatch={
        "Comment": "Upsert by Jeeves route53_update pipeline",
        "Changes": [{
          "Action": "UPSERT",
          "ResourceRecordSet": {
            "Name": domain,
            "Type": "A",
            "TTL": 60,
            "ResourceRecords": [{"Value": public_ip}],
          }
        }]
      }
    )
    ```
  * Scheduled automatically as part of the release pipeline

---

## 🔧 AWS Automations & Helpers

* **`aws_helpers.session()`**

  * Creates Boto3 session via IAM-backed Jenkins credentials

* **Jeeves Config**

  * `.env` / `settings` includes:

    ```dotenv
    DOMAIN=rocketchat.example.com
    CERT_EMAIL=ops@example.com
    ENVIRONMENT=staging
    ```

* **Hosted Zone Discovery**

  * Strips subdomain to lookup parent zone

---

## 📚 Helm Chart Highlights

* **Chart:** `rocketchat-6.25.1`
* **Values of Note:**

  * `service.type: ClusterIP` on port `80`
  * `ingress.enabled: false` (we manage via Traefik IngressRoutes)
  * Image tags pinned to desired Rocket.Chat version
* **Generated Service:**

  ```yaml
  apiVersion: v1
  kind: Service
  metadata:
    name: psautoinframk8s-rocketchat
  spec:
    type: ClusterIP
    ports:
      - name: http; port: 80; targetPort: http
    selector:
      app.kubernetes.io/name: rocketchat
  ```

---

## ✅ Day-2 Operations

* **Certificate Renewal**

  * Traefik ACME handles its own TLS cert rotation
  * No HAProxy; no custom cert hooks needed

* **Scaling**

  * Increase worker ASG size
  * Bump `helm_for_each` replicas for high-availability

* **Monitoring**

  * Prometheus scrapes Traefik metrics (port 9100)
  * Rocket.Chat health endpoints via Traefik ping

* **Cleanup**

  * Terraform `destroy` will tear down everything end-to-end

---

> **Ready to roll?**
>
> 1. Confirm your `.env` and AWS credentials.
> 2. Run `jeeves rc_microservices_helm` to provision & deploy.
> 3. Verify DNS & HTTPS via `https://rocketchat.<domain>`.
