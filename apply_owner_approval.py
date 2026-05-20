"""Aplica las 16 categorias aprobadas por el dueno.

DISTRIBUIDOR (3): Diego Zuluaga, Grupo Inversiones Cuartas, Michael Rada
MAYORISTA (13): Johan Bayona, Anderson Cuadros, Erick Gomez, Oswaldo Lopez,
                Rojas Catalina, Edwar Jhair Rojas, Juan Cogollo, Andrea Giraldo,
                Golden BG, Cindy Rojas, Yeison Araque, Luis Felipe Rojas, Carmen Toro

Ya aplicados antes: Hugo (MAYORISTA), David Zuniga (MAYORISTA), Myriam (DISTRIBUIDOR).
"""
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from skiimo.db.schema import get_conn


# (nit, categoria, notas)
APROBADOS = [
    # DISTRIBUIDOR - precio bolsa $20K
    ("70955081", "DISTRIBUIDOR", "Cliente top: 3556 bolsas/ano, $71.8M en compras"),
    ("901883785", "DISTRIBUIDOR", "Empresa: 680 bolsas/ano a $20K"),
    ("1016080924", "DISTRIBUIDOR", "105 bolsas/ano precio cercano a $20K"),
    # MAYORISTA - precio bolsa $23K
    ("1098659379", "MAYORISTA", "Johan Bayona - 1445 bolsas/ano"),
    ("1098809699", "MAYORISTA", "Anderson Cuadros - 1400 bolsas/ano"),
    ("1099739465", "MAYORISTA", "Erick Gomez - 888 bolsas/ano"),
    ("394856748", "MAYORISTA", "Oswaldo Lopez - 741 bolsas/ano"),
    ("1022437410", "MAYORISTA", "Laura Catalina Rojas - 639 bolsas/ano"),
    ("1022421383", "MAYORISTA", "Edwar Jhair Rojas - 572 bolsas/ano"),
    ("1137975754", "MAYORISTA", "Juan Cogollo - 516 bolsas/ano"),
    ("1034289768", "MAYORISTA", "Andrea Giraldo - 540 bolsas/ano"),
    ("901964827", "MAYORISTA", "Golden BG SAS - 504 bolsas/ano"),
    ("1022361280", "MAYORISTA", "Cindy Rojas - 341 bolsas/ano"),
    ("1096065921", "MAYORISTA", "Yeison Araque - 168 bolsas/ano"),
    ("1033651213", "MAYORISTA", "Luis Felipe Rojas - 108 bolsas/ano"),
    ("1103221973", "MAYORISTA", "Carmen Milena Toro - 97 bolsas/ano"),
]


def main():
    conn = get_conn()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        applied = 0
        skipped = []
        for nit, cat, notas in APROBADOS:
            row = conn.execute(
                "SELECT id, name FROM siigo_customers WHERE identification = ?",
                (nit,),
            ).fetchone()
            if not row:
                skipped.append((nit, "no encontrado en DB"))
                continue
            conn.execute(
                """INSERT INTO clientes_categoria
                   (customer_id, categoria, fuente, notas, confirmed_by, created_at, updated_at)
                   VALUES (?, ?, 'aprobado_dueno', ?, 'dueno', ?, ?)
                   ON CONFLICT(customer_id) DO UPDATE SET
                     categoria = excluded.categoria,
                     fuente = excluded.fuente,
                     notas = excluded.notas,
                     confirmed_by = 'dueno',
                     updated_at = excluded.updated_at""",
                (row["id"], cat, notas, now, now),
            )
            print(f"  [{cat:13}] {row['name'][:42]:42} (NIT {nit})")
            applied += 1
        conn.commit()
        print(f"\nTotal aplicados: {applied}")
        if skipped:
            print(f"\nNo encontrados: {skipped}")

        # Resumen final
        print("\n=== ESTADO ACTUAL DE CATEGORIAS ===")
        cur = conn.execute(
            """SELECT cc.categoria, COUNT(*) as n FROM clientes_categoria cc
               GROUP BY cc.categoria ORDER BY cc.categoria"""
        )
        for r in cur:
            print(f"  {r['categoria']:13} {r['n']} clientes")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
