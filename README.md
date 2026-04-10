# Xyn

Xyn is an AI-driven application factory focused on repeatable, scalable, and governed software delivery.

The platform treats **everything as an artifact**: applications, policies, runtime surfaces, context packs, and operational metadata.  
This repository (`xyn`) provides the bootstrap/control plane path for local and operational environments. By default, bootstrap loads a curated artifact set from the **xyn-platform** source: <https://github.com/xyn-platform>.

## Why Xyn

- **Repeatability**: deterministic bootstrap and artifact-driven provisioning.
- **Scalability**: environment and runtime orchestration for multiple workspaces/instances.
- **Security**: explicit auth modes, credentialed integrations, and least-privilege capability controls.
- **Governance**: auditable plan/apply/run flows, policy bundles, and provenance-aware operations.

## Core Concepts

- **Artifact**: the primary unit of composition and lifecycle management.
- **Workspace**: tenant/context boundary for user-facing operations.
- **Application / Solution**: grouped, multi-artifact development unit.
- **Plan -> Apply -> Run**: the core execution lifecycle.
- **Preview/Sibling runtime**: isolated validation surface before promotion.

## Repository Role

This repository contains:

- `xynctl` and local provisioning workflows
- compose/deployment bootstrap assets
- seed/bootstrap orchestration and runtime coordination scripts
- operational docs and helper scripts

The main product implementation artifacts are loaded from platform artifact sources, with `xyn-platform` as the default authority.

## Installation (Basic Local Setup)

### Prerequisites

- Docker + Docker Compose
- Bash shell
- At least one AI provider API key (for example OpenAI/Anthropic/Gemini)
- A security secret key for local auth/session signing

### 1. Clone and configure

```bash
git clone https://github.com/Xyence/xyn.git
cd xyn
cp .env.example .env
```

### 2. Set required secrets

Add at least one AI key in `.env` (example):

```bash
XYN_OPENAI_API_KEY=your_api_key_here
```

Generate a secure random key and set it in `.env` (example command):

```bash
echo "XYN_SECURITY_KEY=$(openssl rand -hex 32)"
```

If your env uses a different key variable name, set the generated value to the required security/secret setting used by your deployment profile.

### 3. Start local environment

```bash
chmod +x xynctl
./xynctl quickstart
```

### 4. Access

- Workbench/UI: `http://localhost`
- Seed/API endpoints: `http://seed.localhost`

## Common Operations

- Quick bootstrap:
  - `./xynctl quickstart`
- Provision/re-provision local instance:
  - `./xynctl provision local`
- Stop local stack:
  - `docker compose down`

## Source-Backed Runtime (Optional, Staged)

- Default (safe/non-breaking): run `compose.yml` only; source review falls back to packaged files when repo roots are unavailable.
- Enable source-backed mode for `xyn-platform`:
  - `export XYN_PLATFORM_HOST_SRC_PATH=/abs/path/to/xyn-platform`
  - `docker compose -f compose.yml -f compose.source-backed.yml up -d`
- Verify runtime repo-map visibility:
  - `python scripts/check_runtime_repo_map.py`

## MCP Adapter (Thin API Wrapper)

Xyn includes a thin MCP server adapter that exposes existing Xyn control/evidence/release-target APIs as MCP tools.
The adapter does not add workflow semantics; it forwards to existing Xyn endpoints.

Install MCP adapter dependencies in a dedicated environment:

```bash
pip install -r requirements-mcp.txt
```

Set adapter configuration (example):

```bash
export XYN_MCP_XYN_CONTROL_API_BASE_URL=http://localhost:8001
# Optional dedicated code-plane upstream for source-tree/search/analyze (and future mutation tools):
# export XYN_MCP_XYN_CODE_API_BASE_URL=http://localhost:8000
export XYN_MCP_AUTH_BEARER_TOKEN=...
# Optional:
# export XYN_MCP_INTERNAL_TOKEN=...
```

Run the MCP server:

```bash
python -m core.mcp.xyn_mcp_server
```

Endpoints:
- MCP: `http://localhost:8011/mcp`
- Health: `http://localhost:8011/healthz`

### Managed MCP Service via Compose

Xyn can run MCP as an optional managed service (profile: `mcp`) behind Traefik.

Enable with environment:

```bash
export XYN_ENABLE_MCP=true
export XYN_MCP_HOST=mcp.localhost
export XYN_MCP_AUTH_MODE=none
```

Then run:

```bash
./xynctl quickstart
```

Production auth example (OIDC-validated bearer):

```bash
XYN_ENABLE_MCP=true
XYN_MCP_HOST=mcp.xyn.xyence.io
XYN_MCP_AUTH_MODE=oidc
OIDC_ISSUER=https://accounts.google.com
OIDC_CLIENT_ID=<client-id>
```

Emergency/bootstrap token mode:

```bash
XYN_MCP_AUTH_MODE=token
XYN_MCP_AUTH_BEARER_TOKEN=<strong-random-token>
```

## Configuration Notes

- Workspace defaults and bootstrap behavior are environment-driven.
- System/platform workspaces remain internal by design; user-facing work happens in user workspaces.
- Artifact import/build flows can create solution-scoped artifacts and memberships in target user workspaces.

## Security and Governance Notes

- Use non-dev auth mode outside local development.
- Store provider keys in secure secret management for non-local environments.
- Treat policy bundles and operational runs as governed artifacts.
- Preserve audit trails and provenance data in plan/apply pipelines.

## Contributing

1. Create a branch from `develop`.
2. Make focused changes with tests.
3. Run local checks.
4. Open a PR to `develop`.

## Support

- Repo issues: <https://github.com/Xyence/xyn/issues>
- Platform artifact source: <https://github.com/xyn-platform>

## License

See [LICENSE](LICENSE).
