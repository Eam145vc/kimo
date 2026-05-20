#!/usr/bin/env bash
# Setup inicial de la VM Starlight Ubuntu 24.04 para Kimo bot
# Correr como root la primera vez: bash setup_vm.sh

set -euo pipefail

echo "===> 1/8 Actualizando sistema"
apt-get update -y
apt-get upgrade -y

echo "===> 2/8 Instalando dependencias base"
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3.12 python3.12-venv python3-pip \
    git build-essential libssl-dev libffi-dev \
    sqlite3 ufw curl ca-certificates \
    htop tmux vim

echo "===> 3/8 Creando usuario kimo"
if ! id -u kimo >/dev/null 2>&1; then
    useradd -m -s /bin/bash kimo
    echo "Usuario kimo creado"
else
    echo "Usuario kimo ya existe"
fi

echo "===> 4/8 Configurando estructura /srv/kimo"
mkdir -p /srv/kimo /srv/kimo/data /srv/kimo/logs
chown -R kimo:kimo /srv/kimo

echo "===> 5/8 Configurando firewall (solo SSH 22022)"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22022/tcp comment 'SSH custom port'
ufw --force enable

echo "===> 6/8 Configurando timezone Colombia"
timedatectl set-timezone America/Bogota

echo "===> 7/8 Configurando swap (recomendado en VM de 4GB)"
if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "Swap 2GB creado"
fi

echo "===> 8/8 Setup base completado"
echo ""
echo "Proximos pasos:"
echo "  1. Clonar repo en /srv/kimo (como usuario kimo)"
echo "  2. Crear .env con credenciales"
echo "  3. Instalar el servicio systemd"
echo "  4. Configurar GitHub Actions deploy"
