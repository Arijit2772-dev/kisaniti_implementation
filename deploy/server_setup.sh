#!/usr/bin/env bash
# One-time preparation of the KisanNiti server (decision CHAT-54): Ubuntu 24.04 on an Azure B1s VM.
# Run on the server as the admin user: bash server_setup.sh   (idempotent: safe to run again)
# The network perimeter is the Azure network security group (ports 22, 80, 443, 8883 only). ufw is not used,
# because Docker's published ports bypass it; instead the production compose file publishes only Caddy and the broker.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# 1. updates now, and security updates automatically every day
sudo apt-get update -qq
sudo apt-get -y -qq -o Dpkg::Options::=--force-confold upgrade
sudo apt-get -y -qq install unattended-upgrades

# 2. 2 GB swap: the VM has ~0.9 GB RAM; swap absorbs peaks, swappiness 10 keeps it a last resort
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile && sudo swapon /swapfile
  grep -q /swapfile /etc/fstab || echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi
echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-kisaniti.conf >/dev/null
sudo sysctl -q --system

# 3. Docker and Compose from Ubuntu's own archive (no third-party repository)
sudo apt-get -y -qq install docker.io docker-compose-v2
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker

# 4. SSH: keys only (Azure already sets PasswordAuthentication no), no root login
echo "PermitRootLogin no" | sudo tee /etc/ssh/sshd_config.d/10-kisaniti.conf >/dev/null
sudo sshd -t && sudo systemctl reload ssh

echo "server ready: $(docker --version); $(docker compose version)"
