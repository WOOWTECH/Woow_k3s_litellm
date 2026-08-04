# woow_litellm_docker_compose

A production LiteLLM gateway for WoowTech that fronts multiple LLM families
through a single OpenAI-compatible API. Every model is routed via
**OpenRouter** (`openrouter/<provider>/<model>`), keys and budgets are governed
centrally, models and virtual keys persist in Postgres, and a **Claude Code
Skill Hub** is exposed for skill/plugin discovery.

The same shared `config/config.yaml` drives two deployment modes:

- **k3s** — the live production instance, reachable at
  **https://litellm.woowtech.io** via a Cloudflare tunnel.
- **docker-compose** — a self-contained local stack (LiteLLM + Postgres) for
  development and testing.

> Live instance: **https://litellm.woowtech.io**
> Readiness: `https://litellm.woowtech.io/health/readiness`

---

## Architecture

```
                          ┌───────────────────────────────┐
   Clients (OpenAI SDK,   │        OpenRouter            │
   Claude Code, curl)     │  openai/ z-ai/ minimax/ ...  │
        │                 └──────────────▲───────────────┘
        │  Bearer sk-...                  │ openrouter/<provider>/<model>
        ▼                                 │  (OPENROUTER_API_KEY from env)
┌──────────────────┐   Cloudflare   ┌────┴──────────────────┐
│ litellm.woowtech  │◄── tunnel ────►│  LiteLLM proxy     │
│ .io (public)      │                │  Service litellm   │
└──────────────────┘                │  :4000  (/ui, /v1) │
                                     └────┬───────────────┘
                                          │ DATABASE_URL
                                          ▼
                                 ┌────────────────────┐
                                 │ Postgres           │
                                 │ (models, keys,     │
                                 │  budgets, spend)   │
                                 └────────────────────┘
```

- **LiteLLM proxy** (`ghcr.io/berriai/litellm:v1.83.14-stable`) listens on port
  `4000`, serves the OpenAI-compatible `/v1/*` API and the Admin UI at `/ui`.
- **OpenRouter** is the single upstream provider. Each public model name maps to
  a verified OpenRouter slug; the API key is read from
  `os.environ/OPENROUTER_API_KEY` and is never hardcoded.
- **Postgres** persists models (`store_model_in_db: true`), virtual keys,
  budgets and spend. `/health/readiness` returns healthy only when the DB is
  connected.
- **Cloudflare tunnel** publishes the internal `litellm:4000` Service as
  `https://litellm.woowtech.io`. The tunnel routing is managed in Cloudflare and
  must stay untouched; the k8s `cloudflared` Deployment only runs the connector.

---

## Repository layout

```
.
├── docker-compose.yml          # Local stack: LiteLLM + Postgres
├── .env.example                # Placeholder env (copy to .env, fill real keys)
├── config/
│   └── config.yaml             # SHARED LiteLLM config (compose + k8s)
├── k8s/
│   ├── 00-namespace.yaml       # namespace: litellm
│   ├── 01-secrets.example.yaml # PLACEHOLDER Secrets (never commit real values)
│   ├── 02-postgres.yaml        # headless Service + StatefulSet (5Gi PVC)
│   ├── 03-litellm-config.yaml  # ConfigMap wrapping config/config.yaml
│   ├── 04-litellm-deployment.yaml # Deployment (app=litellm) + Service :4000
│   └── 05-cloudflared.yaml     # Cloudflare tunnel connector
├── tests/                      # TDD acceptance suite (see below)
└── README.md
```

> **Config sync note:** `k8s/03-litellm-config.yaml` embeds a byte-for-byte copy
> of `config/config.yaml` inside a ConfigMap. When you change one, update the
> other.

---

## Models

All models are served through OpenRouter. Public name → OpenRouter slug:

| Public model name    | OpenRouter model string                       |
| -------------------- | --------------------------------------------- |
| `gpt-4o-mini`        | `openrouter/openai/gpt-4o-mini`               |
| `glm-4.6`            | `openrouter/z-ai/glm-4.6`                      |
| `minimax-m2`         | `openrouter/minimax/minimax-m2`               |
| `claude-3.5-sonnet`  | `openrouter/anthropic/claude-3.5-sonnet`      |
| `llama-3.3-70b`      | `openrouter/meta-llama/llama-3.3-70b-instruct`|

List them live with `GET /v1/models` (Bearer master key). New models can also be
added at runtime from the Admin UI (`/ui`) and are persisted to Postgres via
`store_model_in_db: true`.

---

## Quickstart — docker-compose (local)

Requirements: Docker + Docker Compose.

```bash
# 1. Copy the placeholder env and fill in REAL values
cp .env.example .env
#    Edit .env and set at least:
#      OPENROUTER_API_KEY   (sk-or-...)
#      LITELLM_MASTER_KEY   (sk-...)
#      LITELLM_SALT_KEY     (sk-...   set once, NEVER change)
#    DATABASE_URL is preset for the bundled Postgres service.

# 2. Bring up LiteLLM + Postgres
docker compose up -d

# 3. Wait for readiness, then smoke-test (uses your master key)
curl -s http://localhost:4000/health/readiness
curl -s http://localhost:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"

# 4. A chat completion via OpenRouter
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
```

Validate the compose file without starting anything:

```bash
docker compose -f docker-compose.yml config
```

---

## Quickstart — k3s (production shape)

Requirements: `kubectl` against your cluster.

