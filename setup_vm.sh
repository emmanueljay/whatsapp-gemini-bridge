#!/usr/bin/env bash
set -euo pipefail

echo "=== [1/4] Configuring 2 GB Swap File ==="
if ! sudo /sbin/swapon --show | grep -q '/swapfile'; then
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo /sbin/mkswap /swapfile
  sudo /sbin/swapon /swapfile
  if ! grep -q '/swapfile' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  fi
  sudo sysctl vm.swappiness=20
  echo 'vm.swappiness=20' | sudo tee /etc/sysctl.d/99-swappiness.conf
  echo "Swap configured successfully:"
  free -h
else
  echo "Swapfile already active."
fi

echo "=== [2/4] Installing Docker Engine & Docker Compose Plugin ==="
if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y ca-certificates curl gnupg git
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER" || true
  sudo systemctl enable --now docker
else
  echo "Docker already installed."
fi

echo "=== [3/4] Preparing Application Directory ==="
sudo mkdir -p /opt/whatsapp-gemini-bridge
sudo chown -R "$USER":"$USER" /opt/whatsapp-gemini-bridge
if [ -f /opt/whatsapp-gemini-bridge/.env.example ] && [ ! -f /opt/whatsapp-gemini-bridge/.env ]; then
  API_KEY=$(openssl rand -hex 24)
  DASH_PASS=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 20)
  cp /opt/whatsapp-gemini-bridge/.env.example /opt/whatsapp-gemini-bridge/.env
  sed -i "s|replace_with_strong_random_hex_secret|$API_KEY|g" /opt/whatsapp-gemini-bridge/.env
  sed -i "s|replace_with_strong_dashboard_password|$DASH_PASS|g" /opt/whatsapp-gemini-bridge/.env
  chmod 600 /opt/whatsapp-gemini-bridge/.env
fi

echo "=== [4/4] Setup Complete ==="
docker --version
docker compose version
free -h
