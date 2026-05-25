"""CLI minimo para gestionar usuarios del panel web.

Uso:
  python panel_user.py create <username> <password>
  python panel_user.py passwd <username> <new_password>
  python panel_user.py list
  python panel_user.py disable <username>
"""
from __future__ import annotations

import sys

from skiimo.bootstrap import ensure_db_ready
from skiimo.db.schema import get_conn
from skiimo.panel.auth import crear_usuario, cambiar_password


def cmd_create(username: str, password: str) -> None:
    uid = crear_usuario(username, password, role="admin")
    print(f"OK usuario id={uid} username={username}")


def cmd_passwd(username: str, new_password: str) -> None:
    ok = cambiar_password(username, new_password)
    if ok:
        print(f"OK password cambiada para {username}")
    else:
        print(f"ERROR: usuario {username!r} no existe")
        sys.exit(1)


def cmd_list() -> None:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, username, role, active, created_at, last_login_at "
            "FROM panel_users ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("(sin usuarios)")
        return
    print(f"{'ID':<4}{'USERNAME':<20}{'ROLE':<10}{'ACTIVE':<8}{'LAST_LOGIN':<20}")
    for r in rows:
        print(f"{r['id']:<4}{r['username']:<20}{r['role']:<10}{r['active']:<8}{(r['last_login_at'] or '-'):<20}")


def cmd_disable(username: str) -> None:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE panel_users SET active = 0 WHERE username = ?",
            (username.strip().lower(),),
        )
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount:
        print(f"OK {username} desactivado")
    else:
        print(f"ERROR: usuario {username!r} no existe")


def main() -> None:
    ensure_db_ready()
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "create" and len(sys.argv) == 4:
        cmd_create(sys.argv[2], sys.argv[3])
    elif cmd == "passwd" and len(sys.argv) == 4:
        cmd_passwd(sys.argv[2], sys.argv[3])
    elif cmd == "list":
        cmd_list()
    elif cmd == "disable" and len(sys.argv) == 3:
        cmd_disable(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
