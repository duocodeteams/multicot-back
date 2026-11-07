"""Catálogos públicos para el formulario de pasajeros Inter Assist.

Países: REST Countries. Provincias AR: Georef (datos.gob.ar).
No se envían a emitir-externa; solo alimentan country_id / province_id locales.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 20.0
_COUNTRIES_URL = "https://restcountries.com/v3.1/all?fields=cca2,name,translations"
_PROVINCES_URL = (
    "https://apis.datos.gob.ar/georef/api/provincias?campos=id,nombre&max=30&orden=nombre"
)

# Fallback si REST Countries no responde.
_FALLBACK_COUNTRIES: list[dict[str, str]] = [
    {"id": "AR", "name": "Argentina"},
    {"id": "BO", "name": "Bolivia"},
    {"id": "BR", "name": "Brasil"},
    {"id": "CL", "name": "Chile"},
    {"id": "CO", "name": "Colombia"},
    {"id": "EC", "name": "Ecuador"},
    {"id": "ES", "name": "España"},
    {"id": "MX", "name": "México"},
    {"id": "PY", "name": "Paraguay"},
    {"id": "PE", "name": "Perú"},
    {"id": "UY", "name": "Uruguay"},
    {"id": "US", "name": "Estados Unidos"},
    {"id": "VE", "name": "Venezuela"},
]

# Fallback Georef (ids oficiales INDEC).
_FALLBACK_AR_PROVINCES: list[dict[str, str]] = [
    {"id": "02", "name": "Ciudad Autónoma de Buenos Aires"},
    {"id": "06", "name": "Buenos Aires"},
    {"id": "10", "name": "Catamarca"},
    {"id": "14", "name": "Córdoba"},
    {"id": "18", "name": "Corrientes"},
    {"id": "22", "name": "Chaco"},
    {"id": "26", "name": "Chubut"},
    {"id": "30", "name": "Entre Ríos"},
    {"id": "34", "name": "Formosa"},
    {"id": "38", "name": "Jujuy"},
    {"id": "42", "name": "La Pampa"},
    {"id": "46", "name": "La Rioja"},
    {"id": "50", "name": "Mendoza"},
    {"id": "54", "name": "Misiones"},
    {"id": "58", "name": "Neuquén"},
    {"id": "62", "name": "Río Negro"},
    {"id": "66", "name": "Salta"},
    {"id": "70", "name": "San Juan"},
    {"id": "74", "name": "San Luis"},
    {"id": "78", "name": "Santa Cruz"},
    {"id": "82", "name": "Santa Fe"},
    {"id": "86", "name": "Santiago del Estero"},
    {"id": "90", "name": "Tucumán"},
    {"id": "94", "name": "Tierra del Fuego, Antártida e Islas del Atlántico Sur"},
]

_countries_cache: list[dict[str, str]] | None = None
_ar_provinces_cache: list[dict[str, str]] | None = None


def list_inter_assist_countries(*, client: httpx.Client | None = None) -> list[dict[str, str]]:
    global _countries_cache
    if _countries_cache is not None:
        return list(_countries_cache)

    owns = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        response = client.get(_COUNTRIES_URL, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
        items = _parse_countries(payload)
        if not items:
            raise ValueError("respuesta vacía")
        _countries_cache = items
        return list(items)
    except Exception:
        logger.exception("InterAssist catalog: falló REST Countries, uso fallback")
        _countries_cache = list(_FALLBACK_COUNTRIES)
        return list(_countries_cache)
    finally:
        if owns:
            client.close()


def list_inter_assist_provinces(
    country_id: str,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, str]]:
    code = country_id.strip().upper()
    if not code or "/" in code:
        return []
    if code != "AR":
        # Georef solo cubre Argentina; otros países sin provincias por ahora.
        return []
    return _list_ar_provinces(client=client)


def _list_ar_provinces(*, client: httpx.Client | None = None) -> list[dict[str, str]]:
    global _ar_provinces_cache
    if _ar_provinces_cache is not None:
        return list(_ar_provinces_cache)

    owns = client is None
    if client is None:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        response = client.get(_PROVINCES_URL, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
        items = _parse_ar_provinces(payload)
        if not items:
            raise ValueError("respuesta vacía")
        _ar_provinces_cache = items
        return list(items)
    except Exception:
        logger.exception("InterAssist catalog: falló Georef, uso fallback provincias AR")
        _ar_provinces_cache = list(_FALLBACK_AR_PROVINCES)
        return list(_ar_provinces_cache)
    finally:
        if owns:
            client.close()


def _parse_countries(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, list):
        return []
    items: list[dict[str, str]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        code = str(row.get("cca2") or "").strip().upper()
        if len(code) != 2:
            continue
        name = _country_name_es(row) or str((row.get("name") or {}).get("common") or "").strip()
        if not name:
            continue
        items.append({"id": code, "name": name})
    # Argentina primero; el resto alfabético en español.
    items.sort(key=lambda item: (0 if item["id"] == "AR" else 1, item["name"].casefold()))
    return items


def _country_name_es(row: dict[str, Any]) -> str:
    translations = row.get("translations")
    if not isinstance(translations, dict):
        return ""
    spa = translations.get("spa")
    if isinstance(spa, dict):
        return str(spa.get("common") or spa.get("official") or "").strip()
    return ""


def _parse_ar_provinces(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("provincias")
    if not isinstance(rows, list):
        return []
    items: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        province_id = str(row.get("id") or "").strip()
        name = str(row.get("nombre") or "").strip()
        if not province_id or not name:
            continue
        items.append({"id": province_id, "name": name})
    items.sort(key=lambda item: item["name"].casefold())
    return items
