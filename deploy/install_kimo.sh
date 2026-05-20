#!/usr/bin/env bash
# Instala Kimo en /srv/kimo. Correr como usuario `kimo` (no root).
# Pre-requisito: setup_vm.sh ya ejecutado.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Eam145vc/kimo.git}"
INSTALL_DIR="/srv/kimo"

if [ "$(id -u)" -eq 0 ]; then
    echo "ERROR: no correr como root. Sudo a usuario kimo: 'su - kimo'"
    exit 1
fi

cd "$INSTALL_DIR"

echo "===> 1/4 Clonando repo"
if [ -d "$INSTALL_DIR/.git" ]; then
    git pull origin main
else
    git clone "$REPO_URL" "$INSTALL_DIR/repo_tmp"
    mv "$INSTALL_DIR/repo_tmp/.git" "$INSTALL_DIR/"
    cp -r "$INSTALL_DIR/repo_tmp/"* "$INSTALL_DIR/"
    rm -rf "$INSTALL_DIR/repo_tmp"
fi

echo "===> 2/4 Creando virtualenv"
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip

echo "===> 3/4 Instalando dependencias"
.venv/bin/pip install -r requirements.txt

echo "===> 4/4 Verificacion"
if [ ! -f ".env" ]; then
    echo ""
    echo "FALTA: crear archivo .env con tus credenciales"
    echo "  cp .env.example .env"
    echo "  vim .env"
    echo ""
fi

echo ""
echo "Kimo instalado en $INSTALL_DIR"
echo ""
echo "Para arrancar el bot manualmente (probar):"
echo "  cd /srv/kimo && .venv/bin/python -m skiimo.bot.app"
echo ""
echo "Para instalar el servicio systemd (correr como root):"
echo "  sudo cp deploy/kimo.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now kimo"