```bash
# 1. Namespace
kubectl apply -f k8s/00-namespace.yaml

# 2. Secrets — copy the example, fill REAL values, apply from a PRIVATE copy.
#    NEVER commit the filled-in file.
cp k8s/01-secrets.example.yaml /tmp/01-secrets.yaml
#    Replace every REPLACE_ME_* value:
#      litellm-secrets:          OPENROUTER_API_KEY, LITELLM_MASTER_KEY,
#                                LITELLM_SALT_KEY, DATABASE_URL
#      litellm-postgres-secret:  POSTGRES_PASSWORD (match DATABASE_URL)
#      cloudflared-token:        TUNNEL_TOKEN (existing tunnel)
kubectl apply -f /tmp/01-secrets.yaml

# 3. Postgres, config, proxy, tunnel connector
kubectl apply -f k8s/02-postgres.yaml
kubectl apply -f k8s/03-litellm-config.yaml
kubectl apply -f k8s/04-litellm-deployment.yaml
kubectl apply -f k8s/05-cloudflared.yaml

# 4. Verify
kubectl -n litellm get pods
kubectl -n litellm exec deploy/litellm -c litellm -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:4000/health/readiness').read().decode())"
```

Notes:

- `DISABLE_SCHEMA_UPDATE=true` on the proxy pods keeps them from running DB
  migrations (restart / multi-replica safe).
- `LITELLM_SALT_KEY` encrypts provider credentials stored in the DB — **set it
  once and never rotate it**, or stored credentials become undecryptable.
- The Cloudflare tunnel and its hostname mapping are managed in Cloudflare; the
  `cloudflared` Deployment only runs the connector using the token Secret.

---

## Virtual keys & governance

The master key (`LITELLM_MASTER_KEY`) is the admin credential. For everyday use,
issue **virtual keys** with budgets and model restrictions instead of handing out
the master key.

Create a scoped virtual key:

```bash
curl -s http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"models":["gpt-4o-mini"],"max_budget":5,"key_alias":"demo"}'
# -> {"key":"sk-...", ...}
```

The returned `sk-...` key:

- can call the models listed in `models` (e.g. `gpt-4o-mini`),
- is **blocked** from any model not in that list (returns an auth/permission
  error),
- stops working once it exceeds `max_budget`.

Keys, budgets and spend are persisted in Postgres, and everything is also
manageable from the Admin UI at **`/ui`** (log in with the master key).

---

## Skill Hub (Claude Code plugins)

This LiteLLM version exposes a Claude Code **Skill Hub / plugin registry**, so
Claude Code clients can discover skills served by the gateway.

Endpoints:

| Method & path                                | Auth        | Purpose                          |
| -------------------------------------------- | ----------- | -------------------------------- |
| `POST /claude-code/plugins`                  | master key  | register a skill/plugin          |
| `GET  /claude-code/plugins`                  | master key  | list registered plugins          |
| `GET  /claude-code/plugins/{plugin_name}`    | master key  | get one                          |
| `DELETE /claude-code/plugins/{plugin_name}`  | master key  | delete                           |
| `POST /claude-code/plugins/{name}/enable`    | master key  | enable                           |
| `POST /claude-code/plugins/{name}/disable`   | master key  | disable                          |
| `GET  /claude-code/marketplace.json`         | public      | marketplace manifest             |
| `GET  /public/skill_hub`                      | public      | public skill hub listing         |

Register a skill (required fields are `name` and `source`):

```bash
curl -s -X POST http://localhost:4000/claude-code/plugins \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "grill-me",
        "source": {"source": "github", "repo": "anthropics/skills"},
        "description": "Interview skill",
        "domain": "Productivity",
        "namespace": "skills"
      }'

# then list / discover
curl -s http://localhost:4000/claude-code/plugins \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
curl -s http://localhost:4000/claude-code/marketplace.json
```

`source` supports `github` (`{"source":"github","repo":"org/repo"}`), `url`
(`{"source":"url","url":"https://.../repo.git"}`) and `git-subdir`
(`{...,"path":"plugins/name"}`).

---

## Test suite (TDD acceptance)

The `tests/` directory holds the acceptance suite that must go green against a
running instance. It exercises three areas:

**A — Core gateway + persistence**
- `A1` `/health/readiness` → status healthy **and** DB connected.
- `A2` `/v1/models` (Bearer master key) lists the OpenRouter-backed models.
- `A3` chat completions return real non-empty content for `gpt-4o-mini`,
  `glm-4.6`, `minimax-m2` via OpenRouter.
- `A4` persistence sanity: DB connected and `store_model_in_db: true`.
- `A5` `docker-compose.yml` is schema-valid.

**B — Key governance + external**
- `B1` create a virtual key via `POST /key/generate` with `max_budget` + model
  restriction; response contains an `sk-` key.
- `B2` that key can call an allowed model and is blocked from a non-allowed one.
- `B3` external tunnel: `https://litellm.woowtech.io/health/readiness` → healthy.
- `B4` Admin UI reachable: `GET /ui` → HTTP 200.

**C — Skill Hub**
- `C1` register a Claude Code skill and confirm it is listed.
- `C2` `GET /claude-code/marketplace.json` is retrievable.

Because the gateway calls out to OpenRouter, tests need a valid
`OPENROUTER_API_KEY` in the environment (the proxy reads it from its Secret /
`.env`). Against the k3s instance, API tests run **inside** a litellm pod using
Python's `urllib` (the image ships Python but not `curl`), while the external
tunnel check is done against the public URL.

---

## Security

- **No real secrets are committed to this repository.** `.env.example` and
  `k8s/01-secrets.example.yaml` contain **placeholders only**.
- The OpenRouter key, master key, salt key and database URL are always read from
  the environment (`os.environ/...`) or a k8s Secret — never hardcoded in
  `config/config.yaml`.
- Copy the example files to a private location, fill real values there, and keep
  the filled-in `.env` / secret manifests out of version control.
- Distribute scoped **virtual keys** (with budgets and model allow-lists) to
  users instead of the master key.
- `LITELLM_SALT_KEY` must be set once and never rotated.
