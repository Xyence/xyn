"""Seed-owned AI bootstrap handshake into xyn-api."""

from __future__ import annotations

import logging
import os
import json
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def ensure_default_agent_via_api() -> None:
    """Request xyn-api to upsert bootstrap AI agent state using seed-resolved env."""
    base_url = str(os.getenv("XYN_API_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        logger.warning("Skipping AI bootstrap: XYN_API_BASE_URL is not configured")
        return
    token = str(os.getenv("XYN_INTERNAL_TOKEN") or "").strip()
    if not token:
        logger.warning("Skipping AI bootstrap: XYN_INTERNAL_TOKEN missing")
        return
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
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:300]
        logger.warning("AI bootstrap request failed status=%s body=%s", exc.code, body)
    except URLError:
        logger.exception("AI bootstrap handshake failed")
    except Exception:
        logger.exception("AI bootstrap handshake failed")
