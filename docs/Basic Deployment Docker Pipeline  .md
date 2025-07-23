
# Basic Deployment Docker Pipeline  
**Location:** `jeeves/pipelines/basic_deployment_docker.py`

---

## 🚀 Overview

The **`basic_deployment_docker`** pipeline automates a two-node AWS deployment:

1. **MongoDB node** (Ubuntu 24.04, t3a.medium)  
2. **Rocket.Chat node** (Ubuntu 24.04, t3a.medium + Docker/Traefik)

It will:

- **Find or create** EC2 instances tagged `jeeves-mongo` and `jeeves-rocketchat`.  
- **Import** your SSH key‐pair into AWS if missing.  
- **Configure** a shared Security Group (`jeeves-basic`) permitting SSH from anywhere and Mongo traffic only between the two nodes.  
- **Wait** for SSH on each host, then **SSH in** and run non-interactive bootstrap scripts under `scripts/`:  
  - `mongodb_bootstrap.sh`  
  - `rocket_chat_ec2_bootstrap.sh`  
- Be **idempotent**—safe to rerun without re-creating or re-installing.

---

## 📋 Prerequisites

1. **AWS account & IAM user/role** with permissions to manage EC2, Security Groups, KeyPairs.  
2. **Python 3.9+** environment with your virtualenv activated.  
3. **Editable install** of Jeeves:

   ```bash
   pip install -e .
````

4. **Bootstrap scripts** present and executable under:

   ```
   scripts/mongodb_bootstrap.sh
   scripts/rocket_chat_ec2_bootstrap.sh
   ```

5. **`.env`** file at project root (loaded with `set -a && source .env && set +a`).

---

## ⚙️ Configuration (`.env`)

| Variable                  | Description                                                                       |
| ------------------------- | --------------------------------------------------------------------------------- |
| `AWS_ACCESS_KEY_ID`       | Your AWS access key ID                                                            |
| `AWS_SECRET_ACCESS_KEY`   | Your AWS secret access key                                                        |
| `AWS_SESSION_TOKEN` (opt) | If using temporary STS credentials                                                |
| `AWS_REGION`              | AWS region (e.g. `us-east-1`)                                                     |
| `DEFAULT_OS_VERSION`      | Ubuntu version for AMI lookup (`24.04`)                                           |
| `DEFAULT_INSTANCE_TYPE`   | EC2 instance type (`t3a.medium`)                                                  |
| **SSH & KeyPair**         |                                                                                   |
| `SSH_KEY_NAME`            | Name of your AWS KeyPair (no `.pem` suffix)                                       |
| `SSH_KEY_PATH`            | Local path to your private key (`.pem`)                                           |
| `SSH_PUBLIC_KEY_PATH`     | Local path to the matching public key (`.pub`)                                    |
| **MongoDB**               |                                                                                   |
| `MONGO_USERNAME`          | Admin user for MongoDB                                                            |
| `MONGO_PASSWORD`          | Admin password                                                                    |
| `MONGO_PORT` (opt)        | MongoDB port (`27017` by default)                                                 |
| `REPLSET_NAME` (opt)      | Replica‐set name (`rs0` by default)                                               |
| **Rocket.Chat & Traefik** |                                                                                   |
| `RELEASE`                 | Rocket.Chat Docker image tag (e.g. `7.7.4`)                                       |
| `IMAGE`                   | Rocket.Chat image repository (e.g. `registry.rocket.chat/rocketchat/rocket.chat`) |
| `TRAEFIK_RELEASE`         | Traefik Docker image tag (e.g. `v2.9.8`)                                          |
| `ROOT_URL`                | Public URL (e.g. `https://chat.example.com`)                                      |
| `DOMAIN`                  | Hostname for Traefik routing (e.g. `chat.example.com`)                            |
| `LETSENCRYPT_EMAIL`       | ACME email for TLS issuance                                                       |

> **Note:** You do *not* need to set `MONGO_HOST` in `.env`.  The pipeline discovers the Mongo private IP at runtime.

---

## 🔍 Pipeline Steps

### 1. SSH KeyPair Handling

