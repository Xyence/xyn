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

- `XYN_AI_PROVIDER` (optional: `openai|gemini|anthropic`)
- `XYN_AI_MODEL` (optional model override)
- Provider keys:
  - `XYN_OPENAI_API_KEY`
  - `XYN_GEMINI_API_KEY`
  - `XYN_ANTHROPIC_API_KEY`
- Optional purpose-specific bootstrap overlays:
  - `XYN_AI_PLANNING_PROVIDER` / `XYN_AI_PLANNING_MODEL` / `XYN_AI_PLANNING_API_KEY`
  - `XYN_AI_CODING_PROVIDER` / `XYN_AI_CODING_MODEL` / `XYN_AI_CODING_API_KEY`
- Optional deterministic routing overrides (agent slugs):
  - `XYN_AI_ROUTING_DEFAULT_AGENT_SLUG`
  - `XYN_AI_ROUTING_PLANNING_AGENT_SLUG`
  - `XYN_AI_ROUTING_CODING_AGENT_SLUG`
  - `XYN_AI_ROUTING_PALETTE_AGENT_SLUG`
- Secret encryption key (for encrypted credential storage fallback):
  - `XYN_SECRET_KEY` (or `XYN_CREDENTIALS_ENCRYPTION_KEY`)

Provider resolution:
- If `XYN_AI_PROVIDER` is set, the matching key is required.
- If provider is unset and exactly one key is present, provider is inferred.
- If provider is unset and multiple keys are present, startup fails fast and requires explicit provider.
- If no keys are present, AI bootstrap is disabled and runtime remains bootable.
- Planning/coding bootstrap overlays are optional. If any overlay field is set for a role, that role requires the full provider/model/api-key triplet.

### Database / Cache

- `DATABASE_URL`
- `REDIS_URL`

### Managed Storage Roots

- `XYN_ARTIFACT_ROOT`
  - canonical durable artifact root for local/runtime storage
  - defaults to `.xyn/artifacts` in seed-owned local/dev setups
- `XYN_WORKSPACE_ROOT`
  - canonical managed workspace root for active coding/scratch workspaces
  - defaults to `.xyn/workspace`
- `XYN_WORKSPACE_RETENTION_DAYS`
  - local retention hint for stale managed workspace cleanup eligibility
  - default: `14`

### Installable Solution Bundle Bootstrap

Use these to auto-install durable solution bundles into the platform at startup:

- `XYN_BOOTSTRAP_INSTALL_SOLUTIONS`
  - comma-separated solution slugs to install (example: `deal-finder`)
- `XYN_BOOTSTRAP_SOLUTION_SOURCE`
  - `local` or `s3`
- `XYN_BOOTSTRAP_SOLUTION_PREFIX`
  - local directory (`local`) or key prefix (`s3`)
- `XYN_BOOTSTRAP_SOLUTION_VERSION`
  - optional version segment for startup lookup (`<solution>/<version>/manifest.json`)
- `XYN_BOOTSTRAP_SOLUTION_BUCKET`
  - required when source is `s3`
- `XYN_BOOTSTRAP_IF_MISSING_ONLY`
  - `true` (default): install only if solution missing
  - `false`: always reinstall/update on startup
- `XYN_BOOTSTRAP_SOLUTION_WORKSPACE_SLUG`
  - optional workspace target (defaults to `XYN_WORKSPACE_SLUG` or `development`)
- AWS credentials for `s3` source:
  - `AWS_ACCESS_KEY_ID`
  - `AWS_SECRET_ACCESS_KEY`
  - `AWS_SESSION_TOKEN` (optional)
  - `AWS_DEFAULT_REGION` / `AWS_REGION`
  - `xynctl` will best-effort hydrate these from `aws configure get ...` when `XYN_BOOTSTRAP_SOLUTION_SOURCE=s3`

Compatibility aliases still exported for current local flows:
- `ARTIFACT_STORE_PATH` mirrors `XYN_ARTIFACT_ROOT`
- `XYN_LOCAL_WORKSPACE_ROOT` and `XYNSEED_WORKSPACE` mirror `XYN_WORKSPACE_ROOT`

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
