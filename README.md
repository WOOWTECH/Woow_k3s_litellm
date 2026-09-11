# Woow_k3s_litellm — LiteLLM Gateway on K3s

[中文說明](README_zh-TW.md)

The WoowTech LiteLLM gateway: one OpenAI-compatible API in front of multiple
LLM families, all routed through **OpenRouter**. Postgres stores virtual keys,
budgets, spend and UI-added models. A Cloudflare Tunnel publishes the gateway
and its MCP admin console, and a daily `pg_dump` CronJob backs up the database.

The manifests in `k8s/` are exactly what runs on the **woow-k3s** cluster
(kubectl context `woow-k3s`). `scripts/check-drift.sh` proves it.

| Endpoint | URL |
|---|---|
| Gateway API (`/v1/*`) | https://litellm.woowtech.io |
| Admin UI (user `admin`, password = master key) | https://litellm.woowtech.io/ui |
| Readiness (DB-aware, no auth) | https://litellm.woowtech.io/health/readiness |
| MCP admin console | https://litellm-mcp.woowtech.io |

---

## Architecture

```
 Clients (OpenAI SDK, Claude Code, curl)
        │ Bearer sk-...
        ▼
 Cloudflare ── tunnel ──► cloudflared ×2 ─────────────┐  (ns litellm)
                            │ litellm.woowtech.io      │ litellm-mcp.woowtech.io
                            ▼                          ▼
                     litellm :4000              litellm-mcp-admin :8080  (ns litellm-mcp)
                       │        │                      │
        openrouter.ai ◄┘        │ DATABASE_URL         └──► litellm.litellm.svc:4000
                                ▼
                     litellm-postgres :5432  ◄── NetworkPolicy: only app=litellm / app=litellm-backup
                       │ Longhorn 5Gi (Retain)
                       ▼
                     litellm-postgres-backup (CronJob 03:15 Asia/Taipei) ──► litellm-backups PVC (14 days)
```

