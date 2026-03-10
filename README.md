# Xyn Seed - Phase v0.0

**Local-first, Traefik-ingressed, minimal agent-native platform core**

Xyn Seed is a minimal platform that turns **events** into **plans** into **actions** with full **auditability**.

This is the **v0.0 implementation** - a local-first proof of concept demonstrating the core execution model.

## What's Included in v0.0

✅ Docker Compose setup (Postgres, Redis, Core Service)
✅ FastAPI + Jinja2 + HTMX web interface
✅ Event ingestion and audit trail
✅ Run/Step execution model
✅ LocalFS artifact storage
✅ Basic UI (Event Console, Run Launcher, Artifact Browser)
✅ REST API (versioned `/api/v1`)
✅ `xynctl` bootstrap tool with preflight checks
✅ Kernel boot mode that dynamically loads workspace-bound artifacts
✅ Instance-local runtime storage under `.xyn/`
✅ Context-pack bridge from `xyn-platform` authority into `xyn-core` runtime

## What's NOT Included in v0.0

✅ Auth bootstrap split (`dev`, `token`, `oidc`)
⚠ DNS/ACME/TLS depends on host DNS + ACME env configuration
❌ AWS integrations
❌ Vault/secrets management
❌ Federation/workcells
❌ CI/CD automation
❌ Full blueprint compilation

## Host Ingress Note (Xyn Runtime)
- In Xyn-managed deployments with `tls.mode=host-ingress`, application stacks should not bind host `80/443`.
- A host ingress layer (Traefik) owns `80/443` and routes by FQDN.

These features are planned for v1 and beyond.

## Quickstart (Fresh Host)

### Prerequisites

- Docker and Docker Compose
- At least one AI provider API key (`XYN_OPENAI_API_KEY` or `XYN_GEMINI_API_KEY` or `XYN_ANTHROPIC_API_KEY`)

### 1. Clone and Configure

```bash
git clone <repo>
cd xyn-seed
cp .env.example .env
```

Add AI key in `.env`:

```bash
XYN_OPENAI_API_KEY=your_key_here
```

### 2. Make `xynctl` executable

```bash
chmod +x xynctl
```

### 3. Run quickstart

```bash
./xynctl quickstart
```

This will:
1. start the seed core stack
2. start Traefik ingress (`:80`, `:443`)
3. pull platform artifacts from

   `public.ecr.aws/i0h0h0n4/xyn/artifacts`

4. provision a sibling Xyn instance behind Traefik
5. print final URLs

Local default:
- Seed API: `http://seed.localhost`
- Sibling UI: `http://localhost`
- Sibling API: `http://localhost/xyn/api`
- Auth mode: `dev` (no external IdP required)

## Auth Modes (Quickstart)

- `XYN_AUTH_MODE=dev` (default):
  - local out-of-box flow
  - `/auth/login` offers "Continue as Admin"
  - no redirects to production domains
- `XYN_AUTH_MODE=token`:
  - xynctl prints bootstrap token on quickstart
  - use token login flow in UI
- `XYN_AUTH_MODE=oidc`:
  - requires `XYN_OIDC_ISSUER`, `XYN_OIDC_CLIENT_ID`
  - set `XYN_PUBLIC_BASE_URL` to the exact external origin (for local testing: `http://localhost`)

## Artifact Registry (Managed Artifact)

Xyn Seed now treats the artifact registry itself as a managed artifact (`kind=artifact-registry`).

- On first startup, seed creates `default-registry` automatically.
- Default seeded endpoint:
  - `public.ecr.aws/i0h0h0n4/xyn/artifacts`
- Provisioning resolves registry in this order:
  1. explicit `registry_slug`
  2. workspace default registry
  3. seeded `default-registry`
  4. `XYN_ARTIFACT_REGISTRY` env fallback (only if no registry artifact exists)

API endpoints:

- `GET /api/v1/artifact-registries`
- `POST /api/v1/artifact-registries`
- `GET /api/v1/artifact-registries/resolve`
- `GET|PATCH|DELETE /api/v1/artifact-registries/{slug}`
- `GET|PATCH /api/v1/workspaces/{workspace_slug}/artifact-registry`

Local dev override remains supported:

- If both `XYN_LOCAL_API_CONTEXT` and `XYN_LOCAL_UI_CONTEXT` are set and contain Dockerfiles, seed builds and uses local images (`xyn-api`, `xyn-ui`).

## TLS On A Host (Traefik + ACME)

To enable automatic certs on a publicly reachable host:

1. Point DNS to the host:
- `${project}.${your_domain}` for UI (or set `XYN_LOCAL_UI_HOST`)
- `api.${project}.${your_domain}` for API (or set `XYN_LOCAL_API_HOST`)
- `seed.${your_domain}` (or set `XYN_SEED_HOST`)

2. Configure `.env`:

