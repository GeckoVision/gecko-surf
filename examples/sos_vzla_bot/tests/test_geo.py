"""Nearest-help geo helpers — pure, offline, deterministic."""

from __future__ import annotations

from examples.sos_vzla_bot.geo import (
    haversine_km,
    maps_link,
    nearest,
    nearest_help_reply,
)


def test_haversine_caracas_to_maracay_is_about_90km() -> None:
    d = haversine_km(10.4806, -66.9036, 10.2469, -67.5958)
    assert 60 < d < 110


def test_nearest_sorts_by_distance_and_skips_missing_coords() -> None:
    places = [
        {"nombre": "A (aquí)", "lat": 10.50, "lng": -66.90},
        {"nombre": "B (sin coords)", "lat": None, "lng": None},
        {"nombre": "C (cerca)", "lat": 10.49, "lng": -66.91},
        {"nombre": "D (lejos)", "lat": 8.60, "lng": -71.14},
    ]
    out = nearest(10.50, -66.90, places, limit=3)
    assert [p["nombre"] for p in out] == ["A (aquí)", "C (cerca)", "D (lejos)"]
    assert out[0]["distancia_km"] == 0.0
    assert all("distancia_km" in p for p in out)


def test_malformed_coords_are_skipped_not_raised() -> None:
    assert nearest(10.0, -66.0, [{"nombre": "x", "lat": "nope", "lng": -66.0}]) == []


def test_reply_has_center_name_maps_link_and_recibe() -> None:
    places = [
        {
            "nombre": "Centro X",
            "lat": 10.5,
            "lng": -66.9,
            "direccion": "Av 1",
            "recibe": "Agua\nComida",
        }
    ]
    r = nearest_help_reply(10.5, -66.9, places)
    assert "Centro X" in r
    assert maps_link(10.5, -66.9) in r
    assert "Recibe: Agua" in r


def test_empty_gives_friendly_spanish_fallback() -> None:
    r = nearest_help_reply(10.0, -66.0, [])
    assert "reportavnzla.com" in r and "cerca" in r.lower()
