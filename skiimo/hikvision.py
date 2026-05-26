"""Cliente ISAPI para Hikvision DS-K1T321MFWX (terminal facial de asistencia).

Patron tipo skiimo/siigo_client.py: cliente con httpx + Digest Auth,
parser comun JSON/XML, y helpers de alto nivel.

Endpoints documentados Hikvision V3.x:
  - GET  /ISAPI/System/deviceInfo
  - GET  /ISAPI/System/time
  - POST /ISAPI/AccessControl/AcsEvent       (consultar marcajes)
  - POST /ISAPI/AccessControl/UserInfo/Search (listar personas)
  - POST /ISAPI/AccessControl/UserInfo/Record (alta persona)
  - PUT  /ISAPI/AccessControl/UserInfo/Modify (editar persona)
  - POST /ISAPI/AccessControl/UserInfo/Delete (borrar persona)
  - POST /ISAPI/Intelligent/FDLib/FDSetUp    (subir foto facial)

Devuelve dataclasses simples, no expone httpx ni XML al caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator
from xml.etree import ElementTree as ET

import httpx

from skiimo.config import (
    HIK_HOST,
    HIK_PASSWORD,
    HIK_PORT,
    HIK_TIMEOUT_SECONDS,
    HIK_USER,
)

# Colombia es UTC-05 todo el ano (no aplica DST).
TZ_BOGOTA = timezone(timedelta(hours=-5))


# ---------------------------------------------------------------------------
# Parser comun: el equipo a veces responde JSON, a veces XML segun firmware.
# ---------------------------------------------------------------------------


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _xml_walk(e: ET.Element) -> Any:
    children = list(e)
    if not children:
        return (e.text or "").strip() or None
    out: dict = {}
    for c in children:
        key = _strip_ns(c.tag)
        val = _xml_walk(c)
        if key in out:
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(val)
        else:
            out[key] = val
    return out


def parse_response(resp: httpx.Response) -> dict:
    """Parsea respuesta Hikvision aceptando JSON o XML, sin levantar excepciones."""
    text = resp.text or ""
    ct = (resp.headers.get("content-type") or "").lower()
    if "json" in ct or text.lstrip().startswith("{"):
        try:
            return resp.json()
        except ValueError:
            pass
    if text.lstrip().startswith("<"):
        try:
            root = ET.fromstring(text)
            inner = _xml_walk(root)
            if isinstance(inner, dict):
                # Exponer el contenido bajo el nombre del root para que caller use .get(rootName)
                return {_strip_ns(root.tag): inner, **inner}
            return {_strip_ns(root.tag): inner}
        except ET.ParseError:
            pass
    return {"_raw": text[:500]}


# ---------------------------------------------------------------------------
# Dataclasses de alto nivel
# ---------------------------------------------------------------------------


@dataclass
class HikDeviceInfo:
    model: str | None
    serial: str | None
    firmware: str | None
    mac: str | None
    name: str | None
    raw: dict = field(default_factory=dict)


@dataclass
class HikAcsEvent:
    """Un evento de acceso/marcaje del terminal."""

    event_id: str  # id deterministico para deduplicar
    timestamp: datetime  # tz-aware en UTC-05
    employee_no: str | None  # ID asignado en el equipo
    name: str | None
    card_no: str | None
    major: int  # 5 = access control event
    minor: int  # tipo especifico (e.g. 75 = face authenticated)
    verify_mode: str | None  # face | fingerprint | card | pin | ...
    attendance_status: str | None  # checkIn | checkOut | breakIn | breakOut | overTimeIn | overTimeOut
    picture_url: str | None
    raw: dict = field(default_factory=dict)


@dataclass
class HikPerson:
    employee_no: str
    name: str | None
    user_type: str  # normal | visitor | blackList | maintenance
    gender: str | None
    valid_begin: str | None
    valid_end: str | None
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


class HikClient:
    """Cliente ISAPI sincrono. Usar como context manager.

    Ejemplo:
        with HikClient() as hik:
            info = hik.device_info()
            for ev in hik.iter_events_since(last_seen):
                ...
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.host = host or HIK_HOST
        self.port = port or HIK_PORT
        self.user = user or HIK_USER
        self.password = password or HIK_PASSWORD
        if not self.host or not self.password:
            raise RuntimeError("HIK_HOST o HIK_PASSWORD no configurados")
        self._client = httpx.Client(
            base_url=f"http://{self.host}:{self.port}",
            auth=httpx.DigestAuth(self.user, self.password),
            timeout=timeout or HIK_TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )

    def __enter__(self) -> "HikClient":
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def close(self) -> None:
        self._client.close()

    # ----- helpers HTTP -----
    def _get(self, path: str) -> dict:
        r = self._client.get(path)
        r.raise_for_status()
        return parse_response(r)

    def _post(self, path: str, payload: dict) -> dict:
        r = self._client.post(path, json=payload)
        r.raise_for_status()
        return parse_response(r)

    # ----- info basica -----
    def device_info(self) -> HikDeviceInfo:
        d = self._get("/ISAPI/System/deviceInfo?format=json")
        di = d.get("DeviceInfo", d)
        return HikDeviceInfo(
            model=di.get("model"),
            serial=di.get("serialNumber"),
            firmware=f"{di.get('firmwareVersion', '')} ({di.get('firmwareReleasedDate', '')})".strip(),
            mac=di.get("macAddress"),
            name=di.get("deviceName"),
            raw=di,
        )

    def device_time(self) -> dict:
        d = self._get("/ISAPI/System/time?format=json")
        return d.get("Time", d)

    def healthcheck(self) -> bool:
        """True si el equipo responde a deviceInfo en < timeout."""
        try:
            self.device_info()
            return True
        except Exception:
            return False

    # ----- eventos de acceso (marcajes) -----
    def iter_events(
        self,
        start: datetime,
        end: datetime,
        *,
        major: int = 0,
        minor: int = 0,
        page_size: int = 30,
    ) -> Iterator[HikAcsEvent]:
        """Itera todos los eventos en [start, end] paginando.

        major=0, minor=0 => todos los eventos. Para solo eventos faciales/huella
        autenticados: major=5, minor=75 (face), 76 (fingerprint), 38 (card).
        """
        position = 0
        # Asegurar tz-aware en UTC-05
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ_BOGOTA)
        if end.tzinfo is None:
            end = end.replace(tzinfo=TZ_BOGOTA)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S%z")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%S%z")
        # Hikvision quiere +05:00 con dos puntos, strftime da +0500
        start_str = start_str[:-2] + ":" + start_str[-2:]
        end_str = end_str[:-2] + ":" + end_str[-2:]

        while True:
            payload = {
                "AcsEventCond": {
                    "searchID": f"skiimo-{int(start.timestamp())}-{position}",
                    "searchResultPosition": position,
                    "maxResults": page_size,
                    "major": major,
                    "minor": minor,
                    "startTime": start_str,
                    "endTime": end_str,
                }
            }
            d = self._post("/ISAPI/AccessControl/AcsEvent?format=json", payload)
            ev = d.get("AcsEvent", {})
            info_list = ev.get("InfoList") or []
            if isinstance(info_list, dict):
                info_list = [info_list]
            for raw in info_list:
                yield _event_from_raw(raw)
            num = int(ev.get("numOfMatches", 0) or 0)
            total = int(ev.get("totalMatches", 0) or 0)
            position += num
            if num == 0 or position >= total:
                break

    # ----- personas -----
    def iter_persons(self, *, page_size: int = 30) -> Iterator[HikPerson]:
        position = 0
        while True:
            payload = {
                "UserInfoSearchCond": {
                    "searchID": f"skiimo-persons-{position}",
                    "searchResultPosition": position,
                    "maxResults": page_size,
                }
            }
            d = self._post("/ISAPI/AccessControl/UserInfo/Search?format=json", payload)
            block = d.get("UserInfoSearch", {})
            users = block.get("UserInfo") or []
            if isinstance(users, dict):
                users = [users]
            for raw in users:
                yield _person_from_raw(raw)
            num = int(block.get("numOfMatches", 0) or 0)
            total = int(block.get("totalMatches", 0) or 0)
            position += num
            if num == 0 or position >= total:
                break

    def count_persons(self) -> int:
        d = self._get("/ISAPI/AccessControl/UserInfo/Count?format=json")
        c = d.get("UserInfoCount", {})
        return int(c.get("userNumber", 0) or 0)

    # ----- alta de persona -----
    def add_person(
        self,
        employee_no: str,
        name: str,
        *,
        user_type: str = "normal",
        gender: str = "unknown",
        valid_days: int = 365 * 10,
    ) -> dict:
        """Crea o actualiza una persona en el equipo. Idempotente por employee_no."""
        now = datetime.now(TZ_BOGOTA)
        end = now + timedelta(days=valid_days)
        payload = {
            "UserInfo": {
                "employeeNo": str(employee_no),
                "name": name,
                "userType": user_type,
                "gender": gender,
                "Valid": {
                    "enable": True,
                    "beginTime": now.strftime("%Y-%m-%dT%H:%M:%S"),
                    "endTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeType": "local",
                },
            }
        }
        return self._post("/ISAPI/AccessControl/UserInfo/Record?format=json", payload)

    def delete_person(self, employee_no: str) -> dict:
        payload = {
            "UserInfoDelCond": {
                "EmployeeNoList": [{"employeeNo": str(employee_no)}],
            }
        }
        return self._post("/ISAPI/AccessControl/UserInfo/Delete?format=json", payload)

    # ----- HTTP Listening Host (push de eventos a un servidor externo) -----
    def get_http_hosts(self) -> dict:
        """Devuelve la lista de HTTP listening hosts configurados."""
        return self._get("/ISAPI/Event/notification/httpHosts?format=json")

    def set_http_host(
        self,
        host_id: int,
        *,
        ip: str,
        port: int = 80,
        url: str = "/api/hik/event",
        protocol: str = "HTTP",
        parameter_format: str = "json",
        user: str | None = None,
        password: str | None = None,
        host_name: str = "skiimo-panel",
    ) -> dict:
        """Configura UN HTTP listening host. host_id suele ser 1, 2 o 3.

        protocol: HTTP | HTTPS
        parameter_format: json | XML
        """
        # Construir XML porque el endpoint individual es mas confiable con XML
        # (algunos firmwares ignoran ?format=json en httpHosts)
        auth_block = ""
        if user and password:
            auth_block = f"<httpAuthenticationMethod>digest</httpAuthenticationMethod><userName>{user}</userName><password>{password}</password>"
        xml_body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<HttpHostNotification version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<id>{host_id}</id>"
            f"<url>{url}</url>"
            f"<protocolType>{protocol}</protocolType>"
            f"<parameterFormatType>{parameter_format}</parameterFormatType>"
            "<addressingFormatType>ipaddress</addressingFormatType>"
            f"<ipAddress>{ip}</ipAddress>"
            f"<portNo>{port}</portNo>"
            f"<httpAuthenticationMethod>none</httpAuthenticationMethod>"
            f"<httpBroken>true</httpBroken>"
            f"<uploadImagesDataType>URL</uploadImagesDataType>"
            f"</HttpHostNotification>"
        )
        if auth_block:
            xml_body = xml_body.replace("<httpAuthenticationMethod>none</httpAuthenticationMethod>", auth_block)
        r = self._client.put(
            f"/ISAPI/Event/notification/httpHosts/{host_id}",
            content=xml_body,
            headers={"Content-Type": "application/xml"},
        )
        r.raise_for_status()
        return parse_response(r)

    def test_http_host(self, host_id: int) -> dict:
        """Dispara un POST de prueba al host configurado para verificar conectividad."""
        r = self._client.post(f"/ISAPI/Event/notification/httpHosts/{host_id}/test")
        r.raise_for_status()
        return parse_response(r)