```bash
XYN_BASE_DOMAIN=your_domain
XYN_TRAEFIK_ENABLE_TLS=true
XYN_TRAEFIK_ACME_EMAIL=you@example.com
XYN_TRAEFIK_CERT_RESOLVER=letsencrypt
# Optional:
# XYN_TRAEFIK_ACME_CHALLENGE=dns
# XYN_TRAEFIK_DNS_PROVIDER=route53
```

3. Run:

```bash
./xynctl quickstart
```

If ACME vars are not set, Traefik runs HTTP-only and quickstart still works.

### Kernel Artifact Loading (Phase 1)

Seed kernel now defaults to legacy routes **OFF** and loads artifact roles dynamically.

Set these variables when running kernel mode:

```bash
XYN_SEED_ENABLE_LEGACY_PRODUCT=false
XYN_API_BASE_URL=http://localhost:8000
XYN_KERNEL_WORKSPACE_ID=<workspace-uuid>
XYENCE_INTERNAL_TOKEN=<same token used by xyn-api internal endpoints>
XYN_KERNEL_MANIFEST_ROOTS=/home/ubuntu/src
```

### Create Your First Run

1. Navigate to `http://seed.localhost/ui/runs/new`
2. Enter a run name (e.g., "My First Run")
3. Click "Create & Execute Run"
4. View the run detail page to see steps and events

## Architecture

