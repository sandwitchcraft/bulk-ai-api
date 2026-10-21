"""App settings, provider registry, and API key storage."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

APP = "bulk-ai"
KEYRING_SERVICE = "bulk-ai"
DEFAULT_CONCURRENCY = 4

BUILTIN_PROVIDERS = [
    {
        "id": "openai",
        "name": "OpenAI (ChatGPT)",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
    },
    {
        "id": "anthropic",
        "name": "Anthropic (Claude)",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "key_env": "ANTHROPIC_API_KEY",
    },
    {
        "id": "gemini",
        "name": "Google (Gemini)",
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
    },
    {
        "id": "xai",
        "name": "xAI (Grok)",
        "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "key_env": "XAI_API_KEY",
    },
]


def config_dir() -> Path:
    override = os.environ.get("BULK_AI_CONFIG")
    if override:
        base = Path(override).expanduser()
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / APP
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / APP
    base.mkdir(parents=True, exist_ok=True)
    return base


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-.")
    return slug or "untitled"


class Config:
    """Persistent settings in <config_dir>/config.json."""

    def __init__(self) -> None:
        self.path = config_dir() / "config.json"
        data = {}
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        self.projects_dir: str = data.get(
            "projects_dir", str(Path.home() / "bulk-ai-projects")
        )
        # Show every request/response line above the progress bars, not just errors.
        self.verbose: bool = bool(data.get("verbose", False))
        # Projects opened from outside projects_dir, so they show up under Open project.
        self.recent_projects: list = data.get("recent_projects", [])
        saved = {p["id"]: p for p in data.get("providers", [])}
        self.providers: list = []
        for builtin in BUILTIN_PROVIDERS:
            p = dict(builtin, builtin=True, models=[], concurrency=DEFAULT_CONCURRENCY)
            if builtin["id"] in saved:
                old = saved.pop(builtin["id"])
                p["models"] = old.get("models", [])
                p["concurrency"] = old.get("concurrency", DEFAULT_CONCURRENCY)
            self.providers.append(p)
        # Whatever is left is user-added custom providers.
        self.providers.extend(saved.values())

    def save(self) -> None:
        write_json_atomic(
            self.path,
            {
                "projects_dir": self.projects_dir,
                "verbose": self.verbose,
                "recent_projects": self.recent_projects,
                "providers": self.providers,
            },
        )

    def projects_path(self) -> Path:
        p = Path(self.projects_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def provider(self, provider_id: str) -> Optional[dict]:
        for p in self.providers:
            if p["id"] == provider_id:
                return p
        return None

    def remember_project(self, path: Path) -> None:
        path = str(Path(path).resolve())
        if path not in self.recent_projects:
            self.recent_projects.append(path)
            self.save()

    def forget_project(self, path: Path) -> None:
        path = str(Path(path).resolve())
        if path in self.recent_projects:
            self.recent_projects.remove(path)
            self.save()

    def add_custom_provider(self, name: str, base_url: str, concurrency: int = DEFAULT_CONCURRENCY) -> dict:
        base_id = slugify(name).lower().replace(":", "-")
        pid, n = base_id, 2
        while self.provider(pid):
            pid, n = f"{base_id}-{n}", n + 1
        p = {
            "id": pid,
            "name": name,
            "kind": "openai",
            "base_url": base_url.rstrip("/"),
            "key_env": None,
            "builtin": False,
            "models": [],
            "concurrency": concurrency,
        }
        self.providers.append(p)
        self.save()
        return p

    def remove_provider(self, provider_id: str) -> None:
        self.providers = [p for p in self.providers if p["id"] != provider_id]
        self.save()


# --- API keys --------------------------------------------------------------
# Lookup order: environment variable, OS keyring, then a 0600 fallback file
# for systems with no usable keyring (e.g. headless Linux).

def _keys_file() -> Path:
    return config_dir() / "keys.json"


def _read_keys_file() -> dict:
    path = _keys_file()
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_keys_file(keys: dict) -> None:
    path = _keys_file()
    write_json_atomic(path, keys)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _keyring():
    try:
        import keyring
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def get_key(provider: dict) -> tuple:
    """Return (key, source) where source is 'env', 'keyring', 'file' or None."""
    env = provider.get("key_env")
    if env and os.environ.get(env):
        return os.environ[env], "env"
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(KEYRING_SERVICE, provider["id"])
            if value:
                return value, "keyring"
        except Exception:
            pass
    value = _read_keys_file().get(provider["id"])
    if value:
        return value, "file"
    return None, None


def set_key(provider: dict, key: str) -> str:
    """Store a key and return where it went ('keyring' or 'file')."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, provider["id"], key)
            keys = _read_keys_file()
            if keys.pop(provider["id"], None) is not None:
                _write_keys_file(keys)
            return "keyring"
        except Exception:
            pass
    keys = _read_keys_file()
    keys[provider["id"]] = key
    _write_keys_file(keys)
    return "file"


def delete_key(provider: dict) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, provider["id"])
        except Exception:
            pass
    keys = _read_keys_file()
    if keys.pop(provider["id"], None) is not None:
        _write_keys_file(keys)


def delete_all_keys(providers: list) -> None:
    for p in providers:
        delete_key(p)
