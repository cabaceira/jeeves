
# Jeeves

![](jeeves.png)

**Jeeves** is a Python-based, pipeline-driven provisioning tool for deploying and managing AWS-backed Rocket.Chat environments.  Its modular architecture lets you target different deployment backends (Docker, Kubernetes + Helm, etc.) simply by adding or extending pipelines. You can reach out to luis.cabaceira@rocket.chat in open for support and clarifications.

---

## 📂 Project Layout

```

jeeves/
├── config.py               # Load settings from environment / defaults
├── pipeline.py             # Base Pipeline abstraction
├── pipelines/              # Individual deployment & destroy pipelines
│   ├── basic\_deployment\_docker.py
│   ├── destroy\_basic\_docker.py
│   ├── k8s\_deployment\_helm.py
│   ├── destroy\_rc\_microservices\_helm.py
│   └── destroy\_rc\_mongo\_docker.py
└── scripts/                # Bootstrap & helper scripts executed on instances
├── rocketchat\_bootstrap.sh
├── mongodb\_bootstrap.sh
├── microk8s\_controller\_bootstrap.sh
├── microk8s\_worker\_bootstrap.sh
└── deploy\_rocketchat\_helm.sh

```

- **config.py**  
  Defines a `Settings` dataclass, reading environment variables (AWS region, OS version, instance type, domain, email, etc.) with sane defaults.
- **pipeline.py**  
  Declares the abstract `Pipeline` base class (must implement `run()`), plus common helpers for logging and error handling.
- **pipelines/**  
  - **basic_deployment_docker.py**: Launches Rocket.Chat + MongoDB in Docker on a single EC2.  
  - **k8s_deployment_helm.py**: Spins up a MicroK8s controller + worker nodes, installs Traefik, and deploys Rocket.Chat via Helm.  
  - **destroy_\*.py**: Tear-down pipelines for each deployment type—terminates instances, removes security groups, and (for K8s) cleans up cluster objects.
- **scripts/**  
  Idempotent `bash` scripts (with `set -euo pipefail`) that run inside EC2 user-data to bootstrap services.

---

## ⚙️ Configuration

Jeeves reads all settings from environment variables. You can export your overrides or drop them into a `.env` file:

| Variable                 | Purpose                                                | Default         |
|--------------------------|--------------------------------------------------------|-----------------|
| `AWS_DEFAULT_REGION`     | AWS region for all operations                          | `us-east-1`     |
| `DEFAULT_OS_VERSION`     | Ubuntu release for EC2 AMI lookup                      | `24.04`         |
| `DEFAULT_INSTANCE_TYPE`  | EC2 instance flavor                                    | `t2.xlarge`     |
| `DOMAIN`                 | Public domain for Rocket.Chat & Traefik                | `""`            |
| `LETSENCRYPT_EMAIL`      | Email for ACME / Let’s Encrypt registration            | `""`            |
| `K8S_NAMESPACE`          | Kubernetes namespace for Rocket.Chat                   | `rocketchat`    |
| `WORKER_HA`              | Whether to launch multiple worker nodes (true/false)   | `false`         |

---

## 🚀 Pipelines

Each pipeline subclasses `Pipeline` and implements `run()`—the ordered steps for provisioning or teardown.

### 1. Basic Docker Deployment

- **File**: `pipelines/basic_deployment_docker.py`  
- **Flow**:
  1. Launch EC2 instance tagged `jeeves-mongo` and `jeeves-rocketchat`.  
  2. Install Docker & Docker Compose via `mongo_bootstrap.sh` and `rocketchat_bootstrap.sh`.  
  3. Run Rocket.Chat + MongoDB using Docker Compose.  
  4. Output the public IP & connection instructions.

### 2. Kubernetes + Helm Deployment

- **File**: `pipelines/k8s_deployment_helm.py`  
- **Flow**:
  1. Establish AWS session & find the latest Ubuntu AMI.  
  2. Create VPC/Subnets/Security Group (via Terraform).  
  3. Launch MicroK8s controller & worker EC2s, bootstrap with `microk8s_*_bootstrap.sh`.  
  4. Install Traefik CRDs + RBAC.  
  5. Deploy Traefik (IngressRoute, ACME, HTTP→HTTPS redirect).  
  6. Deploy Rocket.Chat Helm chart (`deploy_rocketchat_helm.sh`).  
  7. Wait for Traefik to obtain a Let's Encrypt certificate.  
  8. Print JSON summary of all resources.

### 3. Tear-down Pipelines

- **`destroy_rc_mongo_docker.py`**  
  - Terminates instances tagged `jeeves-mongo` & `jeeves-rocketchat`.  
  - Deletes the `jeeves-basic` security group.

- **`destroy_rc_microservices_helm.py`**  
  - Deletes Traefik CRDs, Middleware, and IngressRoutes via `kubectl`.  
  - Runs `terraform destroy`.  
  - Terminates controller & worker instances.  
  - Cleans up security groups with ENI-detachment retries and rule revocations.
<<<<<<< HEAD

All destroy scripts are **idempotent**: they check for resource existence, ignore “not found” errors, and retry or skip gracefully.

---

