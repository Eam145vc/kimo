"""Bootstrap: inicializa DB y hace sync inicial si esta vacia.

Se ejecuta automaticamente al arrancar el bot en Railway/produccion.
"""
from __future__ import annotations

import logging
import sys

from skiimo.db.schema import get_conn, init_db

log = logging.getLogger("skiimo.bootstrap")


def ensure_db_ready() -> None:
    """Garantiza que el schema este creado y haya un sync inicial de Siigo."""
    init_db()

    # Chequear si hay datos
    conn = get_conn()
    try:
        n_customers = conn.execute("SELECT COUNT(*) FROM siigo_customers").fetchone()[0]
        n_products = conn.execute("SELECT COUNT(*) FROM siigo_products").fetchone()[0]
    finally:
        conn.close()

    if n_customers == 0 or n_products == 0:
        log.warning("DB vacia (clientes=%d, productos=%d). Sincronizando con Siigo...",
                    n_customers, n_products)
        try:
            from skiimo.sync.siigo_sync import (
                sync_customers, sync_products, sync_invoices, sync_purchases,
            )
            from siigo_client import SiigoClient

            with SiigoClient() as s:
                s._authenticate()
                conn = get_conn()
                try:
                    sync_customers(s, conn, full=True)
                    sync_products(s, conn, full=True)
                    sync_invoices(s, conn, full=True)
                    sync_purchases(s, conn, full=True)
                finally:
                    conn.close()
            log.info("Sync inicial completado")
        except Exception:
            log.exception("Error en sync inicial. El bot arrancara igual, "
                          "pero usa /resumen u otros comandos para forzar sync mas tarde.")

    # Chequear si hay categorias / precios
    conn = get_conn()
    try:
        n_precios = conn.execute("SELECT COUNT(*) FROM precios_oficiales").fetchone()[0]
    finally:
        conn.close()

    if n_precios == 0 and n_products > 0:
        log.warning("Precios oficiales vacios. Aplicando calibracion automatica...")
        try:
            # Reutilizar logica del calibrador
            import subprocess
            from pathlib import Path
            here = Path(__file__).resolve().parent.parent
            subprocess.run(
                [sys.executable, "-m", "skiimo.pricing.calibrador", "--apply"],
                cwd=here, check=False, timeout=120,
            )
        except Exception:
            log.exception("Error aplicando calibracion. Hacelo manualmente despues.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ensure_db_ready()
