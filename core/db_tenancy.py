from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse

import psycopg2
from psycopg2 import errors, sql


def _safe_identifier(value: str, *, max_length: int = 63) -> str:
    token = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(value or "").strip().lower())
    token = token.strip("_")
    if not token:
        token = "xyn"
    return token[:max_length]


def _uuid_token(value: uuid.UUID | str | None, *, default: str) -> str:
    if isinstance(value, uuid.UUID):
        return value.hex[:12]
    if isinstance(value, str):
        try:
            return uuid.UUID(value).hex[:12]
        except Exception:
            cleaned = _safe_identifier(value, max_length=12)
            return cleaned or default
    return default


def _runtime_database_url(
    *,
    admin_url: str,
    user: str,
    password: str,
    database_name: str,
) -> str:
    parsed = urlparse(admin_url)
    scheme = parsed.scheme or "postgresql"
    host = parsed.hostname or "localhost"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or 5432
    query_dict = parse_qs(parsed.query, keep_blank_values=True)
    query = urlencode(query_dict, doseq=True)
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}"
    netloc = f"{auth}@{host}:{port}"
    path = f"/{quote(database_name, safe='')}"
    if query:
        return f"{scheme}://{netloc}{path}?{query}"
    return f"{scheme}://{netloc}{path}"


def _redact_database_url(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    parsed = urlparse(token)
    host = parsed.hostname or ""
    port = parsed.port
    db_name = parsed.path.lstrip("/") if parsed.path else ""
    host_port = f"{host}:{port}" if host and port else host
    if db_name:
        return f"{parsed.scheme or 'postgresql'}://***@{host_port}/{db_name}"
    return f"{parsed.scheme or 'postgresql'}://***@{host_port}"


@dataclass(frozen=True)
class DatabaseAllocation:
    mode: str
    tenancy_mode: str
    database_name: str = ""
    database_user: str = ""
    database_url: str = ""
    host: str = ""
    port: int = 5432
    metadata: dict[str, Any] | None = None

    def runtime_env(self) -> dict[str, str]:
        if not self.database_url:
            return {}
        return {"DATABASE_URL": self.database_url}

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "tenancy_mode": self.tenancy_mode,
            "database_name": self.database_name,
            "database_user": self.database_user,
            "host": self.host,
            "port": self.port,
            "metadata": dict(self.metadata or {}),
        }


def allocate_database(
    *,
    environment_id: uuid.UUID | str,
    sibling_id: uuid.UUID | str | None,
    workspace_id: uuid.UUID | str | None = None,
    sibling_name: str | None = None,
) -> DatabaseAllocation:
    db_mode = str(os.getenv("XYN_DB_MODE", "local")).strip().lower() or "local"
    if db_mode != "external":
        return DatabaseAllocation(
            mode="local",
            tenancy_mode="local_compose",
            metadata={"allocator": "noop_local"},
        )

    tenancy_mode = str(os.getenv("XYN_DB_TENANCY_MODE", "shared_rds_db_per_sibling")).strip().lower()
    if tenancy_mode != "shared_rds_db_per_sibling":
        raise RuntimeError(f"Unsupported XYN_DB_TENANCY_MODE '{tenancy_mode}' for external DB mode")

    admin_url = (
        str(os.getenv("XYN_DB_BOOTSTRAP_DATABASE_URL", "")).strip()
        or str(os.getenv("XYN_DB_ADMIN_DATABASE_URL", "")).strip()
        or str(os.getenv("DATABASE_URL", "")).strip()
    )
    if not admin_url:
        raise RuntimeError(
            "XYN_DB_BOOTSTRAP_DATABASE_URL (or XYN_DB_ADMIN_DATABASE_URL or DATABASE_URL) "
            "is required for XYN_DB_MODE=external"
        )

    env_token = _uuid_token(environment_id, default="env")
    sib_token = _uuid_token(sibling_id, default=_safe_identifier(sibling_name or "sibling", max_length=12))
    workspace_token = _uuid_token(workspace_id, default="ws")
    database_name = _safe_identifier(f"xyn_e_{env_token}_s_{sib_token}", max_length=63)
    database_user = _safe_identifier(f"xyn_u_{workspace_token}_{sib_token}", max_length=63)
    database_password = secrets.token_urlsafe(24)

    try:
        conn = psycopg2.connect(admin_url)
    except Exception as exc:
        raise RuntimeError(
            "Failed to connect bootstrap database for tenant allocation "
            f"(target={_redact_database_url(admin_url)})."
        ) from exc
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s").format(sql.Identifier(database_user)),
                    [database_password],
                )
            except errors.DuplicateObject:
                pass
            cur.execute(
                sql.SQL("ALTER ROLE {} LOGIN PASSWORD %s").format(sql.Identifier(database_user)),
                [database_password],
            )
            try:
                cur.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(database_name),
                        sql.Identifier(database_user),
                    )
                )
            except errors.DuplicateDatabase:
                pass
            cur.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database_name)))
            cur.execute(
                sql.SQL("GRANT CONNECT, TEMP ON DATABASE {} TO {}").format(
                    sql.Identifier(database_name),
                    sql.Identifier(database_user),
                )
            )
    except Exception as exc:
        raise RuntimeError(
            "Failed to allocate external tenant database "
            f"(target={_redact_database_url(admin_url)}, db={database_name}, user={database_user})."
        ) from exc
    finally:
        conn.close()

    parsed_admin = urlparse(admin_url)
    runtime_url = _runtime_database_url(
        admin_url=admin_url,
        user=database_user,
        password=database_password,
        database_name=database_name,
    )
    return DatabaseAllocation(
        mode="external",
        tenancy_mode=tenancy_mode,
        database_name=database_name,
        database_user=database_user,
        database_url=runtime_url,
        host=parsed_admin.hostname or "",
        port=parsed_admin.port or 5432,
        metadata={"allocator": "shared_rds_db_per_sibling"},
    )