```
┌─────────────────────────────────────────────┐
│              Xyn Seed Core                   │
│  ┌────────────┐  ┌──────────┐  ┌─────────┐ │
│  │   UI       │  │   API    │  │ Executor│ │
│  │ (Jinja2    │  │ (FastAPI)│  │ (Steps) │ │
│  │  + HTMX)   │  │          │  │         │ │
│  └────────────┘  └──────────┘  └─────────┘ │
│         │              │             │       │
│         └──────────────┴─────────────┘       │
│                    │                         │
│  ┌─────────────────▼──────────────────────┐ │
│  │         Database (Postgres)            │ │
│  │  Events | Runs | Steps | Artifacts     │ │
│  └────────────────────────────────────────┘ │
│                                              │
│  ┌────────────────────────────────────────┐ │
│  │     Artifact Store (LocalFS)           │ │
│  └────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

## Key Concepts

- **Event**: Immutable input record (e.g., `xyn.run.created`)
- **Run**: Execution instance of a workflow
- **Step**: Atomic unit within a run
- **Artifact**: Generated output (files, logs, reports)

## API Endpoints

### Health

- `GET /api/v1/health` - Health check

### Events

- `GET /api/v1/events` - List events
- `GET /api/v1/events/{id}` - Get event
- `POST /api/v1/events` - Emit event

### Runs

- `POST /api/v1/runs` - Create run
- `GET /api/v1/runs` - List runs
- `GET /api/v1/runs/{id}` - Get run
- `GET /api/v1/runs/{id}/steps` - List steps
- `POST /api/v1/runs/{id}/cancel` - Cancel run

### Artifacts

- `POST /api/v1/artifacts` - Upload artifact
- `GET /api/v1/artifacts` - List artifacts
- `GET /api/v1/artifacts/{id}` - Get artifact metadata
- `GET /api/v1/artifacts/{id}/download` - Download artifact
- `GET /api/v1/context-packs` - List governed context-pack artifacts
- `GET|PATCH /api/v1/context-packs/bindings` - Inspect/update explicit workspace context-pack bindings

### Drafts (workspace scoped)

- `POST /api/v1/drafts` - Create draft
- `GET /api/v1/drafts` - List drafts
- `GET /api/v1/drafts/{id}` - Get draft
- `PATCH /api/v1/drafts/{id}` - Update draft
- `POST /api/v1/drafts/{id}/submit` - Mark submitted + enqueue job

### Jobs (workspace scoped)

- `GET /api/v1/jobs` - List jobs
- `GET /api/v1/jobs/{id}` - Get job
- `PATCH /api/v1/jobs/{id}` - Update job status/output/logs
- `GET /api/v1/jobs/{id}/logs` - Fetch job logs text

Draft submit now runs chained jobs for app-intent drafts:
- `generate_app_spec`
- `deploy_app_local`
- `provision_sibling_xyn`
- `smoke_test`

### Palette

- `GET /api/v1/palette/commands` - List workspace + global palette command bindings
- `POST /api/v1/palette/commands` - Register workspace palette command
- `PATCH /api/v1/palette/commands/{id}` - Update workspace palette command
- `DELETE /api/v1/palette/commands/{id}` - Remove workspace palette command
- `POST /api/v1/palette/execute` - Execute palette prompt via registered command handlers

### Artifact refresh (local)

- `POST /api/v1/artifacts/refresh` - Pull latest artifact images and return pull logs
  - body example: `{"artifacts":["xyn-ui","xyn-api","net-inventory-api"],"channel":"dev"}`

### Workspace APIs (Phase 2 validation)

- `GET /api/v1/workspaces`
- `POST /api/v1/workspaces`

## Validation Harness

```bash
scripts/run_e2e_validation.sh
```

This runs contract checks (`contracts/*.json`), workspace isolation validation, persistence restart checks, palette execution checks, and artifact refresh smoke tests.

Workspace context is required for draft/job endpoints. Provide one of:
- Query param: `workspace_id=<uuid>` or `workspace_slug=default`
- Header: `X-Workspace-Id: <uuid>` or `X-Workspace-Slug: default`

## xynctl Commands

```bash
./xynctl                          # Preflight + start seed core
./xynctl start --provision        # Start seed + auto-provision sibling instance
./xynctl quickstart               # Alias for start --provision
./xynctl provision local          # Provision/reuse sibling instance and print URLs
./xynctl preflight                # Run preflight checks only
./xynctl status                   # Show seed + provisioned local instance URLs
./xynctl stop                     # Stop seed stack
./xynctl logs [svc]               # View logs (optionally for specific service)
./xynctl help                     # Show help
```

## Verify

Local verify:

```bash
curl -sS http://seed.localhost/health
curl -sS http://localhost/xyn/api/me
open http://localhost
```

Host verify (TLS enabled):

```bash
curl -I https://$XYN_SEED_HOST/health
curl -I https://$XYN_LOCAL_UI_HOST/
curl -I https://$XYN_LOCAL_API_HOST/xyn/api/me
```

## Testing

See [SMOKE.md](SMOKE.md) for a comprehensive smoke test guide with curl commands and UI URLs.

## Development

### Project Structure

```
xyn-seed/
├── compose.yml              # Docker Compose configuration
├── Dockerfile               # Core service container
├── xynctl                   # Bootstrap/control tool
├── requirements.txt         # Python dependencies
├── .env.template            # Configuration template
├── core/                    # FastAPI application
│   ├── main.py             # Application entry point
│   ├── models.py           # SQLAlchemy models
│   ├── schemas.py          # Pydantic DTOs
│   ├── database.py         # Database setup
│   ├── executor.py         # Run/step executor
│   ├── artifact_store.py   # LocalFS storage
│   ├── api/                # API routes
│   │   ├── health.py
│   │   ├── events.py
│   │   ├── runs.py
│   │   └── artifacts.py
│   ├── ui/                 # UI routes
│   │   ├── ui_events.py
│   │   ├── ui_runs.py
│   │   └── ui_artifacts.py
│   └── templates/          # Jinja2 templates
│       ├── base.html
│       ├── events.html
│       ├── runs.html
│       └── ...
├── .xyn/artifacts/         # Instance-local artifact storage
├── .xyn/workspace/         # Instance-local generated specs / bundles
├── .xyn/deployments/       # Instance-local local deployment manifests
├── .xyn/sync/              # Synced runtime manifests (for example context packs)
├── SMOKE.md                # Smoke test guide
└── README.md               # This file
```

### Context Packs

- `xyn-platform` remains authoritative for context-pack governance.
- `xynctl` synchronizes runtime context-pack definitions into `.xyn/sync/context-packs.manifest.json`.
- `xyn-core` consumes that manifest and exposes synchronized context-pack artifacts via `/api/v1/context-packs`.
- See [docs/context-pack-bridge.md](/home/jrestivo/src/xyn/docs/context-pack-bridge.md).

### Viewing Logs

```bash
# All services
./xynctl logs

# Specific service
./xynctl logs core
./xynctl logs postgres
./xynctl logs redis
```

### Database Access

```bash
# Connect to Postgres
docker compose exec postgres psql -U xyn -d xyn

# List tables
\dt

# View events
SELECT event_name, occurred_at FROM events ORDER BY occurred_at DESC LIMIT 10;
```

## Next Steps (v1+)

The following features are planned for future releases:

- **v1**: Secrets management, backup/restore, CI/CD pipeline, full blueprint compilation
- **v2**: Identity/auth, POUs, governance policies, federation

See `xyn_seed_implementation_plan.md` for the complete roadmap.

## License

This repository is part of the core Xyn platform and is licensed under the GNU Affero General Public License v3.0.

In plain language, if you modify Xyn and let users interact with that modified version over a network, you must also make the corresponding source code for those modifications available under AGPLv3.

Commercial use, including paid hosting, support, and consulting, is allowed so long as AGPL obligations are honored. Separate commercial licensing may also be available.

See [LICENSE](/home/jrestivo/src/xyn/LICENSE) and [NOTICE](/home/jrestivo/src/xyn/NOTICE).

## Trademark and branding

The software license does not grant rights to use project names, logos, or branding except as required for reasonable nominative use.

Any formal trademark policy will be published separately.

## Support

For issues or questions, please refer to the implementation plan or contact the development team.
