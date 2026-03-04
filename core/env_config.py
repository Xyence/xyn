"""Canonical environment loader for xyn-seed bootstrap."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_LOADED = False


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
    database_url: str
    redis_url: str


def _default_ai_provider() -> str:
    explicit = _env("XYN_AI_PROVIDER", "").lower()
    if explicit:
        return explicit
    if _env("OPENAI_API_KEY", ""):
        return "openai"
    if _env("ANTHROPIC_API_KEY", ""):
        return "anthropic"
    if _env("GEMINI_API_KEY", ""):
        return "gemini"
    return "none"


def _default_ai_model(provider: str) -> str:
    explicit = _env("XYN_AI_MODEL", "")
    if explicit:
        return explicit
    if provider == "openai":
        return "gpt-5-mini"
    if provider == "anthropic":
        return "claude-3-7-sonnet-latest"
    if provider == "gemini":
        return "gemini-2.0-flash"
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

    ai_provider = _default_ai_provider()
    ai_model = _default_ai_model(ai_provider)

    database_url = _env("DATABASE_URL", "postgresql://xyn:xyn_dev_password@postgres:5432/xyn")
    redis_url = _env("REDIS_URL", "redis://redis:6379/0")

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
        "DATABASE_URL": config.database_url,
        "REDIS_URL": config.redis_url,
    }
    if config.base_domain:
        exported["DOMAIN"] = config.base_domain
    return exported
