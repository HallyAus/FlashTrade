#!/bin/bash
# =============================================================================
# FlashTrade — Proxmox LXC Setup Script
# Run this on your Proxmox HOST (not inside a container)
#
# Usage: bash proxmox-setup.sh
#
# Creates an Ubuntu 24.04 LXC with Docker, clones FlashTrade, and starts it.
# =============================================================================

set -euo pipefail

# --- Configuration (adjust these) ---
CT_ID=200                          # Container ID — pick an unused one
CT_HOSTNAME="flashtrade"
CT_PASSWORD="changeme"             # Root password for the LXC — CHANGE THIS
CT_STORAGE="local-lvm"             # Your Proxmox storage (check: pvesm status)
CT_DISK_SIZE=60                    # GB
CT_CORES=4
CT_RAM=8192                        # MB
CT_SWAP=4096                       # MB
CT_BRIDGE="vmbr0"                  # Network bridge
CT_IP="dhcp"                       # Use "dhcp" or static like "192.168.1.50/24"
CT_GATEWAY=""                      # Only needed for static IP, e.g. "192.168.1.1"

# --- Color output ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# --- Pre-flight checks ---
if ! command -v pct &> /dev/null; then
    error "This script must be run on a Proxmox host (pct not found)"
fi

if pct status "$CT_ID" &> /dev/null; then
    error "Container ID $CT_ID already exists. Pick a different CT_ID."
fi

# --- Download template if missing ---
info "Checking for Ubuntu 24.04 template..."
if ! pveam list local | grep -q "ubuntu-24.04"; then
    info "Downloading Ubuntu 24.04 template..."
    pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst
fi

TEMPLATE=$(pveam list local | grep "ubuntu-24.04" | awk '{print $1}' | head -1)
if [[ -z "$TEMPLATE" ]]; then
    error "Ubuntu 24.04 template not found. Download it manually from Proxmox GUI."
fi
info "Using template: $TEMPLATE"

# --- Build network config ---
NET_CONFIG="name=eth0,bridge=${CT_BRIDGE},ip=${CT_IP}"
if [[ -n "$CT_GATEWAY" ]]; then
    NET_CONFIG="${NET_CONFIG},gw=${CT_GATEWAY}"
fi

# --- Create LXC ---
info "Creating LXC container $CT_ID ($CT_HOSTNAME)..."

pct create "$CT_ID" "$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --password "$CT_PASSWORD" \
    --storage "$CT_STORAGE" \
    --rootfs "${CT_STORAGE}:${CT_DISK_SIZE}" \
    --cores "$CT_CORES" \
    --memory "$CT_RAM" \
    --swap "$CT_SWAP" \
    --net0 "$NET_CONFIG" \
    --unprivileged 0 \
    --features "nesting=1,keyctl=1" \
    --onboot 1 \
    --start 0

# --- Start container ---
info "Starting container..."
pct start "$CT_ID"
sleep 5  # Wait for networking

# --- Install everything inside the LXC ---
info "Installing Docker, Python, and FlashTrade..."

pct exec "$CT_ID" -- bash -c '
set -euo pipefail

echo ">>> Updating system..."
apt-get update && apt-get upgrade -y

echo ">>> Installing prerequisites..."
apt-get install -y \
    ca-certificates \
    curl \
    gnupg \
    git \
    python3 \
    python3-pip \
    python3-venv \
    sudo \
    htop \
    nano

echo ">>> Installing Docker..."
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo ">>> Verifying Docker..."
docker --version
docker compose version

echo ">>> Cloning FlashTrade..."
cd /opt
git clone https://github.com/HallyAus/FlashTrade.git
cd FlashTrade

echo ">>> Creating .env from template..."
cp .env.example .env
# Generate a random secret key
SECRET=$(openssl rand -hex 32)
sed -i "s/changeme_generate_with_openssl_rand_hex_32/$SECRET/" .env
# Generate a random DB password
DB_PASS=$(openssl rand -hex 16)
sed -i "s/changeme_use_strong_password/$DB_PASS/g" .env

echo ">>> Starting FlashTrade stack..."
docker compose up -d --build

echo ">>> Waiting for services to come up..."
sleep 15

echo ">>> Service status:"
docker compose ps
'

# --- Get container IP ---
CT_ACTUAL_IP=$(pct exec "$CT_ID" -- hostname -I | awk '{print $1}')

echo ""
info "============================================"
info "  LXC $CT_ID ($CT_HOSTNAME) is ready!"
info "  IP: $CT_ACTUAL_IP"
info "  SSH: ssh root@$CT_ACTUAL_IP"
info "  API: http://$CT_ACTUAL_IP:8000"
info "============================================"
info ""
info "NEXT: Set up Cloudflare Tunnel (see docs/cloudflare-tunnel.md)"
