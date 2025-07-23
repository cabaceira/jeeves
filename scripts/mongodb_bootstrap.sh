#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# MongoDB 7 LOCAL replica-set bootstrapper – Ubuntu 24.04 (“noble”)
# Fully non-interactive; driven by env vars in user-data.
# -----------------------------------------------------------------------------
set -euo pipefail

############################
# 0. Require ENV vars      #
############################
: "${MONGO_PORT:?MONGO_PORT must be set}"
: "${REPLSET_NAME:?REPLSET_NAME must be set}"
: "${MONGO_USERNAME:?MONGO_USERNAME must be set}"
: "${MONGO_PASSWORD:?MONGO_PASSWORD must be set}"

############################
# 1. Helpers               #
############################
info()  { printf "\e[34m[INFO]\e[0m  %s\n" "$*"; }
ok()    { printf "\e[32m[ OK ]\e[0m  %s\n" "$*"; }
error() { printf "\e[31m[ERR ]\e[0m  %s\n" "$*"; exit 1; }

############################
# 2. Auto-detect NODE_ADDR #
############################
NODE_ADDR=$(hostname -I | awk '{for(i=1;i<=NF;i++) if($i !~ /^127\./){print $i; exit}}')
[[ -n "$NODE_ADDR" ]] || error "Could not detect NODE_ADDR"

############################
# 3. Wait for apt locks    #
############################
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
# 4. Install MongoDB 7     #
############################
install_mongo() {
  if ! command -v mongod &>/dev/null; then
    wait_for_apt
    info "Adding MongoDB 7 apt repo…"
    apt-get update -y
    apt-get install -y curl gnupg
    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc \
      | gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg
    echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg] \
https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \
      > /etc/apt/sources.list.d/mongodb-org-7.0.list

    wait_for_apt
    apt-get update -y
    apt-get install -y mongodb-org net-tools netcat-openbsd openssl
    systemctl enable mongod
    ok "MongoDB package installed"
  else
    info "MongoDB already installed"
  fi
}

############################
# 5. Configure firewall    #
############################
setup_firewall() {
  info "Installing iptables-persistent…"
  DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
  info "Allowing incoming MongoDB on ${MONGO_PORT} from anywhere"
  iptables -I INPUT -p tcp --dport "${MONGO_PORT}" -m conntrack --ctstate NEW -j ACCEPT
  netfilter-persistent save
  ok "Firewall rules set"
}

############################
# 6. Write config files    #
############################
write_conf() {
  cat > /etc/mongod.conf.nosec <<EOF
storage:
  dbPath: /var/lib/mongodb
net:
  bindIp: 0.0.0.0
  port: ${MONGO_PORT}
replication:
  replSetName: ${REPLSET_NAME}
processManagement:
  timeZoneInfo: /usr/share/zoneinfo
EOF

  cat > /etc/mongod.conf.sec <<EOF
storage:
  dbPath: /var/lib/mongodb
net:
  bindIp: 0.0.0.0
  port: ${MONGO_PORT}
replication:
  replSetName: ${REPLSET_NAME}
security:
  authorization: enabled
  keyFile: /etc/mongo-keyfile
processManagement:
  timeZoneInfo: /usr/share/zoneinfo
EOF
  ok "Config files written"
}

############################
# 7. Create keyfile        #
############################
create_keyfile() {
  if [[ ! -f /etc/mongo-keyfile ]]; then
    info "Creating internal auth keyfile…"
    openssl rand -base64 756 > /etc/mongo-keyfile
    chown mongodb:mongodb /etc/mongo-keyfile
    chmod 600 /etc/mongo-keyfile
    ok "Keyfile created"
  fi
}

############################
# 8. Wait for mongod       #
############################
wait_mongo() {
  info "Waiting for mongod to start…"
  until nc -z 0.0.0.0 "${MONGO_PORT}"; do
    printf "."
    sleep 2
  done
  echo
}

############################
# 9. Initialize repl-set   #
############################
initiate_replset() {
  info "Initiating replica-set…"
  mongosh --quiet <<EOF
try {
  rs.status();
  print("Replica-set already initialized.");
} catch(e) {
  rs.initiate({
    _id: "${REPLSET_NAME}",
    members: [{ _id: 0, host: "${NODE_ADDR}:${MONGO_PORT}" }]
  });
  print("Replica-set created.");
}
EOF
}

############################
# 10. Create admin user    #
############################
create_admin() {
  info "Creating admin user…"
  mongosh --quiet <<EOF
const adm = db.getSiblingDB("admin");
if (!adm.getUser("${MONGO_USERNAME}")) {
  adm.createUser({
    user: "${MONGO_USERNAME}",
    pwd:  "${MONGO_PASSWORD}",
    roles:[{ role: "root", db: "admin" }]
  });
  print("Admin user created.");
} else {
  print("Admin user exists.");
}
EOF
}

############################
# 11. Main workflow        #
############################
install_mongo
setup_firewall
write_conf

info "Starting mongod (no auth)…"
cp /etc/mongod.conf.nosec /etc/mongod.conf
systemctl restart mongod
wait_mongo

initiate_replset
create_admin
create_keyfile

info "Enabling auth and restarting…"
cp /etc/mongod.conf.sec /etc/mongod.conf
systemctl restart mongod
wait_mongo

ok "MongoDB '${REPLSET_NAME}' is ready at ${NODE_ADDR}:${MONGO_PORT}"
