# Xyn Runtime Configuration (Seed-Owned)

`xyn-seed` is the canonical bootstrap/config owner for runtime services.
Production deployments should inject env via seed/compose and should not depend on per-repo `.env` files in `xyn-api` or `xyn-ui`.

## Canonical Variables

- `XYN_ENV` = `local|dev|prod` (default: `local`)
- `XYN_BASE_DOMAIN` (optional; alias: `DOMAIN`)
- `XYN_AUTH_MODE` = `simple|oidc` (default: `simple`)
- `XYN_INTERNAL_TOKEN` (required in prod; dev default is generated with warning)

### OIDC (required only when `XYN_AUTH_MODE=oidc`)

- `XYN_OIDC_ISSUER`
- `XYN_OIDC_CLIENT_ID`
- `XYN_OIDC_REDIRECT_URI` (recommended)
- Optional domain controls:
  - `XYN_OIDC_ALLOWED_DOMAINS`

### AI Provider Defaults

- `XYN_AI_PROVIDER` (default: `openai`)
- `XYN_AI_MODEL` (default: `gpt-5-mini`)
- Provider keys:
  - `OPENAI_API_KEY`
  - `ANTHROPIC_API_KEY`
  - `GEMINI_API_KEY`

### Database / Cache

- `DATABASE_URL`
- `REDIS_URL`

## Compatibility Aliases (Migration Window)

- `DOMAIN` -> `XYN_BASE_DOMAIN`
- `XYENCE_INTERNAL_TOKEN` -> `XYN_INTERNAL_TOKEN`
- `XYENCE_*` operational vars continue to map to `XYN_*` in `xyn-api` runtime bootstrap.

## xyn-api Legacy .env Fallback

`xyn-api` now prefers process env injection.

Legacy `backend/.env` is loaded only in local/dev mode if present, and emits a deprecation warning.
In production compose, `env_file` is disabled (`env_file: []`) and only injected env is used.

## Startup Summary

`xyn-seed` logs a safe startup summary:

- `env=<...>`
- `auth=<simple|oidc>`
- `ai_provider=<...>`
- `ai_model=<...>`

Secrets are never logged.
