"""Registrar un vendedor o admin del bot.

Uso:
    python register_user.py <chat_id> <nombre> [--rol admin] [--seller-id 341]

Ejemplo:
    python register_user.py 123456789 "Oscar" --rol admin
    python register_user.py 987654321 "Frank" --seller-id 716
"""
import argparse
import sys
from datetime import datetime

from skiimo.config import DEFAULT_SELLER_ID
from skiimo.db.schema import get_conn, init_db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chat_id", type=int, help="Telegram chat_id")
    ap.add_argument("nombre", help="Nombre del vendedor")
    ap.add_argument("--rol", default="vendedor", choices=["vendedor", "admin"])
    ap.add_argument("--seller-id", type=int, default=DEFAULT_SELLER_ID,
                    help=f"siigo_seller_id (default {DEFAULT_SELLER_ID})")
    args = ap.parse_args()

    init_db()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO bot_vendedores (telegram_chat_id, nombre, siigo_seller_id, rol, activo, created_at)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(telegram_chat_id) DO UPDATE SET
                 nombre = excluded.nombre,
                 siigo_seller_id = excluded.siigo_seller_id,
                 rol = excluded.rol,
                 activo = 1""",
            (args.chat_id, args.nombre, args.seller_id, args.rol,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        print(f"OK: chat_id={args.chat_id} nombre='{args.nombre}' rol={args.rol} seller_id={args.seller_id}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
