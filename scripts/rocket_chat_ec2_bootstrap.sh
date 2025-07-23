#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Rocket.Chat + Traefik bootstrapper (Docker) – Ubuntu 24.04
# Fully non-interactive; driven by env vars in user-data.
# -----------------------------------------------------------------------------
set -euo pipefail

############################
# 0. Required ENV VARS     #
############################
: "${RELEASE:?RELEASE (e.g. 7.7.4) is required}"
: "${IMAGE:?IMAGE (e.g. registry.rocket.chat/rocketchat/rocket.chat) is required}"
: "${TRAEFIK_RELEASE:?TRAEFIK_RELEASE (e.g. v2.9.8) is required}"
: "${MONGO_USERNAME:?MONGO_USERNAME is required}"
: "${MONGO_PASSWORD:?MONGO_PASSWORD is required}"
: "${MONGO_HOST:?MONGO_HOST (private IP of Mongo node) is required}"
: "${MONGO_PORT:?MONGO_PORT is required}"
: "${REPLSET:?REPLSET is required}"
: "${ROOT_URL:?ROOT_URL (e.g. https://chat.example.com) is required}"
: "${DOMAIN:?DOMAIN (e.g. chat.example.com) is required}"
: "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL is required}"
: "${ROCKETCHAT_SCALE:=4}"     # how many Rocket.Chat replicas after Traefik is ready

############################
# Helpers & Lock-wait      #
############################
info()  { printf "\e[34m[INFO]\e[0m  %s\n" "$*"; }
ok()    { printf "\e[32m[ OK ]\e[0m  %s\n" "$*"; }
error() { printf "\e[31m[ERR ]\e[0m  %s\n" "$*"; exit 1; }
(( EUID == 0 )) || error "Must run as root"

wait_for_apt() {
  info "Waiting for existing apt/dpkg locks to clear…"
  for lock in \
    /var/lib/dpkg/lock-frontend \
    /var/lib/dpkg/lock \
    /var/lib/apt/lists/lock \
    /var/cache/apt/archives/lock; do
    while fuser "$lock" >/dev/null 2>&1; do
      printf "[WAIT] lock on %s…\n" "$lock"
      sleep 5
    done
  done
}

############################
# Compute Mongo URLs       #
############################
MONGO_URL="mongodb://${MONGO_USERNAME}:${MONGO_PASSWORD}@${MONGO_HOST}:${MONGO_PORT}/rocketchat?replicaSet=${REPLSET}&authSource=admin&directConnection=true"
MONGO_OPLOG_URL="mongodb://${MONGO_USERNAME}:${MONGO_PASSWORD}@${MONGO_HOST}:${MONGO_PORT}/local?replicaSet=${REPLSET}&authSource=admin&directConnection=true"

############################
# Install Docker Engine    #
############################
install_docker() {
  if ! command -v docker &>/dev/null; then
    info "Installing Docker…"
    wait_for_apt
    apt-get update -y
    wait_for_apt
    apt-get install -y ca-certificates curl gnupg lsb-release
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list
    wait_for_apt
    apt-get update -y
    wait_for_apt
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable docker && systemctl start docker
    ok "Docker installed"
  else
    info "Docker already present"
  fi
}

############################
# Create Docker network    #
############################
create_network() {
  local net=web
  if ! docker network ls --format '{{.Name}}' | grep -xq "$net"; then
    info "Creating Docker network '$net'"
    docker network create "$net"
    ok "Network '$net' created"
  else
    info "Network '$net' exists"
  fi
}

############################
# Write docker-compose     #
############################
write_compose() {
  info "Writing docker-compose.yml"
  # ensure ACME file exists & locked
  touch acme.json && chmod 600 acme.json

  cat > docker-compose.yml <<EOF
version: "3.7"

networks:
  web:
    external: true

volumes:
  traefik: {}

services:
  traefik:
    image: traefik:${TRAEFIK_RELEASE}
    restart: always
    networks:
      - web
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./acme.json:/letsencrypt/acme.json:rw
      - /var/run/docker.sock:/var/run/docker.sock:ro
    command:
      # Entrypoints
      - --entrypoints.web.address=:80
      - --entrypoints.websecure.address=:443
      # HTTP→HTTPS redirect
      - --entrypoints.web.http.redirections.entryPoint.to=websecure
      - --entrypoints.web.http.redirections.entryPoint.scheme=https
      # Docker provider
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
      # ACME (HTTP-01 challenge)
      - --certificatesresolvers.le.acme.httpchallenge=true
      - --certificatesresolvers.le.acme.httpchallenge.entryPoint=web
      - --certificatesresolvers.le.acme.email=${LETSENCRYPT_EMAIL}
      - --certificatesresolvers.le.acme.storage=/letsencrypt/acme.json

  rocketchat:
    image: ${IMAGE}:${RELEASE}
    user: "65533:65533"
    restart: always
    networks:
      - web
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
      # tell Traefik to watch this service
      traefik.enable: "true"

      # 1) HTTP router: catch DOMAIN on :80, redirect to HTTPS
      traefik.http.routers.http-redirect.rule: "Host(\\"${DOMAIN}\\")"
      traefik.http.routers.http-redirect.entrypoints: "web"
      traefik.http.routers.http-redirect.middlewares: "redirect-to-https"

      # middleware definition
      traefik.http.middlewares.redirect-to-https.redirectscheme.scheme: "https"

      # 2) HTTPS router: catch DOMAIN on :443, secure it, route to service
      traefik.http.routers.rc-secure.rule: "Host(\\"${DOMAIN}\\")"
      traefik.http.routers.rc-secure.entrypoints: "websecure"
      traefik.http.routers.rc-secure.tls: "true"
      traefik.http.routers.rc-secure.tls.certresolver: "le"
      traefik.http.routers.rc-secure.service: "rc-svc"

      # service → port 3000
      traefik.http.services.rc-svc.loadbalancer.server.port: "3000"
EOF

  ok "docker-compose.yml written"
}


############################
# Deploy & validate TLS    #
############################
deploy_stack() {
  info "Tearing down any existing stack…"
  docker compose down || true

  info "Bringing up Traefik & 1 Rocket.Chat…"
  docker compose up -d --scale rocketchat=1

  info "Waiting for Traefik HTTP endpoint…"
  until curl -sf http://localhost/ >/dev/null; do
    printf "."; sleep 5
  done; echo
  ok "Traefik is responding"

  # DNS must point to this host’s public IP
  my_ip=$(curl -sf http://169.254.169.254/latest/meta-data/public-ipv4)
  info "Waiting for DNS ${DOMAIN} → ${my_ip}…"
  until host "${DOMAIN}" | grep -q "${my_ip}"; do
    printf "."; sleep 5
  done; echo
  ok "DNS record is live"

  # Validate TLS (no self-signed)
  info "Checking TLS certificate for https://${DOMAIN}…"
  until curl --fail --silent --show-error https://${DOMAIN} -o /dev/null; do
    printf "."; sleep 5
  done; echo
  ok "Valid TLS certificate (no warnings)"

  info "Scaling Rocket.Chat to ${ROCKETCHAT_SCALE} replicas…"
  docker compose up -d --scale rocketchat=${ROCKETCHAT_SCALE}
  ok "Rocket.Chat scaled to ${ROCKETCHAT_SCALE}"
}

############################
# Main                     #
############################
install_docker
create_network
write_compose
deploy_stack

echo
ok "Rocket.Chat & Traefik are live: ${ROOT_URL}"
