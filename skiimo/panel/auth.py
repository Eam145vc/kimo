"""Autenticacion del panel web. bcrypt + cookie de sesion."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import bcrypt

from skiimo.db.schema import get_conn


SESSION_TTL_HOURS = 24 * 7  # 1 semana


def crear_usuario(username: str, password: str, role: str = "admin") -> int:
    """Crea un panel_user. Devuelve id. Hashea password con bcrypt."""
    username = username.strip().lower()
    if not username or len(password) < 6:
        raise ValueError("Usuario requerido y password minimo 6 chars")
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO panel_users (username, password_hash, role, active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (username, pw_hash, role, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def cambiar_password(username: str, new_password: str) -> bool:
    if len(new_password) < 6:
        raise ValueError("Password minimo 6 chars")
    pw_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE panel_users SET password_hash = ? WHERE username = ?",
            (pw_hash, username.strip().lower()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def autenticar(username: str, password: str) -> dict | None:
    """Devuelve el dict del user si credenciales OK, None si no."""
    username = username.strip().lower()
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, password_hash, role, active FROM panel_users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if not row["active"]:
        return None
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8"))
    except Exception:
        return None
    if not ok:
        return None
    # Actualizar last_login
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE panel_users SET last_login_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), row["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return dict(row)


def crear_sesion(user_id: int, ip: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(hours=SESSION_TTL_HOURS)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO panel_sessions (token, user_id, created_at, expires_at, ip) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, user_id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds"), ip),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def validar_sesion(token: str | None) -> dict | None:
    """Devuelve el user dict si el token es valido y no expiro."""
    if not token:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT s.token, s.user_id, s.expires_at, u.username, u.role, u.active
               FROM panel_sessions s
               JOIN panel_users u ON u.id = s.user_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if not row["active"]:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            return None
    except Exception:
        return None
    return {
        "id": row["user_id"],
        "username": row["username"],
        "role": row["role"],
        "token": row["token"],
    }


def cerrar_sesion(token: str) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM panel_sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def limpiar_sesiones_expiradas() -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM panel_sessions WHERE expires_at < ?",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
