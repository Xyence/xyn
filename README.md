# Xyn Seed - Phase v0.0

**Local-first, HTTP-only, minimal agent-native platform core**

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

## What's NOT Included in v0.0

❌ Authentication/authorization
❌ DNS/ACME/TLS (HTTP-only on localhost)
❌ AWS integrations
❌ Vault/secrets management
❌ Federation/workcells
❌ CI/CD automation
❌ Full blueprint compilation

## Host Ingress Note (Xyn Runtime)
- In Xyn-managed deployments with `tls.mode=host-ingress`, application stacks should not bind host `80/443`.
- A host ingress layer (Traefik) owns `80/443` and routes by FQDN.

These features are planned for v1 and beyond.

## Quick Start

### Prerequisites

- Docker and Docker Compose
- At least one AI provider API key (OpenAI or Anthropic)

### 1. Clone and Configure

```bash
cd xyn-seed
cp .env.template .env
```

Edit `.env` and add at least one API key:

```bash
OPENAI_API_KEY=sk-...
# OR
ANTHROPIC_API_KEY=sk-ant-...
```

### 2. Make xynctl Executable

```bash
chmod +x xynctl
```

### 3. Start the Platform

```bash
./xynctl start
```

This will:
- Run preflight checks (Docker, API keys, etc.)
- Build and start all services
- Initialize the database
- Start the web UI on http://localhost:8000

### 4. Access the UI

Open your browser to:

**http://localhost:8000**

You'll be redirected to the Event Console.

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

### 5. Create Your First Run

1. Navigate to http://localhost:8000/ui/runs/new
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

## xynctl Commands

```bash
./xynctl              # Preflight + start (default)
./xynctl preflight    # Run preflight checks only
./xynctl start        # Start the platform
./xynctl stop         # Stop the platform
./xynctl logs [svc]   # View logs (optionally for specific service)
./xynctl help         # Show help
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
├── artifacts/              # LocalFS artifact storage
├── SMOKE.md                # Smoke test guide
└── README.md               # This file
```

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

Copyright © 2026 Xyence. All rights reserved.

## Support

For issues or questions, please refer to the implementation plan or contact the development team.
