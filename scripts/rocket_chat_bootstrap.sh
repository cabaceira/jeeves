#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Rocket.Chat + Traefik bootstrapper (Docker) – Ubuntu 24.04
# Fully driven by environment variables, no hard-coded secrets.
# -----------------------------------------------------------------------------
set -euo pipefail

############################
# 0. Required ENV VARS     #
############################
: "${RELEASE:?Environment variable RELEASE is required (e.g. 7.7.4)}"
: "${IMAGE:?Environment variable IMAGE is required (e.g. registry.rocket.chat/rocketchat/rocket.chat)}"
: "${TRAEFIK_RELEASE:?Environment variable TRAEFIK_RELEASE is required (e.g. v2.9.8)}"
: "${MONGO_USERNAME:?Environment variable MONGO_USERNAME is required}"
: "${MONGO_PASSWORD:?Environment variable MONGO_PASSWORD is required}"
: "${MONGO_HOST:?Environment variable MONGO_HOST is required (private IP)}"
: "${ROOT_URL:?Environment variable ROOT_URL is required (e.g. https://chat.example.com)}"
: "${DOMAIN:?Environment variable DOMAIN is required (e.g. chat.example.com)}"
: "${LETSENCRYPT_EMAIL:?Environment variable LETSENCRYPT_EMAIL is required}"

############################
# 1. Defaults              #
############################
: "${MONGO_PORT:=27017}"
: "${REPLSET:=rs0}"
: "${APP_DB:=rocketchat}"
: "${OPLOG_DB:=local}"

############################
# 2. Helpers               #
############################
info()  { printf "\e[34m[INFO]\e[0m  %s\n" "$*"; }
ok()    { printf "\e[32m[ OK ]\e[0m  %s\n" "$*"; }
error() { printf "\e[31m[ERR ]\e[0m  %s\n" "$*"; exit 1; }

############################
# 3. Preflight             #
############################
(( EUID == 0 )) || error "This script must be run as root."

############################
# 4. Compute URLs          #
############################
MONGO_URL="mongodb://${MONGO_USERNAME}:${MONGO_PASSWORD}@${MONGO_HOST}:${MONGO_PORT}/${APP_DB}?replicaSet=${REPLSET}&authSource=admin&directConnection=true"
MONGO_OPLOG_URL="mongodb://${MONGO_USERNAME}:${MONGO_PASSWORD}@${MONGO_HOST}:${MONGO_PORT}/${OPLOG_DB}?replicaSet=${REPLSET}&authSource=admin&directConnection=true"

############################
# 5. Install Docker        #
############################
install_docker() {
  if ! command -v docker &>/dev/null; then
    info "Installing Docker Engine & Compose plugin…"
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg lsb-release
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      | tee /etc/apt/sources.list.d/docker.list >/dev/null
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable docker && systemctl start docker
    ok "Docker installed."
    # ────────────────────────────────────────────────────
    # Allow the ubuntu user to run Docker without sudo
    # ────────────────────────────────────────────────────
    # Docker package already creates the 'docker' group, but ensure it:
    groupadd docker 2>/dev/null || true
    usermod -aG docker ubuntu
    ok "Added 'ubuntu' to the docker group"
  else
    info "Docker already present."
  fi
}

############################
# 6. Create network        #
############################
create_network() {
  local net="web"
  if ! docker network ls --format '{{.Name}}' | grep -xq "$net"; then
    info "Creating Docker network '$net'…"
    docker network create "$net"
    ok "Network '$net' created."
  else
    info "Network '$net' exists."
  fi
}

############################
# 7. Write docker-compose  #
############################
write_compose() {
  info "Writing docker-compose.yml…"

  # ACME storage
  touch acme.json && chmod 600 acme.json

  cat > docker-compose.yml <<EOF
version: "3.7"

networks:
  web:
    external: true

services:
  traefik:
    image: traefik:${TRAEFIK_RELEASE}
    restart: always
    networks: [web]
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./acme.json:/letsencrypt/acme.json:rw
      - /var/run/docker.sock:/var/run/docker.sock:ro
    command:
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
      - --api.insecure=false

      # entrypoints
      - --entrypoints.web.address=:80
      - --entrypoints.websecure.address=:443

      # HTTP-01 challenge on port 80
      - --certificatesresolvers.le.acme.httpchallenge=true
      - --certificatesresolvers.le.acme.httpchallenge.entryPoint=web
      - --certificatesresolvers.le.acme.email=${LETSENCRYPT_EMAIL}
      - --certificatesresolvers.le.acme.storage=/letsencrypt/acme.json

  rocketchat:
    image: ${IMAGE}:${RELEASE}
    user: "65533:65533"
    restart: always
    networks: [web]
    volumes:
      - ./uploads:/app/uploads
    expose:
      - "3000"
    environment:
      MONGO_URL:       "${MONGO_URL}"
      MONGO_OPLOG_URL: "${MONGO_OPLOG_URL}"
      ROOT_URL:        "${ROOT_URL}"
      PORT:            "3000"
      DEPLOY_METHOD:   "docker"
      OVERWRITE_SETTING_Statistics_reporting:   "false"
      OVERWRITE_SETTING_Accounts_TwoFactorAuthentication_Enabled: "false"
      OVERWRITE_SETTING_Allow_Marketing_Emails: "false"
    labels:
      traefik.enable: "true"
      traefik.docker.network: "web"

      # 1) HTTP → HTTPS redirect but skip ACME challenge path
      traefik.http.routers.http-redirect.rule: \
"Host(\\"${DOMAIN}\\") && PathPrefix(\\"/\\") && !PathPrefix(\\"/.well-known/acme-challenge/\\")"
      traefik.http.routers.http-redirect.entrypoints: "web"
      traefik.http.routers.http-redirect.middlewares: "redirect-to-https"
      traefik.http.middlewares.redirect-to-https.redirectscheme.scheme: "https"

      # 2) HTTPS router
      traefik.http.routers.rc-secure.rule: "Host(\\"${DOMAIN}\\")"
      traefik.http.routers.rc-secure.entrypoints: "websecure"
      traefik.http.routers.rc-secure.tls: "true"
      traefik.http.routers.rc-secure.tls.certresolver: "le"
      traefik.http.routers.rc-secure.service: "rc-svc"
      traefik.http.services.rc-svc.loadbalancer.server.port: "3000"
EOF

  ok "docker-compose.yml written"
}


############################
# 8. Deploy                #
############################
deploy_stack() {
  info "Tearing down any existing stack…"
  docker compose down || true

  info "Deploying Traefik & Rocket.Chat (scale=4)…"
  docker compose up -d --scale rocketchat=4
  ok "Deployment complete."
}

############################
# 9. Main                  #
############################
install_docker
create_network
write_compose
deploy_stack

echo
ok "Rocket.Chat & Traefik are live!"
info "Visit: ${ROOT_URL}"
