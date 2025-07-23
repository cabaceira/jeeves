# Rocket.Chat & MongoDB Docker Pipeline Documentation

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Configuration](#configuration)

   * [Environment Variables](#environment-variables)
   * [Settings and Defaults](#settings-and-defaults)
5. [Running the Pipeline](#running-the-pipeline)
6. [Step-by-Step Execution](#step-by-step-execution)

   1. [SSH Key Setup](#ssh-key-setup)
   2. [AWS Client & KeyPair Import](#aws-client--keypair-import)
   3. [Default VPC & Subnet Discovery](#default-vpc--subnet-discovery)
   4. [Security Groups Configuration](#security-groups-configuration)
   5. [MongoDB EC2 Instance Provisioning](#mongodb-ec2-instance-provisioning)
   6. [MongoDB Bootstrap](#mongodb-bootstrap)
   7. [Rocket.Chat EC2 Instance Provisioning](#rocket-chat-ec2-instance-provisioning)
   8. [Rocket.Chat Bootstrap & Traefik](#rocket-chat-bootstrap--traefik)
   9. [DNS Update & Propagation](#dns-update--propagation)
   10. [Final Summary & SSH Access](#final-summary--ssh-access)
7. [Bootstrap Scripts](#bootstrap-scripts)

   * [mongodb\_bootstrap.sh](#mongodb_bootstrapsh)
   * [rocket\_chat\_ec2\_bootstrap.sh](#rocket_chat_ec2_bootstrapsh)
8. [Troubleshooting & Tips](#troubleshooting--tips)
9. [Cleanup](#cleanup)

---

## Overview

This pipeline automates a two-node deployment of Rocket.Chat with MongoDB on AWS EC2 using Docker and Traefik. It performs the following tasks:

* Provisions two EC2 instances in the default VPC:

  * **MongoDB node** (`jeeves-mongo`)
  * **Rocket.Chat node** (`jeeves-rocketchat`)
* Configures Security Groups for intra-node communication and public access
* Bootstraps MongoDB on the database node via non-interactive SSH
* Bootstraps Rocket.Chat + Traefik (with Let's Encrypt TLS) on the application node via non-interactive SSH
* Updates DNS in Route 53 and waits for propagation

## Architecture

```plaintext
┌────────────┐       ┌────────────┐
│            │       │            │
│  Internet  │       │  Internet  │
│  (Users)   │       │  AWS API   │
└──────┬─────┘       └──────┬─────┘
       │                      │
       ▼                      ▼
┌────────────┐        ┌────────────┐
│  ALB/DNS   │◀──────▶│ Route 53   │
│ (Rocket)   │        └────────────┘
└──────┬─────┘
       │
       ▼
┌──────────────────────────────┐
│ EC2 Instance: Rocket.Chat    │
│ - Traefik (80,443)           │
│ - Docker Compose             │
│ - Docker network `web`       │
│ - Rocket.Chat replicas       │
└──────┬───────────────────────┘
       │
   27017│
       ▼
┌──────────────────────────────┐
│ EC2 Instance: MongoDB        │
│ - MongoDB ReplicaSet `rs0`   │
│ - auth enabled               │
└──────────────────────────────┘
```

## Prerequisites

1. **AWS Credentials**

   * `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` configured in environment or AWS CLI
2. **SSH Key Pair**

   * `SSH_KEY_NAME`: name of the key pair in AWS
   * `SSH_KEY_PATH`: path to the private key file (`~/.ssh/id_rsa`)
   * `SSH_PUBLIC_KEY_PATH`: path to the public key file (`~/.ssh/id_rsa.pub`)
3. **Route 53 Configuration**

   * Hosted zone for your domain
   * Appropriate IAM permissions to update DNS records
4. **Python Dependencies**

   * `boto3`, `botocore` for AWS interactions
   * `jeeves` package installed, with `aws_helpers` and `pipeline` modules

## Configuration

### Environment Variables

Create a `.env` file (or export in your shell) with the following:

```bash
# SSH
SSH_KEY_NAME=your-key-name
SSH_KEY_PATH=~/.ssh/your-key.pem
SSH_PUBLIC_KEY_PATH=~/.ssh/your-key.pub

# MongoDB Credentials
MONGO_USERNAME=rc_admin
MONGO_PASSWORD=supersecret
REPLSET_NAME=rs0       # optional, defaults to rs0
MONGO_PORT=27017       # optional, defaults to 27017

# Rocket.Chat & Traefik
RELEASE=7.7.4
IMAGE=registry.rocket.chat/rocketchat/rocket.chat
TRAEFIK_RELEASE=v2.9.8
ROOT_URL=https://chat.example.com
DOMAIN=chat.example.com
LETSENCRYPT_EMAIL=admin@example.com
ROCKETCHAT_SCALE=4     # optional, defaults to 4

# Jeeves Settings (in config/settings.py)
# default_os_version: e.g. "24.04"
# default_instance_type: e.g. "t3.medium"
# domain: must match DOMAIN above
```

### Settings and Defaults

| Setting                 | Default      | Description                      |
| ----------------------- | ------------ | -------------------------------- |
| `default_os_version`    | `24.04`      | Ubuntu release for EC2 instances |
| `default_instance_type` | `t3.medium`  | EC2 instance type                |
| `domain`                | *(required)* | Must match `DOMAIN` env var      |

## Running the Pipeline

```bash
# Activate your virtualenv / install jeeves
pip install -e .

# Export env vars or source .env
set -a && source .env && set +a

# Execute the pipeline
jeevectl run rc_mongo_docker
```

## Step-by-Step Execution

### 1. SSH Key Setup

* Verifies that `SSH_KEY_NAME`, `SSH_KEY_PATH`, and `SSH_PUBLIC_KEY_PATH` are set and exist
* Imports the public key into EC2 if not already present

### 2. AWS Client & KeyPair Import

* Creates a Boto3 session via `aws_helpers.session()`
* Checks for existing KeyPair; imports if `InvalidKeyPair.NotFound`

### 3. Default VPC & Subnet Discovery

* Finds the default VPC (`isDefault=true`)
* Selects the first available subnet

### 4. Security Groups Configuration

1. **jeeves-basic** (MongoDB SG):

   * Ingress TCP 22 open to `0.0.0.0/0`
   * Intra-SG TCP 27017 (MongoDB) only from Rocket.Chat SG
2. **jeeves-rc** (Rocket.Chat SG):

   * Ingress TCP 22, 80, 443 open to `0.0.0.0/0`
3. Allows Rocket.Chat SG to connect to MongoDB SG on port 27017

### 5. MongoDB EC2 Instance Provisioning

* Searches for instances tagged `Name=jeeves-mongo` in states `pending`, `running`, or `stopped`
* Reuses or starts an existing instance, or launches a new one:

  * AMI: Ubuntu (via `latest_ubuntu_ami`)
  * InstanceType: `settings.default_instance_type`
  * KeyName: `SSH_KEY_NAME`
  * SecurityGroup: `jeeves-basic`
  * UserData: Placeholder (no-op)
  * Tags: `Name=jeeves-mongo`, `Project=jeeves`, `Role=mongo-node`, `Deployment=<name>`

### 6. MongoDB Bootstrap

* Waits for SSH (port 22) to become available
* Runs `scripts/mongodb_bootstrap.sh` via SSH, passing:

  * `MONGO_PORT`, `REPLSET_NAME`, `MONGO_USERNAME`, `MONGO_PASSWORD`
* Script installs MongoDB, configures replica set, enables authentication

### 7. Rocket.Chat EC2 Instance Provisioning

* Same pattern as MongoDB, but tagged `jeeves-rocketchat` and SG `jeeves-rc`
* Captures public IP for Rocket.Chat host

### 8. Rocket.Chat Bootstrap & Traefik

* Waits for SSH on Rocket.Chat host
* Runs `scripts/rocket_chat_ec2_bootstrap.sh` via SSH, passing:

  * `MONGO_*`, `RELEASE`, `IMAGE`, `TRAEFIK_RELEASE`, `ROOT_URL`, `DOMAIN`, `LETSENCRYPT_EMAIL`
* Script steps:

  1. Install Docker Engine
  2. Create Docker network `web`
  3. Generate `docker-compose.yml` with:

     * **traefik** service (ports 80/443, HTTP→HTTPS, ACME resolver)
     * **rocketchat** service (environment, volumes, labels)
  4. `docker compose up -d --scale rocketchat=1`
  5. Verify Traefik HTTP endpoint and DNS record
  6. Validate TLS certificate via HTTPS
  7. Scale Rocket.Chat to `${ROCKETCHAT_SCALE}` replicas

### 9. DNS Update & Propagation

* Invokes `Route53Update` pipeline to upsert A record for `DOMAIN`
* Polls DNS until the record resolves to Rocket.Chat public IP

### 10. Final Summary & SSH Access

* Prints JSON summary of instance IDs and IPs
* Provides an SSH command for Rocket.Chat node

## Bootstrap Scripts

### `mongodb_bootstrap.sh`

> See `scripts/mongodb_bootstrap.sh` for full details. Key points:

* **Required ENV:** `MONGO_PORT`, `REPLSET_NAME`, `MONGO_USERNAME`, `MONGO_PASSWORD`
* Installs `mongodb-org` from official repos
* Configures `mongod.conf` with replica set and network bindings
* Initiates replica set with `rs.initiate()`
* Creates admin user in `admin` database

### `rocket_chat_ec2_bootstrap.sh`

> See `scripts/rocket_chat_ec2_bootstrap.sh` for full details. Key points:

1. **ENV Validation:** Exits if any required variable (e.g. `RELEASE`, `IMAGE`, `LETSENCRYPT_EMAIL`) is missing
2. **Lock-wait:** Ensures `apt` and `dpkg` locks are cleared before installing packages
3. **Docker Installation:** Adds Docker’s GPG key, repo, and installs via `apt`
4. **Network Creation:** Ensures Docker network `web` exists
5. **Compose File:** Writes `docker-compose.yml` with Traefik and Rocket.Chat definitions
6. **Stack Deployment:** Tears down existing stack, brings up Traefik + one Rocket.Chat, waits for endpoints
7. **TLS Validation:** Verifies valid Let's Encrypt certificate
8. **Scaling:** Adjusts Rocket.Chat replicas to `${ROCKETCHAT_SCALE}`

## Troubleshooting & Tips

* **Timeouts Waiting for SSH:** Ensure security groups allow port 22 and SSH key has correct permissions (`chmod 600`)
* **DNS Propagation Failures:** Confirm Route 53 hosted zone IDs and IAM permissions
* **Certificate Errors:** Check that `DOMAIN` resolves before TLS validation
* **Docker Compose Issues:** Run `docker compose logs` on the Rocket.Chat host for detailed service logs

## Cleanup

To tear down resources:

```bash
# Delete instances
aws ec2 terminate-instances --instance-ids <jeeves-mongo-id> <jeeves-rocketchat-id>

# Delete security groups
aws ec2 delete-security-group --group-name jeeves-basic
aws ec2 delete-security-group --group-name jeeves-rc

# Remove key pair (if imported)
aws ec2 delete-key-pair --key-name your-key-name

# Remove Route 53 record
# Use jeevectl or aws route53 change-resource-record-sets
```

---

*Documentation generated for the `rc_mongo_docker` pipeline.*
