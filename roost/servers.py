"""The owner's model servers. Public configuration and write-only keys live in separate files."""

import json
import os
from pathlib import Path
from uuid import uuid4

from .provider import THINKING_MODES, base_url

SERVER_IDS = ("primary", "fallback")


class ServerError(ValueError):
    """A refused server setting, safe to show to the owner."""


class ServerStorageError(ServerError):
    """Configuration could not be read or saved; existing files must not be overwritten blindly."""


def _save(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".new")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ServerStorageError("Cannot read model server configuration.") from None


def _key(value) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(not 32 <= ord(char) < 127 for char in value):
        raise ServerError("Model key must be a single line of ASCII text, at most 4096 characters.")
    return value


def _models(models, default_model) -> list[dict]:
    if not isinstance(models, list) or len(models) > 200:
        raise ServerError("Choose at most 200 curated models.")
    cleaned, seen = [], set()
    for model in models:
        if not isinstance(model, dict) or set(model) != {"id", "display_name", "enabled", "thinking_mode"}:
            raise ServerError("Each model needs an id, display_name, enabled and thinking_mode.")
        mid, name = model["id"], model["display_name"]
        if (not isinstance(mid, str) or not mid.strip() or len(mid) > 256 or not mid.isprintable()
                or not isinstance(name, str) or not name.strip() or len(name) > 80 or not name.isprintable()
                or type(model["enabled"]) is not bool or model["thinking_mode"] not in THINKING_MODES):
            raise ServerError("Invalid curated model.")
        if mid in seen:
            raise ServerError("Model IDs must be unique within a server.")
        seen.add(mid)
        cleaned.append(dict(model))
    enabled = {model["id"] for model in cleaned if model["enabled"]}
    if (default_model is not None and (not isinstance(default_model, str) or default_model not in enabled)) or (enabled and default_model is None):
        raise ServerError("Choose an enabled default model, or disable every model.")
    return cleaned


def merge_models(curated: list[dict], discovered: list[str]) -> list[dict]:
    """Keep curated names and controls, including models absent from discovery; new ones start disabled."""
    candidates = {model["id"]: dict(model) for model in curated}
    for mid in discovered:
        candidates.setdefault(mid, {"id": mid, "display_name": mid, "enabled": False, "thinking_mode": "unsupported"})
    return list(candidates.values())


class Servers:
    """Synchronous file changes run on the app's owning thread, with no await between file replacements."""

    def __init__(self, state: Path, *, environ=None):
        self.path, self.keys_path = state / "servers.json", state / "model-keys.json"
        env = os.environ if environ is None else environ
        state.mkdir(parents=True, exist_ok=True)
        try:
            if not self.path.exists():
                primary = {"base_url": "", "models": [], "default_model": None, "key_id": ""}
                keys = {}
                if env.get("ROOST_MODEL_URL"):
                    try:
                        primary["base_url"] = base_url(env["ROOST_MODEL_URL"])
                    except ValueError as exc:
                        raise ServerError(str(exc)) from None
                    if env.get("ROOST_MODEL_KEY_FILE"):
                        try:
                            key = _key(Path(env["ROOST_MODEL_KEY_FILE"]).read_text(encoding="utf-8").strip())
                        except (OSError, UnicodeError):
                            raise ServerStorageError("Cannot read the initial model key file.") from None
                        if key:
                            primary["key_id"] = uuid4().hex
                            keys[primary["key_id"]] = key
                # Commit keys first. A crash can leave an unused key, never a URL with the wrong key.
                _save(self.keys_path, keys)
                _save(self.path, {"version": 1, "servers": {"primary": primary}})
            self._load()
            # Tighten permissions even when an owner restored files with a broader mode.
            os.chmod(self.path, 0o600)
            os.chmod(self.keys_path, 0o600)
            self._prune()
        except OSError:
            raise ServerStorageError("Cannot save model server configuration.") from None

    def _load(self) -> tuple[dict, dict]:
        config, keys = _read(self.path), _read(self.keys_path)
        try:
            if config["version"] != 1 or set(config) != {"version", "servers"}:
                raise ValueError
            records = config["servers"]
            if not isinstance(records, dict) or "primary" not in records or set(records) - set(SERVER_IDS):
                raise ValueError
            for record in records.values():
                if not isinstance(record, dict) or set(record) != {"base_url", "models", "default_model", "key_id"}:
                    raise ValueError
                if record["base_url"]:
                    base_url(record["base_url"])
                elif record["base_url"] != "":
                    raise ValueError
                _models(record["models"], record["default_model"])
                if not isinstance(record["key_id"], str) or (record["key_id"] and record["key_id"] not in keys):
                    raise ValueError
            for value in keys.values():
                _key(value)
        except (ValueError, KeyError, TypeError):
            raise ServerStorageError("Invalid model server configuration; restore the saved files.") from None
        return config, keys

    def _prune(self) -> None:
        config, keys = self._load()
        active = {record["key_id"] for record in config["servers"].values()}
        kept = {ref: key for ref, key in keys.items() if ref in active}
        if kept != keys:
            _save(self.keys_path, kept)

    def list(self) -> list[dict]:
        config, keys = self._load()
        return [{"id": sid, **{key: value for key, value in record.items() if key != "key_id"},
                 "has_key": bool(keys.get(record["key_id"]))} for sid, record in config["servers"].items()]

    def get(self, server_id: str) -> dict:
        for server in self.list():
            if server["id"] == server_id:
                return server
        raise ServerError("Model server is not configured.")

    def key(self, server_id: str) -> str:
        """For server-side provider calls only; never include this value in a response or export."""
        config, keys = self._load()
        if server_id not in config["servers"]:
            raise ServerError("Model server is not configured.")
        return keys.get(config["servers"][server_id]["key_id"], "")

    def update(self, server_id: str, changes: dict) -> dict:
        if server_id not in SERVER_IDS:
            raise ServerError("Choose the primary or fallback server.")
        if not changes or set(changes) - {"base_url", "key", "clear_key"}:
            raise ServerError("Unsupported model server setting.")
        if "clear_key" in changes and type(changes["clear_key"]) is not bool:
            raise ServerError("clear_key must be a boolean.")
        key = _key(changes.get("key", ""))
        if key and changes.get("clear_key"):
            raise ServerError("Choose a new key or clear the old one, not both.")
        config, keys = self._load()
        record = config["servers"].setdefault(server_id, {"base_url": "", "models": [], "default_model": None, "key_id": ""})
        if "base_url" in changes:
            try:
                record["base_url"] = base_url(changes["base_url"])
            except ValueError as exc:
                raise ServerError(str(exc)) from None
        if not record["base_url"]:
            raise ServerError("Set a model base URL first.")
        if changes.get("clear_key"):
            record["key_id"] = ""
        elif key:
            record["key_id"] = uuid4().hex
            keys[record["key_id"]] = key
        try:
            _save(self.keys_path, keys)
            _save(self.path, config)
            self._prune()
        except OSError:
            raise ServerStorageError("Cannot save model server configuration. Retry the change.") from None
        return self.get(server_id)

    def set_models(self, server_id: str, changes: dict) -> dict:
        if set(changes) != {"models", "default_model"}:
            raise ServerError("Provide models and default_model.")
        models = _models(changes["models"], changes["default_model"])
        config, _ = self._load()
        if server_id not in config["servers"]:
            raise ServerError("Model server is not configured.")
        config["servers"][server_id].update(models=models, default_model=changes["default_model"])
        try:
            _save(self.path, config)
        except OSError:
            raise ServerStorageError("Cannot save curated models. Retry the change.") from None
        return self.get(server_id)
