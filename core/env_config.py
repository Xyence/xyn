"""Canonical environment loader for xyn-seed bootstrap."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_LOADED = False
AI_MODEL_DEFAULTS = {
    "openai": "gpt-5-mini",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-3-7-sonnet-latest",
}
AI_PROVIDER_KEYS = {
    "openai": ("XYN_OPENAI_API_KEY", "OPENAI_API_KEY"),
    "gemini": ("XYN_GEMINI_API_KEY", "XYN_GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("XYN_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
}


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key] = value
    return values


def _load_seed_dotenv_once() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    for key, value in _read_dotenv(env_path).items():
        os.environ.setdefault(key, value)
    _ENV_LOADED = True


def _env(key: str, default: Optional[str] = None, aliases: tuple[str, ...] = ()) -> str:
    direct = os.getenv(key)
    if direct is not None and str(direct).strip() != "":
        return str(direct).strip()
    for alias in aliases:
        aliased = os.getenv(alias)
        if aliased is not None and str(aliased).strip() != "":
            if key not in os.environ:
                os.environ[key] = str(aliased).strip()
            return str(aliased).strip()
    return default or ""


@dataclass(frozen=True)
class SeedConfig:
    env: str
    base_domain: str
    auth_mode: str
    internal_token: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_redirect_uri: str
    ai_provider: str
    ai_model: str
    ai_enabled: bool
    openai_api_key: str
    gemini_api_key: str
    anthropic_api_key: str
    secret_key: str
    credentials_encryption_key: str
    database_url: str
    redis_url: str


def _resolve_ai_provider_and_keys() -> tuple[str, bool, dict[str, str]]:
    keys = {
        provider: _env(alias_set[0], "", aliases=alias_set[1:])
        for provider, alias_set in AI_PROVIDER_KEYS.items()
    }
    available = [provider for provider, value in keys.items() if value]
    explicit = _env("XYN_AI_PROVIDER", "").strip().lower()
    if explicit:
        if explicit not in {"openai", "gemini", "anthropic", "none", "disabled"}:
            raise RuntimeError("XYN_AI_PROVIDER must be one of: openai|gemini|anthropic|none")
        if explicit in {"none", "disabled"}:
            return "none", False, keys
        if not keys.get(explicit):
            required_key = AI_PROVIDER_KEYS[explicit][0]
            raise RuntimeError(f"XYN_AI_PROVIDER={explicit} requires {required_key}")
        return explicit, True, keys
    if len(available) == 0:
        return "none", False, keys
    if keys.get("openai"):
        return "openai", True, keys
    if len(available) == 1:
        return available[0], True, keys
    raise RuntimeError("Multiple AI provider keys are set; specify XYN_AI_PROVIDER explicitly")


def _default_ai_model(provider: str) -> str:
    explicit = _env("XYN_AI_MODEL", "")
    if explicit:
        return explicit
    if provider in AI_MODEL_DEFAULTS:
        return AI_MODEL_DEFAULTS[provider]
    return "none"


def load_seed_config() -> SeedConfig:
    _load_seed_dotenv_once()

    env = _env("XYN_ENV", "local").lower()
    if env not in {"local", "dev", "prod"}:
        raise RuntimeError("XYN_ENV must be one of: local|dev|prod")

    auth_mode = _env("XYN_AUTH_MODE", "simple").lower()
    if auth_mode not in {"simple", "oidc"}:
        raise RuntimeError("XYN_AUTH_MODE must be one of: simple|oidc")

    base_domain = _env("XYN_BASE_DOMAIN", "", aliases=("DOMAIN",))
    internal_token = _env("XYN_INTERNAL_TOKEN", "", aliases=("XYENCE_INTERNAL_TOKEN",))
    if env == "prod" and not internal_token:
        raise RuntimeError("XYN_INTERNAL_TOKEN is required in prod")
    if env != "prod" and not internal_token:
        internal_token = "xyn-dev-internal-token"
        os.environ.setdefault("XYN_INTERNAL_TOKEN", internal_token)
        logger.warning("XYN_INTERNAL_TOKEN not set; using dev bootstrap token")

    oidc_issuer = _env("XYN_OIDC_ISSUER", "", aliases=("OIDC_ISSUER",))
    oidc_client_id = _env("XYN_OIDC_CLIENT_ID", "", aliases=("OIDC_CLIENT_ID",))
    oidc_redirect_uri = _env("XYN_OIDC_REDIRECT_URI", "", aliases=("OIDC_REDIRECT_URI",))
    if auth_mode == "oidc":
        missing = [name for name, value in [("XYN_OIDC_ISSUER", oidc_issuer), ("XYN_OIDC_CLIENT_ID", oidc_client_id)] if not value]
        if missing:
            raise RuntimeError(f"OIDC mode requires: {', '.join(missing)}")

    ai_provider, ai_enabled, ai_keys = _resolve_ai_provider_and_keys()
    ai_model = _default_ai_model(ai_provider) if ai_enabled else "none"

    database_url = _env("DATABASE_URL", "postgresql://xyn:xyn_dev_password@postgres:5432/xyn")
    redis_url = _env("REDIS_URL", "redis://redis:6379/0")
    secret_key = _env("XYN_SECRET_KEY", "")
    credentials_encryption_key = _env("XYN_CREDENTIALS_ENCRYPTION_KEY", "")

    return SeedConfig(
        env=env,
        base_domain=base_domain,
        auth_mode=auth_mode,
        internal_token=internal_token,
        oidc_issuer=oidc_issuer,
        oidc_client_id=oidc_client_id,
        oidc_redirect_uri=oidc_redirect_uri,
        ai_provider=ai_provider,
        ai_model=ai_model,
        ai_enabled=ai_enabled,
        openai_api_key=ai_keys["openai"],
        gemini_api_key=ai_keys["gemini"],
        anthropic_api_key=ai_keys["anthropic"],
        secret_key=secret_key,
        credentials_encryption_key=credentials_encryption_key,
        database_url=database_url,
        redis_url=redis_url,
    )


def export_runtime_env(config: SeedConfig) -> dict[str, str]:
    """Canonical runtime env map for seed + downstream runtime artifacts."""
    exported = {
        "XYN_ENV": config.env,
        "XYN_BASE_DOMAIN": config.base_domain,
        "XYN_AUTH_MODE": config.auth_mode,
        "XYN_INTERNAL_TOKEN": config.internal_token,
        "XYN_OIDC_ISSUER": config.oidc_issuer,
        "XYN_OIDC_CLIENT_ID": config.oidc_client_id,
        "XYN_OIDC_REDIRECT_URI": config.oidc_redirect_uri,
        "XYN_AI_PROVIDER": config.ai_provider,
        "XYN_AI_MODEL": config.ai_model,
        "XYN_AI_ENABLED": "true" if config.ai_enabled else "false",
        "XYN_DEFAULT_MODEL_PROVIDER": config.ai_provider,
        "XYN_DEFAULT_MODEL_NAME": config.ai_model,
        "XYN_OPENAI_API_KEY": config.openai_api_key,
        "XYN_GEMINI_API_KEY": config.gemini_api_key,
        "XYN_GOOGLE_API_KEY": config.gemini_api_key,
        "XYN_ANTHROPIC_API_KEY": config.anthropic_api_key,
        "OPENAI_API_KEY": config.openai_api_key,
        "GEMINI_API_KEY": config.gemini_api_key,
        "GOOGLE_API_KEY": config.gemini_api_key,
        "ANTHROPIC_API_KEY": config.anthropic_api_key,
        "XYN_SECRET_KEY": config.secret_key,
        "XYN_CREDENTIALS_ENCRYPTION_KEY": config.credentials_encryption_key,
        "DATABASE_URL": config.database_url,
        "REDIS_URL": config.redis_url,
    }
    if config.base_domain:
        exported["DOMAIN"] = config.base_domain
    return exported