| Component | Kind | Image |
|---|---|---|
| `litellm` | Deployment + Service :4000 | `ghcr.io/berriai/litellm:v1.83.14-stable` |
| `litellm-postgres` | StatefulSet + headless Service :5432 | `docker.io/library/postgres:16-alpine` |
| `cloudflared` | Deployment ×2 | `cloudflare/cloudflared:latest` |
| `litellm-postgres-backup` | CronJob + PVC | `docker.io/library/postgres:16-alpine` |
| `litellm-mcp-admin` | Deployment + Service :8080 + PVC | built from [Woow_litellm_mcp_server](https://github.com/WOOWTECH/Woow_litellm_mcp_server) |

## Models

All models go through OpenRouter (`api_key: os.environ/OPENROUTER_API_KEY`):

| Public model name | OpenRouter model |
|---|---|
| `gpt-4o-mini` | `openrouter/openai/gpt-4o-mini` |
| `glm-4.6` | `openrouter/z-ai/glm-4.6` |
| `minimax-m2` | `openrouter/minimax/minimax-m2` |
| `claude-sonnet-4.5` | `openrouter/anthropic/claude-sonnet-4.5` |
| `llama-3.3-70b` | `openrouter/meta-llama/llama-3.3-70b-instruct` |

More models can be added in the Admin UI (`store_model_in_db: true`). Before
adding an `anthropic/*` slug, check it against
`GET https://openrouter.ai/api/v1/models`, because OpenRouter retires old slugs.

## Repository layout

```
config/config.yaml            LiteLLM config (single source; embedded in k8s/03)
k8s/00-namespaces.yaml        litellm, litellm-mcp
k8s/02-postgres.yaml          headless Service, StatefulSet, NetworkPolicy
k8s/03-litellm-config.yaml    ConfigMap = config/config.yaml (CI-enforced)
k8s/04-litellm-deployment.yaml proxy Deployment + Service
k8s/05-cloudflared.yaml       tunnel connector
k8s/06-backup.yaml            backup PVC + CronJob
k8s/10-litellm-mcp.yaml       MCP admin console
examples/secrets.example.yaml all 5 Secrets, placeholders only (kept OUT of k8s/)
scripts/check-drift.sh        kubectl diff k8s/ against the cluster
tests/acceptance.py           API acceptance suite (runs inside the litellm pod)
```

## Deploy

Always pass `--context` explicitly.

```bash
# 1. Namespaces
kubectl --context woow-k3s apply -f k8s/00-namespaces.yaml

# 2. Secrets: copy OUTSIDE the repo, fill every REPLACE_ME, apply that copy
cp examples/secrets.example.yaml /secure/path/secrets.yaml
kubectl --context woow-k3s apply -f /secure/path/secrets.yaml

# 3. Everything else (safe: k8s/ contains no Secrets)
kubectl --context woow-k3s apply -f k8s/

# 4. Wait for it (first boot runs the Prisma migrations, about 2 minutes)
kubectl --context woow-k3s -n litellm rollout status deploy/litellm --timeout=10m
kubectl --context woow-k3s -n litellm-mcp rollout status deploy/litellm-mcp-admin --timeout=10m
```

The tunnel's hostname routing is managed in Cloudflare, not in this repo:

| Hostname | Service (must keep these names) |
|---|---|
| `litellm.woowtech.io` | `http://litellm:4000` |
| `litellm-mcp.woowtech.io` | `http://litellm-mcp-admin.litellm-mcp.svc.cluster.local:8080` |

**Only one deployment may run the tunnel token.** A second set of connectors
joins the same tunnel and Cloudflare splits traffic between them.

### Changing models or settings

```bash
$EDITOR config/config.yaml            # then copy the same content into k8s/03-litellm-config.yaml
kubectl --context woow-k3s apply -f k8s/03-litellm-config.yaml
kubectl --context woow-k3s -n litellm rollout restart deploy/litellm   # a ConfigMap change alone does not restart
```

## Verify

```bash
curl -s https://litellm.woowtech.io/health/readiness     # {"status":"healthy","db":"connected",...}

# Full acceptance suite, run inside the pod with the pod's own master key
kubectl --context woow-k3s -n litellm exec -i deploy/litellm -c litellm -- \
  sh -c 'MASTER_KEY="$LITELLM_MASTER_KEY" python -' < tests/acceptance.py

# Repo vs cluster (exit 0 = identical)
scripts/check-drift.sh
```

`acceptance.py` makes real model calls, which cost OpenRouter credits. It also
leaves a budget-limited virtual key and a `grill-me` plugin in the database.
See [tests/README.md](tests/README.md).

## Backup and restore

- The `litellm-postgres-backup` CronJob runs `pg_dump -Fc` at 03:15 Asia/Taipei
  into the `litellm-backups` PVC and keeps 14 days.
- To run a backup now:
  `kubectl --context woow-k3s -n litellm create job backup-$(date +%s) --from=cronjob/litellm-postgres-backup`
- **`LITELLM_SALT_KEY` must be backed up outside the cluster.** Provider
  credentials in the dump are encrypted with it. Rotating or losing the key
  makes them undecryptable, and LiteLLM fails silently when that happens.

Restore. This procedure has not been exercised yet, so rehearse it before you
need it:

```bash
C="--context woow-k3s -n litellm"
kubectl $C scale deploy/litellm --replicas=0
kubectl $C run pg-restore --rm -i --restart=Never --image=docker.io/library/postgres:16-alpine \
  --labels=app=litellm-backup \
  --overrides='{"spec":{"volumes":[{"name":"b","persistentVolumeClaim":{"claimName":"litellm-backups"}}],
    "containers":[{"name":"pg-restore","image":"docker.io/library/postgres:16-alpine","stdin":true,
    "envFrom":[{"secretRef":{"name":"litellm-postgres-secret"}}],
    "volumeMounts":[{"name":"b","mountPath":"/backup"}],
    "command":["sh","-c","ls -l /backup; PGPASSWORD=$POSTGRES_PASSWORD pg_restore -h litellm-postgres -U $POSTGRES_USER -d $POSTGRES_DB --clean --if-exists --no-owner /backup/litellm-YYYYmmdd-HHMMSS.dump"]}]}}'
kubectl $C scale deploy/litellm --replicas=1
```

## Design decisions

These settings combine the original k3s manifests, the spec that ran in
production, and the single-host port
[Woow_podman_litellm](https://github.com/WOOWTECH/Woow_podman_litellm):

| Setting | Why |
|---|---|
| `DISABLE_SCHEMA_UPDATE=false` | With `true`, an empty database never gets its tables. The original manifests set `true`, but that value never ran in production |
| `startupProbe` 40×15s | Gives first-boot migrations up to 10 minutes. Afterwards readiness and liveness use 10s timeouts instead of `initialDelaySeconds` |
| `pg_isready` initContainer | A bare TCP check passes while Postgres is still initialising |
| `RollingUpdate maxUnavailable 0 / maxSurge 1` | No gap during rollouts. Only the new pod migrates |
| NetworkPolicy on Postgres | The database holds every key and encrypted credential |
| `pg_isready` wait in the backup job | On woow-k3s a fresh pod can race the NetworkPolicy controller |
| cloudflared `--metrics 0.0.0.0:2000` | Pins the port the `/ready` probes use |
| Longhorn `Retain` | Deleting a PVC does not delete the data |

## History

- **2026-07-23**: first deployed on the local laptop cluster (context `default`) using `local-path` storage.
- **2026-08-04**: this repo was written as `Woow_litellm_docker_compose`. Its manifests drifted from what was running and were never applied, apart from the 2026-08-05 model fix.
- **2026-09-11**: the local cluster lost its nodes and the `local-path` database was lost with them. The gateway was redeployed on woow-k3s from the merged spec with an **empty database**: models and master key unchanged, old virtual keys and spend history gone. The repo was renamed `Woow_k3s_litellm`, and the docker-compose files were removed in favour of Woow_podman_litellm.

## Related repositories

- [Woow_podman_litellm](https://github.com/WOOWTECH/Woow_podman_litellm): the same stack on a single host with rootless Podman (compose or Quadlet)
- [Woow_litellm_mcp_server](https://github.com/WOOWTECH/Woow_litellm_mcp_server): source of the MCP admin console

## Security

- No real secret is committed. `examples/secrets.example.yaml` holds placeholders, and `.gitignore` blocks secret-shaped files.
- All keys are read from Secrets (`os.environ/...`), never from `config/config.yaml`.
- Give users scoped virtual keys (`POST /key/generate` with `models` and `max_budget`), never the master key.
