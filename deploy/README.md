# Deploy Kimo en Starlight VM

## Arquitectura

```
GitHub (Eam145vc/kimo)
        │
        │ git push main
        ▼
GitHub Actions workflow
        │
        │ SSH a VM
        ▼
VM Starlight Ubuntu 24.04
  └─ /srv/kimo/
       ├─ .venv/          (Python virtualenv)
       ├─ .env             (credenciales)
       ├─ data/skiimo.db   (SQLite persistente)
       ├─ logs/
       └─ skiimo/...       (codigo del bot)

systemd: kimo.service (auto-restart, arranca al boot)
```

## Setup inicial (una vez, manual)

### En la VM como root

```bash
# 1. Bajar script de setup
curl -fsSL https://raw.githubusercontent.com/Eam145vc/kimo/main/deploy/setup_vm.sh -o setup_vm.sh
bash setup_vm.sh

# 2. Hacer login como usuario kimo
su - kimo
```

### Como usuario kimo

```bash
cd /srv/kimo
curl -fsSL https://raw.githubusercontent.com/Eam145vc/kimo/main/deploy/install_kimo.sh -o install_kimo.sh
bash install_kimo.sh

# Crear .env con credenciales
cp .env.example .env
vim .env  # completar valores
```

### De nuevo como root

```bash
# Instalar el servicio systemd
cp /srv/kimo/deploy/kimo.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kimo

# Permitir que el usuario kimo reinicie el servicio sin password
# (necesario para que GitHub Actions pueda deployar)
cat > /etc/sudoers.d/kimo-restart <<EOF
kimo ALL=(ALL) NOPASSWD: /bin/systemctl restart kimo
kimo ALL=(ALL) NOPASSWD: /bin/systemctl status kimo
EOF
chmod 0440 /etc/sudoers.d/kimo-restart

# Ver el bot corriendo
systemctl status kimo
journalctl -u kimo -f
```

## Configurar GitHub Actions

### En la VM (como usuario kimo)

```bash
# Generar SSH key dedicada para deploys
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_deploy -N ""

# Autorizar esa clave para entrar
cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys

# Mostrar clave privada (la copiamos a GitHub Secrets)
cat ~/.ssh/github_deploy
```

### En GitHub (settings del repo)

Settings → Secrets and variables → Actions → New repository secret. Crear:

| Nombre | Valor |
|---|---|
| `SSH_HOST` | `203.161.47.11` |
| `SSH_PORT` | `22022` |
| `SSH_USER` | `kimo` |
| `SSH_KEY` | (la clave privada, copiada completa incluyendo `-----BEGIN...END-----`) |

## Operacion diaria

### Hacer un cambio

```bash
# En tu PC
git add .
git commit -m "lo que cambie"
git push origin main

# GitHub Actions hace el deploy solo en ~30 segundos.
# Te llega email si falla.
```

### Ver logs

```bash
# En la VM
journalctl -u kimo -n 100 --no-pager      # ultimas 100 lineas
journalctl -u kimo -f                       # tiempo real
```

### Reiniciar manualmente

```bash
sudo systemctl restart kimo
```

### Actualizar variables de entorno

```bash
sudo vim /srv/kimo/.env
sudo systemctl restart kimo
```

## Comandos de mantenimiento

```bash
# Espacio en disco
df -h /srv/kimo

# Tamano de la DB
ls -lh /srv/kimo/data/skiimo.db

# Backup manual de la DB
cp /srv/kimo/data/skiimo.db /srv/kimo/data/skiimo-$(date +%Y%m%d).db

# RAM y CPU
htop

# Verificar firewall
ufw status verbose
```
