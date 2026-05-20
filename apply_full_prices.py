"""Aplica precios oficiales detectados para TODAS las familias de productos.

Patrones (con IVA / pre-IVA):
  Bolsa 6L CON licor:   $26,000 / $23,000 / $20,000
  Bolsa 6L SIN licor:   $24,000 / $21,500 / $20,000
  Sachet 08 OZ:         $ 2,200 / $ 2,000 / $ 1,800
  Perlas 1200 GR:       $37,500 / $34,500 / $32,000
  Perlas 3400 GR:       $99,000 / $90,000 / $82,000
  Perlas 350 GR:        $16,000 / $14,500 / $13,000
  Sales (chamoy etc):   moda historica con descuento -8% / -15%
  Siropes 1L:           moda historica
  Gelatinas:            moda historica
  Cremosos:             $45,000 / $40,000 / $36,000
"""
import json
import statistics
import sys
from collections import Counter
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from skiimo.db.schema import get_conn


# Pre-IVA = con_IVA / 1.19
PRECIOS_FIJOS = {
    # (familia, con_licor) -> (detal_con_iva, mayor_con_iva, distri_con_iva)
    "BOLSA_6L_CON": (26000.0, 23000.0, 20000.0),
    "BOLSA_6L_SIN": (24000.0, 21500.0, 20000.0),
    "SACHET_CON":   (2200.0, 2000.0, 1800.0),
    "SACHET_SIN":   (2200.0, 2000.0, 1800.0),
    "PERLAS_1200":  (37500.0, 34500.0, 32000.0),
    "PERLAS_3400":  (99000.0, 90000.0, 82000.0),
    "PERLAS_350":   (16000.0, 14500.0, 13000.0),
    "CREMOSO":      (45000.0, 40000.0, 36000.0),
}


def clasificar_producto(code: str, name: str, account_group: str | None) -> str | None:
    """Asigna un producto a uno de los buckets de precio fijo."""
    n = (name or "").upper()
    g = (account_group or "").upper()
    sin_licor = "SIN LICOR" in n or "SIN LIC" in n

    # BOLSAS 6L
    if "BOLSA" in n and ("6L" in n or "6 L" in n) and "SACHET" not in n:
        return "BOLSA_6L_SIN" if sin_licor else "BOLSA_6L_CON"
    # SACHETS
    if "SACHET" in n or "SACHETS" in g:
        return "SACHET_SIN" if sin_licor else "SACHET_CON"
    # PERLAS
    if "PERLA" in n or "PERLAS" in g:
        if "3400" in n:
            return "PERLAS_3400"
        if "350" in n and "1200" not in n and "3400" not in n:
            return "PERLAS_350"
        return "PERLAS_1200"  # default
    # CREMOSOS
    if "CREMOSO" in n or "CREMOSOS" in g:
        return "CREMOSO"
    return None


def calcular_precio_moda_historica(code: str) -> tuple[float, int] | None:
    """Para productos no contemplados, devuelve (precio_pre_iva, ventas_count)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT items_json FROM siigo_invoices WHERE date >= date('now', '-180 days')"
        ).fetchall()
    finally:
        conn.close()
    precios = []
    for r in rows:
        try:
            items = json.loads(r["items_json"])
        except Exception:
            continue
        for it in items:
            if it.get("code") == code:
                p = float(it.get("price") or 0)
                if p > 0:
                    precios.append(p)
    if not precios:
        return None
    moda = Counter(round(p, 0) for p in precios).most_common(1)[0][0]
    return float(moda), len(precios)


def main():
    conn = get_conn()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        products = conn.execute(
            "SELECT id, code, name, account_group_name, iva_percentage FROM siigo_products WHERE active = 1"
        ).fetchall()

        n_aplicados = 0
        n_skip = 0
        por_bucket: dict[str, int] = {}
        for p in products:
            bucket = clasificar_producto(p["code"], p["name"], p["account_group_name"])
            iva = (p["iva_percentage"] or 19.0) / 100.0
            factor = 1.0 + iva

            if bucket and bucket in PRECIOS_FIJOS:
                detal_con, mayor_con, distri_con = PRECIOS_FIJOS[bucket]
                precios_con_iva = {
                    "DETAL": detal_con,
                    "MAYORISTA": mayor_con,
                    "DISTRIBUIDOR": distri_con,
                }
                por_bucket[bucket] = por_bucket.get(bucket, 0) + 1
            else:
                # Fallback: moda historica para DETAL, -8% mayor, -15% distri
                moda = calcular_precio_moda_historica(p["code"])
                if not moda:
                    n_skip += 1
                    continue
                pre_iva, ventas = moda
                con_iva_detal = pre_iva * factor
                precios_con_iva = {
                    "DETAL": con_iva_detal,
                    "MAYORISTA": con_iva_detal * 0.92,
                    "DISTRIBUIDOR": con_iva_detal * 0.85,
                }
                por_bucket["MODA_HISTORICA"] = por_bucket.get("MODA_HISTORICA", 0) + 1

            for lista, con_iva in precios_con_iva.items():
                pre_iva = con_iva / factor
                # Solo actualizar si NO esta confirmado por dueno
                existente = conn.execute(
                    "SELECT confirmed_by FROM precios_oficiales WHERE product_id = ? AND lista = ?",
                    (p["id"], lista),
                ).fetchone()
                if existente and existente["confirmed_by"] == "dueno":
                    continue  # no pisar lo que el dueno confirmo manualmente
                conn.execute(
                    """INSERT INTO precios_oficiales
                       (product_id, product_code, lista, precio_pre_iva, precio_con_iva, fuente, ventas_referencia, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                       ON CONFLICT(product_id, lista) DO UPDATE SET
                         precio_pre_iva = excluded.precio_pre_iva,
                         precio_con_iva = excluded.precio_con_iva,
                         fuente = excluded.fuente,
                         updated_at = excluded.updated_at""",
                    (p["id"], p["code"], lista, pre_iva, con_iva,
                     bucket or "moda_historica", now),
                )
                n_aplicados += 1

        conn.commit()
        print(f"\nPrecios aplicados: {n_aplicados}")
        print(f"Productos sin datos historicos (skip): {n_skip}")
        print("\nResumen por bucket:")
        for k, v in sorted(por_bucket.items()):
            print(f"  {k:20} {v} productos")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
