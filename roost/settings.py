"""Roost's own preferences, shared by every signed-in device: the agent's name, the accent colour
and the time zone. One small JSON file; whatever is in it, `load` returns clean values."""

import json
import os
import re
from pathlib import Path

# Accent presets; the browser holds each one's light and dark colours.
ACCENTS = ("forest", "ocean", "violet", "rose", "amber", "slate")
# Or a custom colour from the picker; the browser derives its light and dark shades.
CUSTOM_ACCENT = re.compile(r"#[0-9a-f]{6}")
# "" follows each device's own time zone; otherwise an IANA name such as "America/New_York".
TIME_ZONE = re.compile(r"(UTC|[A-Za-z]+(/[A-Za-z0-9_+-]+){1,2})")
FIELDS = {"agent_name", "accent", "time_zone", "backend", "search_url"}


class SettingsError(ValueError):
    """A change Roost refuses; the message is shown to the owner."""


def clean_name(value) -> str:
    """A short display name: 1 to 40 printable characters once trimmed, else ""."""
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= 40 and value.strip().isprintable() else ""


def _accent(value) -> str:
    return value if value in ACCENTS or (isinstance(value, str) and CUSTOM_ACCENT.fullmatch(value)) else ""


def _time_zone(value) -> str | None:
    """The zone when valid ("" is the device's own); None when not."""
    return value if isinstance(value, str) and len(value) <= 64 and (not value or TIME_ZONE.fullmatch(value)) else None


def _search_url(value):
    from .provider import base_url
    if value == "":
        return ""
    try:
        return base_url(value)
    except ValueError:
        return None


def load(path: Path) -> dict:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return {"agent_name": clean_name(stored.get("agent_name")),
            "accent": _accent(stored.get("accent")) or ACCENTS[0],
            "time_zone": _time_zone(stored.get("time_zone")) or "",
            "backend": "native" if stored.get("backend") == "native" else "hermes",
            "search_url": _search_url(stored.get("search_url", "")) or ""}


def update(path: Path, changes: dict) -> dict:
    """Apply `changes` and save them; raises SettingsError, saving nothing, if any is refused."""
    if not changes or set(changes) - FIELDS:
        raise SettingsError("Unsupported setting.")
    settings = load(path)
    if "agent_name" in changes:
        value = changes["agent_name"]
        if not isinstance(value, str) or (value.strip() and not clean_name(value)):
            raise SettingsError("The name must be 1 to 40 printable characters, or empty to use Hermes's name.")
        settings["agent_name"] = value.strip()
    if "accent" in changes:
        value = _accent(changes["accent"].lower() if isinstance(changes["accent"], str) else None)
        if not value:
            raise SettingsError("Choose one of the accent colours, or a colour like #2f6f4e.")
        settings["accent"] = value
    if "time_zone" in changes:
        zone = _time_zone(changes["time_zone"])
        if zone is None:
            raise SettingsError("Choose a time zone from the list, or the device's own.")
        settings["time_zone"] = zone
    if "backend" in changes:
        if changes["backend"] not in ("hermes", "native"):
            raise SettingsError("Choose Hermes or Native backend.")
        settings["backend"] = changes["backend"]
    if "search_url" in changes:
        value = _search_url(changes['search_url'])
        if value is None:
            raise SettingsError("Choose an HTTP(S) search base URL without credentials, query or fragment.")
        settings['search_url'] = value
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(settings, stream)
    os.replace(temporary, path)
    return settings
