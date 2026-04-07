"""Seed-owned AI bootstrap handshake into xyn-api."""

from __future__ import annotations

import logging
import os
import json
from urllib.parse import urlparse
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def _is_seed_loopback_base_url(base_url: str) -> bool:
    parsed = urlparse(str(base_url or "").strip())
    host = str(parsed.hostname or "").strip().lower()
    if host not in {"localhost", "127.0.0.1"}:
        return False
    if parsed.port in {None, 8000}:
        return True
    return False


def ensure_default_agent_via_api() -> str:
    """Request xyn-api to upsert bootstrap AI agent state using seed-resolved env.

    Returns one of:
    - "succeeded": bootstrap completed
    - "retryable_failure": transient error, caller may retry
    - "unsupported": endpoint/config is not available in this runtime
    """
    base_url = str(os.getenv("XYN_API_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        logger.warning("Skipping AI bootstrap: XYN_API_BASE_URL is not configured")
        return "unsupported"
    if _is_seed_loopback_base_url(base_url):
        logger.info(
            "Skipping AI bootstrap: XYN_API_BASE_URL points at seed loopback (%s); "
            "provisioned runtime bootstrap is authoritative.",
            base_url,
        )
        return "unsupported"
    token = str(os.getenv("XYN_INTERNAL_TOKEN") or "").strip()
    if not token:
        logger.warning("Skipping AI bootstrap: XYN_INTERNAL_TOKEN missing")
        return "unsupported"
    url = f"{base_url}/xyn/internal/ai/bootstrap-default-agent"
    try:
        req = Request(
            url=url,
            method="POST",
            headers={"X-Internal-Token": token, "Content-Type": "application/json"},
            data=b"{}",
        )
        with urlopen(req, timeout=15) as response:
            body = response.read().decode("utf-8") if response else ""
        payload = json.loads(body or "{}")
        logger.info(
            "AI bootstrap ensured agents default=%s planning=%s coding=%s provider=%s model=%s key_present=%s",
            payload.get("default_agent_slug"),
            payload.get("planning_agent_slug"),
            payload.get("coding_agent_slug"),
            payload.get("provider"),
            payload.get("model"),
            payload.get("key_present"),
        )
        return "succeeded"
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:300]
        if exc.code in {404, 405}:
            logger.info(
                "Skipping AI bootstrap: endpoint unavailable at %s (status=%s).",
                url,
                exc.code,
            )
            return "unsupported"
        logger.warning("AI bootstrap request failed status=%s body=%s", exc.code, body)
        return "retryable_failure"
    except URLError:
        logger.warning("AI bootstrap handshake failed (transient network error)")
        return "retryable_failure"
    except Exception:
        logger.warning("AI bootstrap handshake failed", exc_info=True)
        return "retryable_failure"
