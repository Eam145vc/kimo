r"""Configura el Hikvision para que pushee eventos al panel local + test.

Pasos:
  1. GET httpHosts (ver config actual)
  2. PUT httpHost id=1 apuntando al panel local de esta PC (192.168.128.27:8765)
  3. POST /test (que el equipo intente conectar al panel)
  4. Imprimir como verificar
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from skiimo.hikvision import HikClient


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def main() -> None:
    local_ip = get_local_ip()
    print(f"\nIP local de esta PC: {local_ip}")
    print(f"Panel debe estar corriendo en http://{local_ip}:8765\n")

    with HikClient() as hik:
        # 1. Ver config actual
        print("[1] GET httpHosts (config actual)")
        try:
            current = hik.get_http_hosts()
            print(json.dumps(current, indent=2, default=str)[:2000])
        except Exception as e:
            print(f"  Error: {e}")

        # 2. Configurar host id=1
        print("\n[2] PUT httpHost id=1")
        try:
            r = hik.set_http_host(
                host_id=1,
                ip=local_ip,
                port=8765,
                url="/api/hik/event",
                protocol="HTTP",
                parameter_format="json",
                host_name="skiimo-panel-local",
            )
            print(json.dumps(r, indent=2, default=str)[:1500])
        except Exception as e:
            print(f"  Error: {e}")
            import traceback; traceback.print_exc()

        # 3. Test connectivity
        print("\n[3] POST httpHosts/1/test (el equipo intenta llegar al panel)")
        try:
            r = hik.test_http_host(1)
            print(json.dumps(r, indent=2, default=str)[:1500])
        except Exception as e:
            print(f"  Error: {e}")

        # 4. Verificar config final
        print("\n[4] GET httpHosts (verificar guardado)")
        try:
            current = hik.get_http_hosts()
            print(json.dumps(current, indent=2, default=str)[:2000])
        except Exception as e:
            print(f"  Error: {e}")

    print("\nLISTO. Ahora hace lo siguiente:")
    print("  1. Acercate al equipo y autenticate con tu cara/huella")
    print("  2. El evento deberia llegar al panel en < 2 segundos")
    print(f"  3. Ver logs del panel para confirmar")
    print("  4. Tambien podes verificar con: curl http://localhost:8765/api/hik/event/test")


if __name__ == "__main__":
    main()
