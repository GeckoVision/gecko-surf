"""Nearest-relief geo helpers — pure, deterministic, offline.

The "nearest collection center" answer is a Haversine over coordinates, NOT an LLM
call: reliable and $0. These functions take a plain list of places (from
ReportaVNZLA's ``listRecursos``) and the user's shared location; the bot does the
live fetch and passes the data in, so everything here is unit-testable offline.
"""

from __future__ import annotations

import math
from typing import Any

_EARTH_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometers between two lat/lng points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(a))


def maps_link(lat: float, lng: float) -> str:
    """A Google Maps directions/search link for a coordinate."""
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"


def nearest(
    user_lat: float, user_lng: float, places: list[dict[str, Any]], limit: int = 3
) -> list[dict[str, Any]]:
    """Return the ``limit`` nearest places to the user, each with ``distancia_km``.

    Places without usable coordinates are skipped (many crowdsourced records lack
    them). Coordinates are coerced to float; a malformed pair is skipped, never raised.
    """
    scored: list[tuple[float, dict[str, Any]]] = []
    for place in places:
        lat, lng = place.get("lat"), place.get("lng")
        if lat is None or lng is None:
            continue
        try:
            dist = haversine_km(user_lat, user_lng, float(lat), float(lng))
        except (TypeError, ValueError):
            continue
        scored.append((dist, place))
    scored.sort(key=lambda item: item[0])
    return [{**place, "distancia_km": round(dist, 1)} for dist, place in scored[:limit]]


def format_centros_reply(places: list[dict[str, Any]]) -> str:
    """Plain-text Spanish reply for the nearest collection centers (Telegram-safe)."""
    if not places:
        return (
            "No encontré centros de acopio con ubicación registrada cerca de ti. "
            "Puedes ver el mapa completo en https://reportavnzla.com"
        )
    lines = ["📍 Centros de acopio más cercanos a ti:\n"]
    for i, place in enumerate(places, 1):
        nombre = str(place.get("nombre") or "Centro de acopio").strip()
        dist = place.get("distancia_km")
        lines.append(f"{i}. {nombre} — {dist} km")
        direccion = str(place.get("direccion") or "").strip()
        if direccion:
            lines.append(f"   {direccion}")
        recibe = str(place.get("recibe") or "").strip().splitlines()
        if recibe and recibe[0]:
            lines.append(f"   Recibe: {recibe[0][:70]}")
        lines.append(f"   🗺️ {maps_link(place['lat'], place['lng'])}")
    lines.append("\nFuente: ReportaVNZLA. Emergencias: 171.")
    return "\n".join(lines)


def nearest_help_reply(
    user_lat: float, user_lng: float, centros: list[dict[str, Any]], limit: int = 3
) -> str:
    """One call: find the nearest collection centers and format the Spanish reply."""
    return format_centros_reply(nearest(user_lat, user_lng, centros, limit))
