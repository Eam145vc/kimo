"""Aplica las reglas confirmadas por el dueno (descarta sugerencias del calibrador).

Reglas del dueno:
  - Hugo Montoya (NIT 111254874): MAYORISTA. Pronto pago: 8 dias = 10%.
  - David Zuniga (NIT 1002547844): MAYORISTA. Pronto pago: 8 dias = 10%.
  - Myriam Rocio Ramirez (NIT 32220877): DISTRIBUIDOR. Sin pronto pago.
  - Resto: DETAL, sin pronto pago.
"""
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from skiimo.db.schema import get_conn


HUGO_ID = "307a83aa-f601-4bde-bcbe-9d94d4ad54ed"
DAVID_ID = "4c77ba58-b73f-4869-8b75-1ab4ecdc8ee2"
MYRIAM_ID = "440ef191-6bae-4a24-b83c-51f863a05afe"


def main():
    conn = get_conn()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        # 1) Limpiar categorias previas (todas las sugerencias del calibrador)
        n = conn.execute("DELETE FROM clientes_categoria").rowcount
        print(f"Eliminadas {n} categorias previas")

        # 2) Insertar solo las confirmadas
        casos = [
            (HUGO_ID, "MAYORISTA", "manual_dueno", "Bolsas 6L a $23K c/IVA + 10% pronto pago 8d"),
            (DAVID_ID, "MAYORISTA", "manual_dueno", "Bolsas 6L a $23K c/IVA + 10% pronto pago 8d"),
            (MYRIAM_ID, "DISTRIBUIDOR", "manual_dueno", "Bolsas 6L a $20K c/IVA. SIN pronto pago. Precio fijo."),
        ]
        for cid, cat, fuente, notas in casos:
            conn.execute(
                """INSERT INTO clientes_categoria
                   (customer_id, categoria, fuente, notas, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (cid, cat, fuente, notas, now, now),
            )
            row = conn.execute("SELECT name FROM siigo_customers WHERE id = ?", (cid,)).fetchone()
            print(f"  [{cat:13}] {row['name']:40} -> {notas}")

        # 3) Limpiar pronto pago previo
        n = conn.execute("DELETE FROM clientes_pronto_pago").rowcount
        print(f"\nEliminadas {n} reglas pronto pago previas")

        # 4) Configurar pronto pago: Hugo y David
        for cid, nombre in [(HUGO_ID, "Hugo"), (DAVID_ID, "David Zuniga")]:
            conn.execute(
                """INSERT INTO clientes_pronto_pago
                   (customer_id, dias_max, descuento_pct, notas, activo, created_at)
                   VALUES (?, 8, 10.0, ?, 1, ?)""",
                (cid, "10% si paga en 8 dias", now),
            )
            print(f"  {nombre}: 8d -> 10%")

        # 5) IMPORTANTE: Reescribir precios oficiales con valores EXACTOS del dueno
        # Para bolsas 6L solamente (codes A1xx y A2xx, no SACHET, no CREMOSO).
        # DETAL = $21,848.74 (precio actual moda)
        # MAYORISTA = $19,327.73 (= $23K c/IVA / 1.19)
        # DISTRIBUIDOR = $16,806.72 (= $20K c/IVA / 1.19)

        # Buscar todos los productos bolsa 6L
        bolsas_query = (
            "SELECT id, code, name FROM siigo_products "
            "WHERE active = 1 "
            "  AND (code LIKE 'A1%' OR code LIKE 'A2%') "
            "  AND name LIKE '%BOLSA%' "
            "  AND name LIKE '%6L%' "
            "  AND name NOT LIKE '%SACHET%' "
            "  AND name NOT LIKE '%CREMOSO%'"
        )
        bolsas = conn.execute(bolsas_query).fetchall()
        print(f"\n{len(bolsas)} productos bolsa 6L detectados, aplicando precios del dueno:")
        precios_bolsa = {
            "DETAL": (21848.74, 26000.0),
            "MAYORISTA": (19327.73, 23000.0),
            "DISTRIBUIDOR": (16806.72, 20000.0),
        }
        upsert_sql = (
            "INSERT INTO precios_oficiales "
            "(product_id, product_code, lista, precio_pre_iva, precio_con_iva, fuente, ventas_referencia, confirmed_by, confirmed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'manual_dueno', 0, 'dueno', ?, ?) "
            "ON CONFLICT(product_id, lista) DO UPDATE SET "
            "  precio_pre_iva = excluded.precio_pre_iva, "
            "  precio_con_iva = excluded.precio_con_iva, "
            "  fuente = 'manual_dueno', "
            "  confirmed_by = 'dueno', "
            "  confirmed_at = excluded.confirmed_at, "
            "  updated_at = excluded.updated_at"
        )
        n_p = 0
        for b in bolsas:
            for lista, (pre, con) in precios_bolsa.items():
                conn.execute(upsert_sql, (b["id"], b["code"], lista, pre, con, now, now))
                n_p += 1
        print(f"  {n_p} entradas de precio insertadas/actualizadas")

        conn.commit()
        print("\nListo. Reglas del dueno aplicadas.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
