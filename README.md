# Woow_k3s_litellm — LiteLLM Gateway Helm Chart

[中文說明](README_zh-TW.md)

The WoowTech LiteLLM gateway: one OpenAI-compatible API in front of multiple
LLM families, all routed through **OpenRouter**. Postgres stores virtual keys,
budgets, spend and UI-added models. A Cloudflare Tunnel publishes the gateway,
and a daily `pg_dump` CronJob backs up the database.

This chart is what runs on the **woow-k3s** cluster (kubectl context `woow-k3s`)
as Helm release `litellm` in namespace `litellm`. Its instance values are in
[`deploy/woow-k3s/litellm.yaml`](deploy/woow-k3s/litellm.yaml) (no secrets; they
equal the chart defaults), and `scripts/check-drift.sh` compares the chart with
the release and the live objects.

The **LiteLLM MCP admin console** (`litellm-mcp-admin`, namespace `litellm-mcp`)
is no longer part of this chart. Since chart 2.0.0 it is its own chart and
release in [Woow_litellm_mcp_server](https://github.com/WOOWTECH/Woow_litellm_mcp_server).
The tunnel connector stays here and still carries its hostname. Read
[Phase 2: MCP objects leave this release](#phase-2-mcp-objects-leave-this-release)
before the first upgrade of the live release to 2.x.

| Endpoint | URL |
|---|---|
| Gateway API (`/v1/*`) | https://litellm.woowtech.io |
| Admin UI (user `admin`, password = master key) | https://litellm.woowtech.io/ui |
| Readiness (DB-aware, no auth) | https://litellm.woowtech.io/health/readiness |
| MCP admin console (separate chart, same tunnel) | https://litellm-mcp.woowtech.io |

---

## Architecture

```
 Clients (OpenAI SDK, Claude Code, curl)
        │ Bearer sk-...
        ▼
 Cloudflare ── tunnel ──► cloudflared ×2 ─────────────┐  (ns litellm, this chart)
                            │ litellm.woowtech.io      │ litellm-mcp.woowtech.io
                            ▼                          ▼
                     litellm :4000              litellm-mcp-admin :8080  (ns litellm-mcp,
                       │        │                                         Woow_litellm_mcp_server chart)
        openrouter.ai ◄┘        │ DATABASE_URL
                                ▼
                     litellm-postgres :5432  ◄── NetworkPolicy: only app=litellm / app=litellm-backup
                       │ Longhorn 5Gi (Retain)
                       ▼
                     litellm-postgres-backup (CronJob 03:15 Asia/Taipei) ──► litellm-backups PVC (14 days)
```

| Template | Objects | Toggle |
|---|---|---|
| `templates/litellm.yaml` | ConfigMap (from `config/config.yaml`), Deployment, Service :4000 | always |
| `templates/postgres.yaml` | headless Service, StatefulSet, NetworkPolicy | `networkPolicy.enabled` |
| `templates/cloudflared.yaml` | Deployment ×2 | `cloudflared.enabled` |
| `templates/backup.yaml` | PVC, CronJob | `backup.enabled` |
| `templates/secrets.yaml` | the 3 Secrets (`litellm-secrets`, `litellm-postgres-secret`, `cloudflared-token`) | `secrets.create` |
| `templates/namespace.yaml` | Namespace `namespace.name`, only when it differs from the release namespace | `namespace.create` |
| `templates/tests/smoke.yaml` | `helm test` pod | `tests.enabled` |

## Models

All models go through OpenRouter (`api_key: os.environ/OPENROUTER_API_KEY`).
They are defined in `config/config.yaml`, which the chart reads directly, so
there is no second copy to keep in sync.

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

## Quick start

Always pass the context explicitly (`--kube-context` for helm, `--context` for kubectl).

### A. Let the chart create the Secrets

Keep the values file with the real keys **outside** the repository:

```bash
cat > ~/secure/litellm-secrets.values.yaml <<EOF
secrets:
  create: true
  openrouterApiKey: sk-or-...                   # from OpenRouter
  masterKey: sk-$(openssl rand -hex 32)
  saltKey: sk-$(openssl rand -hex 32)           # set once, never rotate, back it up
  postgresPassword: $(openssl rand -hex 24)     # letters and digits only
  tunnelToken: <cloudflare tunnel token>        # only with cloudflared.enabled=true
EOF
chmod 600 ~/secure/litellm-secrets.values.yaml

# Install straight from the repo tarball (no clone needed)
helm --kube-context woow-k3s install litellm \
  https://github.com/WOOWTECH/Woow_k3s_litellm/archive/refs/heads/main.tar.gz \
  -n litellm --create-namespace -f ~/secure/litellm-secrets.values.yaml

# Or from a local clone
git clone https://github.com/WOOWTECH/Woow_k3s_litellm.git && cd Woow_k3s_litellm
helm --kube-context woow-k3s install litellm . -n litellm --create-namespace \
  -f ~/secure/litellm-secrets.values.yaml
```

Every later `helm upgrade` needs the same `-f` file. Without it, the
`required` checks stop the upgrade; the keys are never silently blanked.

### B. Manage the Secrets outside Helm (how woow-k3s runs)

With the default `secrets.create=false`, the chart never renders or touches a
Secret, so no upgrade can ever overwrite a real key.

```bash
kubectl --context woow-k3s create namespace litellm
cp examples/secrets.example.yaml ~/secure/secrets.yaml      # fill every REPLACE_ME
kubectl --context woow-k3s apply -f ~/secure/secrets.yaml

helm --kube-context woow-k3s install litellm . -n litellm -f deploy/woow-k3s/litellm.yaml
```

### C. Test install (no tunnel, disposable storage)

A test install must never start a tunnel connector. Use its own namespace, a
StorageClass that deletes volumes, and generated credentials:

```bash
NS=ht-litellm
helm --kube-context woow-k3s install litellm . -n $NS --create-namespace \
  --set namespace.name=$NS,storageClassName=longhorn-delete,cloudflared.enabled=false \
  --set secrets.create=true,secrets.openrouterApiKey=sk-or-dummy \
  --set secrets.masterKey=sk-$(openssl rand -hex 16),secrets.saltKey=sk-$(openssl rand -hex 16) \
  --set secrets.postgresPassword=$(openssl rand -hex 16)
```

`/health/readiness` and `/v1/models` work with a dummy OpenRouter key; only
real chat completions need a real one.

### Then

```bash
# First boot runs the Prisma migrations (about 2 minutes)
kubectl --context woow-k3s -n litellm rollout status deploy/litellm --timeout=10m
helm --kube-context woow-k3s test litellm -n litellm --logs
```

The tunnel's hostname routing is managed in Cloudflare, not in this chart:

| Hostname | Service | Deployed by |
|---|---|---|
| `litellm.woowtech.io` | `http://litellm:4000` | this chart (fixed name) |
| `litellm-mcp.woowtech.io` | `http://litellm-mcp-admin.litellm-mcp.svc.cluster.local:8080` | Woow_litellm_mcp_server chart |

**Only one deployment may run the tunnel token.** A second set of connectors
joins the same tunnel and Cloudflare splits traffic between them. Set
`cloudflared.enabled=false` for any test install.

## Key values

| Value | Default | Description |
|---|---|---|
| `namespace.name` | `litellm` | Namespace of every object |
| `namespace.create` | `true` | Render that Namespace when it differs from the release namespace |
| `keepOnUninstall` | `true` | `helm.sh/resource-policy: keep` on Namespaces, PVCs and chart-created Secrets |
| `storageClassName` | `longhorn` | Default StorageClass (Longhorn, reclaimPolicy Retain) |
| `secrets.create` | `false` | Render the Secrets from `secrets.*` instead of using existing ones |
| `litellm.image.tag` | `v1.83.14-stable` | LiteLLM version |
| `litellm.logLevel` | `ERROR` | `LITELLM_LOG` |
| `litellm.resources` | 250m/512Mi → 2/2Gi | Proxy requests/limits |
| `postgres.storage.size` | `5Gi` | Database volume |
| `cloudflared.enabled` / `replicas` | `true` / `2` | Tunnel connector |
| `backup.enabled` / `schedule` / `retentionDays` | `true` / `15 3 * * *` / `14` | Daily `pg_dump` (Asia/Taipei) |
| `tests.enabled` | `true` | `helm test` smoke pod |

Full list: [`values.yaml`](values.yaml). Chart 2.0.0 removed every `mcp.*` and
`secrets.mcp.*` value; Helm ignores them if an old values file still sets them.

## Verify

```bash
helm --kube-context woow-k3s test litellm -n litellm --logs   # liveness, readiness + DB, all config models (read-only)
curl -s https://litellm.woowtech.io/health/readiness

# Full acceptance suite, run inside the pod with the pod's own master key
kubectl --context woow-k3s -n litellm exec -i deploy/litellm -c litellm -- \
  sh -c 'MASTER_KEY="$LITELLM_MASTER_KEY" python -' < tests/acceptance.py

# Repo vs Helm release vs live objects (exit 0 = identical)
scripts/check-drift.sh -f deploy/woow-k3s/litellm.yaml
```

`acceptance.py` makes real model calls, which cost OpenRouter credits. It also
leaves a budget-limited virtual key and a `grill-me` plugin in the database.
See [tests/README.md](tests/README.md).

## Changing models or settings

```bash
$EDITOR config/config.yaml          # or deploy/woow-k3s/litellm.yaml
helm --kube-context woow-k3s upgrade litellm . -n litellm -f deploy/woow-k3s/litellm.yaml
# A config.yaml change alone does not restart the proxy:
kubectl --context woow-k3s -n litellm rollout restart deploy/litellm
```

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

## Uninstall

```bash
helm --kube-context woow-k3s uninstall litellm -n litellm
```

This removes the workloads, Services, ConfigMap, NetworkPolicy and CronJob.
Data stays:

- the `litellm-backups` PVC and every chart-created Secret carry the keep policy;
- the database PVC `pgdata-litellm-postgres-0` comes from the StatefulSet's
  `volumeClaimTemplates`, which Helm never deletes;
- the `litellm` namespace is the release namespace and is never rendered, so it
  is never deleted. A Namespace the chart does render (`namespace.name` different
  from `-n`) carries the keep policy.

To really delete everything, including the data:

```bash
kubectl --context woow-k3s delete namespace litellm
# Longhorn "longhorn" is Retain: the PVs stay Released until you delete them.
```

## Phase 2: MCP objects leave this release

Release `litellm` was installed with chart 1.0.0, which also rendered the MCP
console. Its release manifest therefore still owns four objects that chart
2.0.0 no longer renders:

| Object | Keep policy on the live object today |
|---|---|
| Namespace `litellm-mcp` | yes (`keepOnUninstall`) |
| PVC `litellm-mcp-data` (ns `litellm-mcp`) | yes (`keepOnUninstall`) |
| Deployment `litellm-mcp-admin` (ns `litellm-mcp`) | **no** |
| Service `litellm-mcp-admin` (ns `litellm-mcp`) | **no** |

A `helm upgrade` to 2.x deletes every object that left the chart unless the
**live** object has `helm.sh/resource-policy: keep` (Helm reads the annotation
from the cluster at delete time). Without it the upgrade deletes the MCP
Deployment and Service, and https://litellm-mcp.woowtech.io goes down. So,
**before** that upgrade:

```bash
C="--context woow-k3s -n litellm-mcp"
# 1. Keep the MCP objects the release still owns
kubectl $C annotate deploy/litellm-mcp-admin svc/litellm-mcp-admin helm.sh/resource-policy=keep
kubectl --context woow-k3s get ns litellm-mcp -o jsonpath='{.metadata.annotations.helm\.sh/resource-policy}{"\n"}'  # keep
kubectl $C get pvc litellm-mcp-data -o jsonpath='{.metadata.annotations.helm\.sh/resource-policy}{"\n"}'            # keep
kubectl $C get pod -l app=litellm-mcp-admin -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,RESTARTS:.status.containerStatuses[0].restartCount

# 2. Upgrade litellm to 2.x. Helm logs "Skipping delete ... due to annotation"
#    for the four objects; every other object renders identically, so nothing restarts.
helm --kube-context woow-k3s upgrade litellm . -n litellm -f deploy/woow-k3s/litellm.yaml
kubectl $C get pod -l app=litellm-mcp-admin -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,RESTARTS:.status.containerStatuses[0].restartCount

# 3. Adopt the four objects into the MCP chart (see Woow_litellm_mcp_server):
#    helm upgrade --install ... --take-ownership
```

Until step 2, `scripts/check-drift.sh` reports exactly those four objects as
removed; every other object is identical. After step 3 the objects still carry
the manual keep annotation; remove it
(`kubectl $C annotate deploy/litellm-mcp-admin svc/litellm-mcp-admin helm.sh/resource-policy-`)
only if the MCP chart does not want it.

## Migrating from the kubectl manifests

Up to 2026-09-11 this repository held plain manifests in `k8s/`, applied with
`kubectl`. Rendered with default values, chart 1.0.0 was resource-equivalent to
them, field by field. The only intentional differences were:

1. Namespace `litellm` is not rendered, because it is the release namespace.
2. Namespaces, PVCs and chart-created Secrets carry `helm.sh/resource-policy: keep`.
3. The `woowtech.io/source` provenance annotation is gone.
4. The ConfigMap is filled from `config/config.yaml` instead of an embedded copy.

Chart 2.0.0 additionally drops the MCP console (see Phase 2 above). Pod
templates, selectors and `volumeClaimTemplates` of everything else are
unchanged. An existing kubectl deployment of the gateway can therefore be
adopted without restarting any pod:

```bash
helm --kube-context woow-k3s upgrade --install litellm . -n litellm \
  -f deploy/woow-k3s/litellm.yaml --take-ownership
```

The old manifests remain in the git history (tag `kubectl-manifests`).

## Design decisions

| Setting | Why |
|---|---|
| Fixed resource names and labels | The tunnel routes to these Service names. Changing a selector or pod label would restart every pod |
| `secrets.create=false` by default | An upgrade can never overwrite real keys |
| Keep policy on Namespaces, PVCs and Secrets | `helm uninstall` can never delete the database or the salt key |
| MCP console in its own chart | One release per MCP instance, upgraded independently of the gateway |
| cloudflared stays in this chart | The tunnel belongs to the app chart; it also carries the MCP hostname |
| `DISABLE_SCHEMA_UPDATE=false` | With `true`, an empty database never gets its tables |
| `startupProbe` 40×15s | Gives first-boot migrations up to 10 minutes. Afterwards the probes use 10s timeouts |
| `pg_isready` initContainer | A bare TCP check passes while Postgres is still initialising |
| `RollingUpdate maxUnavailable 0 / maxSurge 1` | No gap during rollouts. Only the new pod migrates |
| NetworkPolicy on Postgres | The database holds every key and encrypted credential |
| `pg_isready` wait in the backup job | On woow-k3s a fresh pod can race the NetworkPolicy controller |
| cloudflared `--metrics 0.0.0.0:2000` | Pins the port the `/ready` probes use |

## History

- **2026-07-23**: first deployed on the local laptop cluster (context `default`) using `local-path` storage.
- **2026-08-04**: this repo was written as `Woow_litellm_docker_compose`. Its manifests drifted from what was running and were never applied, apart from the 2026-08-05 model fix.
- **2026-09-11**: the local cluster lost its nodes and the `local-path` database was lost with them. The gateway was redeployed on woow-k3s with an empty database: models and master key unchanged, old virtual keys and spend gone. The repo was renamed `Woow_k3s_litellm`, converted to this Helm chart, and the running deployment was adopted as release `litellm`.
- **2026-09-12**: chart 2.0.0 moves the MCP console to its own chart in Woow_litellm_mcp_server and adds `deploy/woow-k3s/litellm.yaml`.

## Related repositories

- [Woow_podman_litellm](https://github.com/WOOWTECH/Woow_podman_litellm): the same stack on a single host with rootless Podman
- [Woow_litellm_mcp_server](https://github.com/WOOWTECH/Woow_litellm_mcp_server): the MCP admin console and its Helm chart

## Security

- No real secret is committed. `values.yaml` has empty secret values, `examples/secrets.example.yaml` has placeholders, `deploy/` holds no secrets, and CI enforces all three.
- All keys are read from Secrets (`os.environ/...`), never from `config/config.yaml`.
- Give users scoped virtual keys (`POST /key/generate` with `models` and `max_budget`), never the master key.

## License

Internal WoowTech deployment configuration.
