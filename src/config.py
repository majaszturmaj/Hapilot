"""Hapilot settings — the one place that reads the environment (.env) and config/profiles.yaml.

Every tunable value lives here; modules call ``get_settings()`` instead of reading os.environ.
Model endpoints are restricted to local/LAN addresses (see ``is_local_url``).
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Invalid or missing configuration."""


class NotLocalEndpoint(ConfigError):
    """A model endpoint points outside the local machine / LAN."""


# --------------------------------------------------------------------------- endpoints

def is_local_url(url: str) -> bool:
    """True for loopback, private (RFC 1918 / ULA) and *.local / *.localhost hosts."""
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def assert_local_url(url: str) -> None:
    if not is_local_url(url):
        raise NotLocalEndpoint(
            f"Endpoint {url!r} is not a local/LAN address. Hapilot only talks to models on this machine or the local network."
        )


# --------------------------------------------------------------------------- profiles

@dataclass(frozen=True)
class ModelSpec:
    """One model as seen by Hapilot: an OpenAI-compatible endpoint plus request parameters."""

    key: str
    base_url: str
    model: str
    api_key: str = "local"
    temperature: float = 0.3
    max_tokens: Optional[int] = 2500
    timeout: float = 600.0
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    seed: Optional[int] = None


@dataclass(frozen=True)
class Profile:
    """Maps agent roles (planner, knowledge, writer, coder, …) to models."""

    name: str
    description: str
    models: Mapping[str, ModelSpec]
    roles: Mapping[str, Mapping[str, Any]]  # role -> {"model": key, overrides...}

    def model_for(self, role: str) -> ModelSpec:
        entry = self.roles.get(role) or self.roles.get("default")
        if entry is None:
            raise ConfigError(f"Profile {self.name!r} has no model for role {role!r} and no 'default' role")
        spec = self.models[entry["model"]]
        overrides = {k: v for k, v in entry.items() if k in ("temperature", "max_tokens", "timeout")}
        if overrides:
            spec = ModelSpec(**{**spec.__dict__, **overrides})
        return spec

    def default_model(self) -> ModelSpec:
        return self.model_for("default")