* Reads `SSH_KEY_NAME`, `SSH_KEY_PATH`, `SSH_PUBLIC_KEY_PATH`.
* Validates local files exist.
* Calls `ec2.describe_key_pairs` and **imports** the public key if AWS KeyPair is missing.

### 2. Security Group (`jeeves-basic`)

* Looks up your **default VPC**.
* **Reuses** or **creates** a Security Group named `jeeves-basic` in that VPC.
* Ensures ingress rules:

  * **SSH** (`tcp/22`) from `0.0.0.0/0`.
  * **MongoDB** (`tcp/27017`) only between members of `jeeves-basic`.

### 3. MongoDB EC2 Instance

* **Searches** for any EC2 tagged `Name=jeeves-mongo` in states `pending|running|stopped`.

  * If **stopped**, starts it; if **running**, reuses it.
* If none found, **launches** a new t3a.medium Ubuntu 24.04 instance with:

  * `AssociatePublicIpAddress=True` (so you can SSH in).
  * `UserData` stub (`#!/usr/bin/env bash\nexit 0`)—we’ll SSH in instead.
* **Waits** for the instance to reach **running** state.

### 4. MongoDB Bootstrap (over SSH)

* **Waits** for TCP port 22 on the Mongo host.
* **Prepares** a combined script:

  1. `export MONGO_PORT=…`, `REPLSET_NAME=…`, `MONGO_USERNAME=…`, `MONGO_PASSWORD=…`
  2. Contents of `scripts/mongodb_bootstrap.sh`
* **SSH** to `ubuntu@<mongo_ip>` and runs `sudo bash -s`, feeding the combined script via STDIN.
* The bootstrap script is fully **non-interactive** and **idempotent**:

  * Skips package installs if `mongod` is present.
  * Detects and preserves firewall, config templates, replica-set, admin user, keyfile.

### 5. Rocket.Chat EC2 Instance

* **Searches** for any EC2 tagged `Name=jeeves-rocketchat`.
* **Reuses** or **starts** it if found; otherwise **launches** a new t3a.medium Ubuntu 24.04 with a stub `UserData` as above.

### 6. Rocket.Chat & Traefik Bootstrap (over SSH)

* **Waits** for TCP port 22 on the Rocket.Chat host.
* **Prepares** a combined script:

  1. `export MONGO_USERNAME=…`, `export MONGO_PASSWORD=…`, `export MONGO_HOST=<mongo_private_ip>`, etc.
  2. Contents of `scripts/rocket_chat_ec2_bootstrap.sh`
* **SSH** to `ubuntu@<rc_ip>` and runs `sudo bash -s`, feeding the script.
* The Rocket.Chat script installs Docker, creates a Docker network, writes `docker-compose.yml`, and brings up Traefik + Rocket.Chat (scale 4) in a single `docker compose up -d`.

### 7. Final Summary & SSH Hint

At the end, the pipeline prints:

* A **JSON summary** with both instance IDs and IPs.
* A ready-to-copy SSH command for the Rocket.Chat node:

  ```bash
  ssh -i $SSH_KEY_PATH ubuntu@<rocket_chat_public_ip>
  ```

---

## 🔄 Idempotency & Re-runs

* **Security Group**: reused if already exists, permissions deduplicated.
* **KeyPair**: only imported once.
* **EC2 Instances**: reused by tag, or started if stopped.
* **Bootstrap scripts**: detect their own state and skip already-completed steps.
* **Safe to re-run** anytime; only missing pieces will be installed/fixed.

---

## 🛠 Troubleshooting

* **Logs on EC2**:

  ```
  sudo cat /var/log/cloud-init-output.log
  ```
* **SSH access**: Use the printed SSH hint.
* **Pipeline errors**: Shown in your local terminal—check stack traces and ensure your `.env` is correct.

---

## ⏭ Next Steps

* To **tear down** everything, use the `destroy_stack` or your own cleanup pipeline.
* Explore the **K8s/Helm** pipeline: `jeeves pipelines run k8s_deployment_helm`.
* Customize VPC, subnets, instance types, scaling, etc., via `config.py` or new pipelines.

Happy provisioning! 🎉
