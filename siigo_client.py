"""Cliente minimo para Siigo API. Solo lo necesario para explorar la cuenta."""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()


class SiigoClient:
    def __init__(
        self,
        username: str | None = None,
        access_key: str | None = None,
        base_url: str | None = None,
        partner_id: str | None = None,
    ) -> None:
        self.username = username or os.environ["SIIGO_USERNAME"]
        self.access_key = access_key or os.environ["SIIGO_ACCESS_KEY"]
        self.base_url = (base_url or os.environ.get("SIIGO_BASE_URL", "https://api.siigo.com")).rstrip("/")
        self.partner_id = partner_id or os.environ.get("SIIGO_PARTNER_ID", "Skiimo")
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._client = httpx.Client(timeout=30.0)

    def _authenticate(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = self._client.post(
            f"{self.base_url}/auth",
            json={"username": self.username, "access_key": self.access_key},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 86400))
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._authenticate()}",
            "Partner-Id": self.partner_id,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._client.get(self._url(path), headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(self._url(path), headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json()

    def put(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.put(self._url(path), headers=self._headers(), json=json)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def delete(self, path: str) -> int:
        resp = self._client.delete(self._url(path), headers=self._headers())
        resp.raise_for_status()
        return resp.status_code

    def get_all_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        page_size: int = 100,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        params = dict(params or {})
        params["page_size"] = page_size
        page = 1
        while page <= max_pages:
            params["page"] = page
            data = self.get(path, params=params)
            items = data.get("results") if isinstance(data, dict) else None
            if items is None:
                if isinstance(data, list):
                    results.extend(data)
                break
            results.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return results

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SiigoClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