_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand(value: Any, env: Mapping[str, str]) -> Any:
    """Expand ${VAR} and ${VAR:-default} in strings, recursively."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: env.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    return value


def load_profiles(path: Path, env: Mapping[str, str]) -> tuple[dict[str, Profile], str]:
    """Parse the profiles file. Returns (profiles, default profile name)."""
    if not path.is_file():
        raise ConfigError(f"Profiles file not found: {path}")
    raw = _expand(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, env)
    endpoint = raw.get("endpoint", "")
    profiles: dict[str, Profile] = {}
    for name, body in (raw.get("profiles") or {}).items():
        models = {}
        for key, m in (body.get("models") or {}).items():
            base_url = m.get("base_url") or endpoint
            if not base_url or not m.get("model"):
                raise ConfigError(f"Profile {name!r}, model {key!r}: 'model' and an endpoint are required")
            assert_local_url(base_url)
            models[key] = ModelSpec(
                key=key,
                base_url=base_url.rstrip("/"),
                model=m["model"],
                api_key=m.get("api_key", "local"),
                temperature=float(m.get("temperature", 0.3)),
                max_tokens=m.get("max_tokens", 2500),
                timeout=float(m.get("timeout", 600)),
                extra_body=m.get("extra_body") or {},
            )
        roles = {}
        for role, entry in (body.get("roles") or {}).items():
            entry = {"model": entry} if isinstance(entry, str) else dict(entry)
            if entry.get("model") not in models:
                raise ConfigError(f"Profile {name!r}, role {role!r}: unknown model {entry.get('model')!r}")
            roles[role] = entry
        if "default" not in roles:
            raise ConfigError(f"Profile {name!r} needs a 'default' role")
        profiles[name] = Profile(name=name, description=body.get("description", ""), models=models, roles=roles)
    if not profiles:
        raise ConfigError(f"No profiles defined in {path}")
    default = raw.get("default") or next(iter(profiles))
    if default not in profiles:
        raise ConfigError(f"Default profile {default!r} is not defined in {path}")
    return profiles, default


# --------------------------------------------------------------------------- settings

@dataclass(frozen=True)
class Settings:
    data_dir: Path
    workspace_root: Path
    state_dir: Path
    docs_dir: Path
    chroma_path: Path
    collection: str
    embedding_model: str
    rag_top_k: int
    profiles: Mapping[str, Profile]
    default_profile: str
    plan_mode: str
    approval_policy: str
    recursion_limit: int
    python_timeout: int
    latex_timeout: int
    sandbox_image: str
    sandbox_memory: str
    sandbox_cpus: float
    sandbox_pids: int
    max_edit_chars: int
    n8n_webhook_base: str

    def profile(self, name: Optional[str] = None) -> Profile:
        name = name or self.default_profile
        if name not in self.profiles:
            raise ConfigError(f"Unknown profile {name!r}; defined: {', '.join(self.profiles)}")
        return self.profiles[name]


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build Settings from an environment mapping (defaults to os.environ after loading .env)."""
    if env is None:
        load_dotenv(PROJECT_ROOT / ".env")
        env = os.environ

    def get(name: str, default: str) -> str:
        return env.get(name, default)

    def path(name: str, default: Path) -> Path:
        p = Path(get(name, str(default))).expanduser()
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    data_dir = path("HAPILOT_DATA_DIR", PROJECT_ROOT / "var")
    profiles, default_profile = load_profiles(path("HAPILOT_PROFILES_FILE", PROJECT_ROOT / "config" / "profiles.yaml"), env)
    default_profile = get("HAPILOT_PROFILE", default_profile)
    if default_profile not in profiles:
        raise ConfigError(f"HAPILOT_PROFILE={default_profile!r} is not defined; defined: {', '.join(profiles)}")

    plan_mode = get("HAPILOT_PLAN_MODE", "auto")
    if plan_mode not in ("auto", "explicit", "off"):
        raise ConfigError("HAPILOT_PLAN_MODE must be auto, explicit or off")
    approval = get("HAPILOT_APPROVAL_POLICY", "ask")
    if approval not in ("ask", "auto"):
        raise ConfigError("HAPILOT_APPROVAL_POLICY must be ask or auto")
    n8n_base = get("HAPILOT_N8N_WEBHOOK_BASE", "http://127.0.0.1:8678/webhook").rstrip("/")
    assert_local_url(n8n_base)  # used only when the optional n8n_operations extension is enabled

    return Settings(
        data_dir=data_dir,
        workspace_root=path("HAPILOT_WORKSPACE_DIR", data_dir / "workspace"),
        state_dir=path("HAPILOT_STATE_DIR", data_dir / "task_states"),
        docs_dir=path("HAPILOT_DOCS_DIR", PROJECT_ROOT / "examples" / "demo_company" / "docs"),
        chroma_path=path("HAPILOT_CHROMA_DIR", data_dir / "chroma"),
        collection=get("HAPILOT_COLLECTION", "company_knowledge"),
        embedding_model=get("HAPILOT_EMBEDDING_MODEL", "intfloat/multilingual-e5-base"),
        rag_top_k=int(get("HAPILOT_RAG_TOP_K", "8")),
        profiles=profiles,
        default_profile=default_profile,
        plan_mode=plan_mode,
        approval_policy=approval,
        recursion_limit=int(get("HAPILOT_RECURSION_LIMIT", "50")),
        python_timeout=int(get("HAPILOT_PYTHON_TIMEOUT", "60")),
        latex_timeout=int(get("HAPILOT_LATEX_TIMEOUT", "120")),
        sandbox_image=get("HAPILOT_SANDBOX_IMAGE", "hapilot-sandbox:0.1"),
        sandbox_memory=get("HAPILOT_SANDBOX_MEMORY", "1g"),
        sandbox_cpus=float(get("HAPILOT_SANDBOX_CPUS", "1.0")),
        sandbox_pids=int(get("HAPILOT_SANDBOX_PIDS", "128")),
        max_edit_chars=int(get("HAPILOT_MAX_EDIT_CHARS", "20000")),
        n8n_webhook_base=n8n_base,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings() -> None:
    """Forget cached settings (tests, or after changing the environment)."""
    get_settings.cache_clear()
