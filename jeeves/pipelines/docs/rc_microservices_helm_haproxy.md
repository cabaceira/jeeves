
# Rocket.Chat Microservices Helm Pipeline (Jeeves)

A fully-automated CI/CD pipeline for provisioning AWS infrastructure, bootstrapping a MicroK8s cluster, and deploying Rocket.Chat (with its microservices) via Helm. Jeeves handles Terraform orchestration, SSH-tunneling, certificate provisioning, and Route 53 DNS updates.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)  
2. [Machine Topology & Roles](#machine-topology--roles)  
3. [Terraform Modules](#terraform-modules)  
4. [Pipeline Workflow](#pipeline-workflow)  
5. [Networking & Ingress](#networking--ingress)  
6. [Certificate Management](#certificate-management)  
7. [AWS Automations (Route 53)](#aws-automations-route-53)  
8. [Configuration & Variables](#configuration--variables)  
9. [Troubleshooting & Tips](#troubleshooting--tips)  

---

## Architecture Overview

```
┌──────────┐       ┌─────────┐       ┌─────────┐
│ Route 53 │◀──►   │ HAProxy │◀──►   │ Traefik │◀──► MicroK8s Cluster
└──────────┘       └─────────┘       └─────────┘
                                            │
                                            ▼
           ┌─────────────────────────────────────────────────┐
           │               Rocket.Chat Helm                  │
           │ ┌───────────┐ ┌─────────┐ ┌──────────┐          │
           │ │  NATS     │ │  DDP    │ │  Auth    │     …    │ 
           │ └───────────┘ └─────────┘ └──────────┘.         │
           └─────────────────────────────────────────────────┘
```

1. **AWS Infrastructure**

   * **Controller node** (EC2): runs MicroK8s master, HAProxy edge-router, Certbot.
   * **Worker nodes** (EC2): join the MicroK8s cluster to run Rocket.Chat microservices.
2. **HAProxy** on the controller exposes ports **80** → HTTP redirect, **443** → TLS termination.
3. **Traefik** (as a Kubernetes Deployment) receives traffic from HAProxy via NodePort **30080/30443**, routes to in-cluster services.
4. **Rocket.Chat** microservices (NATS, DDP streamer, authorization, presence, account, stream hub, Rocket.Chat itself) are deployed via Helm into namespace `psautoinfra`.
5. **Route 53** A-record automatically updated to point `<domain>` → controller public IP.

---

## Machine Topology & Roles

### Controller (EC2)

* **Ubuntu** AMI
* **MicroK8s master** installation via Terraform `controller` module
* **Remote-exec** provisioners:

  1. Install HAProxy & snapd/Certbot
  2. Stop HAProxy → bind port 80 for Certbot
  3. Issue/Renew Let's Encrypt cert
  4. Build `haproxy.pem` and start HAProxy
  5. Export `microk8s.config` and set up an SSH‐tunnel on `localhost:16443` for Terraform’s Kubernetes provider

### Workers (EC2)

* **Ubuntu** AMI
* **MicroK8s worker** installation via Terraform `worker` module
* Join cluster by fetching join‐token from the controller

---

## Terraform Modules

### controller

* **Provision EC2** (controller)
* **Install MicroK8s**, enable add-ons (DNS, storage, RBAC)
* **Local-exec**:

  * `scp` down kubeconfig
  * Patch `server: https://localhost:16443` → `127.0.0.1:16443`
  * Spawn SSH tunnel
  * Wait for API readiness

### worker

* **Provision EC2** (worker)
* Remote-exec: join MicroK8s cluster

### haproxy

* **null\_resource.install\_haproxy\_certbot**
* **null\_resource.certbot\_issue** (Let’s Encrypt)
* **null\_resource.haproxy\_deploy** (bundle PEM, config, restart)

### traefik

* Deploy CRDs via `kubectl apply`
* Create ServiceAccount, RBAC, ConfigMap, PV/PVC, Deployment
* Expose NodePort 30080/30443

### rocketchat (Helm)

* Uses the `helm_release` resource: installs `rocketchat` chart into `psautoinfra`
* Exposes on port 80 internally

---

## Pipeline Workflow

1. **Terraform Apply** (via Jenkins/CLI):

   * Spins up controller + workers
   * Waits for SSH tunnel → Kubernetes API
   * Provisions Ingress (Traefik) & HAProxy certs
2. **Helm Deploy**:

   * `helm upgrade --install rc-microservices ./chart`
   * Applies Rocket.Chat micro-services
3. **IngressRoute**:

   * `kubectl apply` Traefik `IngressRoute` YAML for HTTP/HTTPS
4. **Route 53 Update**:

   * Jeeves’ Python pipeline (`route53_update.py`)
   * Boto3 upsert A record for `<domain>` → controller public IP

---

## Networking & Ingress

* **HAProxy**

  * Frontend `http_in`: `*:80`, redirects to `https` when Host matches
  * Frontend `https_in`: `*:443 ssl crt /etc/letsencrypt/live/${DOMAIN}/haproxy.pem`
  * Backend `traefik_backend`: `127.0.0.1:30080` (HTTP)

* **Traefik**

  * EntryPoints `web` (`:80`), `websecure` (`:443`)
  * Providers: KubernetesCRD / Ingress
  * CertificatesResolver: `letsencrypt` with ACME HTTP challenge

---

## Certificate Management

* **Certbot standalone** on controller:

  ```bash
  sudo certbot certonly \
    --non-interactive \
    --agree-tos \
    --keep-until-expiring \
    --email "${var.cert_email}" \
    --standalone \
    -d "${var.domain}"
  ```
* On success:

  * `fullchain.pem`, `privkey.pem` land in `/etc/letsencrypt/live/${DOMAIN}`
  * Terraform‐provisioner copies key + chain into `haproxy.pem` (key first)
  * HAProxy reloads to pick up new cert

---

## AWS Automations (Route 53)

Located in `jeeves/pipelines/route53_update.py`:

```python
class Route53Update(Pipeline):
    def run(self):
        domain = settings.domain
        rc = find_ec2_by_tag("Name", "jeeves-rocketchat")
        public_ip = rc.public_ip_address
        parent = zone_from(domain)
        r53 = session().client("route53")
        zone_id = find_hosted_zone(parent)
        upsert_a_record(zone_id, domain, public_ip)
```

* **When**: after pipeline completes infra + Helm
* **What**: UPSERT A record TTL 60 → controller IP
* Hooks into AWS credentials/permissions from `.env`

---

## Configuration & Variables

* **terraform.tfvars**:

  ```hcl
  controller_ami = "ami-xxx"
  worker_ami     = "ami-yyy"
  domain         = "guru.ps-rocketchat.com"
  cert_email     = "you@example.com"
  ssh_key_path   = "~/.ssh/jeeves.pem"
  ```
* **.env** (for Python pipelines):

  ```
  AWS_ACCESS_KEY_ID=…
  AWS_SECRET_ACCESS_KEY=…
  DOMAIN=guru.ps-rocketchat.com
  ```

---

## Troubleshooting & Tips

* **SSH Tunnel**: ensure no stale `ssh -L 16443` processes; Terraform’s local-exec kills prior tunnels.
* **Let’s Encrypt rate limits**: avoid rapid re-requests; use `--keep-until-expiring` or staging for testing.
* **HAProxy config errors**: run `haproxy -c -f /etc/haproxy/haproxy.cfg` on the controller to catch syntax issues.
* **Route 53 propagation**: after upsert, DNS may take a few seconds—`dig +short <domain>` to verify.
* **Re-runs**: pipeline idempotency relies on Terraform `triggers` (e.g. `sha256(cfg)`), `certbot --keep-until-expiring`, and safe `local_file` usage.

---

*End of documentation.*