# ---------------------------------------------------------------------------
# Builders raw -> dataclass
# ---------------------------------------------------------------------------


def _parse_hik_time(s: str | None) -> datetime:
    """Parsea timestamp Hikvision (e.g. '2026-05-26T13:53:58-05:00')."""
    if not s:
        return datetime.now(TZ_BOGOTA)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # Fallback: sin tz info
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=TZ_BOGOTA)
        except Exception:
            return datetime.now(TZ_BOGOTA)


def _event_from_raw(raw: dict) -> HikAcsEvent:
    # Hikvision puede mandar employeeNo o employeeNoString
    emp = raw.get("employeeNoString") or raw.get("employeeNo")
    ts = _parse_hik_time(raw.get("time"))
    # construir id deterministico: timestamp + serialNo + tipo
    sn = raw.get("serialNo") or "0"
    event_id = f"{int(ts.timestamp())}-{sn}-{raw.get('major')}-{raw.get('minor')}-{emp or 'x'}"
    return HikAcsEvent(
        event_id=event_id,
        timestamp=ts,
        employee_no=str(emp) if emp else None,
        name=raw.get("name") or None,
        card_no=raw.get("cardNo") or None,
        major=int(raw.get("major", 0) or 0),
        minor=int(raw.get("minor", 0) or 0),
        verify_mode=raw.get("currentVerifyMode") or raw.get("verifyNo") or None,
        attendance_status=raw.get("attendanceStatus") or None,
        picture_url=raw.get("pictureURL") or None,
        raw=raw,
    )


def _person_from_raw(raw: dict) -> HikPerson:
    return HikPerson(
        employee_no=str(raw.get("employeeNo", "")),
        name=raw.get("name") or None,
        user_type=raw.get("userType") or "normal",
        gender=raw.get("gender") or None,
        valid_begin=(raw.get("Valid") or {}).get("beginTime") if isinstance(raw.get("Valid"), dict) else None,
        valid_end=(raw.get("Valid") or {}).get("endTime") if isinstance(raw.get("Valid"), dict) else None,
        raw=raw,
    )
