# Acceptance / TDD Suite

This directory holds the test-driven acceptance suite for the **LiteLLM gateway**
deployed to the k3s `litellm` namespace and exposed externally at
`https://litellm.woowtech.io`.

The suite is written **test-first**: every check maps to a concrete acceptance
criterion, and the deploy is not "done" until all executed checks are green.

## Files

| File            | Purpose                                                                 |
| --------------- | ----------------------------------------------------------------------- |
| `acceptance.py` | Self-contained (stdlib-only) API acceptance runner. Prints a JSON scorecard. |
| `README.md`     | This document — the acceptance criteria and how they are run.           |

## Why the tests run *inside* the pod

This cloud sandbox's egress **blocks `woowtech.io`** (the sandbox proxy returns
`403` to outbound requests), so the API-level checks cannot hit the external URL
directly. Instead they run **inside a running `litellm` pod** against the
pod-local `http://localhost:4000`, driven via the k3s MCP tool
`mcp__k3s_mcp__pods_exec` (container `litellm`).

The litellm image ships **`python` but not `curl`**, so `acceptance.py` uses only
the Python standard library (`urllib`) — no third-party deps, no shell tools.

The one exception is the **external tunnel** check (B3), which must verify the
Cloudflare tunnel end-to-end. That is done from the agent side with the
**`WebFetch`** tool against `https://litellm.woowtech.io/health/readiness`
(WebFetch is not subject to the curl-proxy 403).

## How to run

### API checks (A1–A4, B1, B2, B4, C1, C2) — inside the pod

Run `acceptance.py` inside a `litellm` pod via `pods_exec`. Supply the master key
through the `MASTER_KEY` env var — **never hardcode it** in the repo.

```
# conceptually, inside the litellm pod (container: litellm):
BASE_URL=http://localhost:4000 MASTER_KEY=<litellm-master-key> python /tmp/acceptance.py
```

Because the sandbox copies files into the pod indirectly, the Deploy+Test phase
typically streams the script over stdin:

```
MASTER_KEY=<master-key> python - <<'PY'
<contents of acceptance.py>
PY
```

The script prints a JSON scorecard:

```json
{
  "base_url": "http://localhost:4000",
  "summary": { "passed": 11, "total": 11, "all_green": true },
  "results": [ { "id": "A1", "pass": true, "detail": "status=healthy db=connected ..." }, ... ]
}
```

Exit code is `0` only when every executed check passes.

### A5 — docker-compose schema validity (outside the pod)

Validate the shared compose file on the host that has the repo checked out:

```
docker compose -f docker-compose.yml config      # preferred (needs docker)
```

If `docker` is absent in the sandbox, validate structurally with Python instead
(YAML parses + the compose-spec top-level keys are present):

```
python -c "import yaml,sys; d=yaml.safe_load(open('docker-compose.yml')); \
assert 'services' in d and 'litellm' in d['services']; print('compose ok')"
```

### B3 — external tunnel health (agent side, WebFetch)

Use the `WebFetch` tool (not curl) against the public URL and assert the JSON
reports healthy:

```
WebFetch https://litellm.woowtech.io/health/readiness
  -> expect {"status": "healthy", "db": "connected", ...}
```

## Acceptance criteria

### A. Core gateway + persistence

| ID | Criterion | Where checked |
| -- | --------- | ------------- |
| **A1** | `GET /health/readiness` reports `status: healthy` **and** `db: connected`. | `acceptance.py` (pod) |
| **A2** | `GET /v1/models` (Bearer master key) lists the OpenRouter-backed model names: `gpt-4o-mini`, `glm-4.6`, `minimax-m2`. | `acceptance.py` (pod) |
| **A3** | Chat completion returns **real non-empty content** for all three families via OpenRouter (`gpt-4o-mini`, `glm-4.6`, `minimax-m2`), Bearer master key. Reported as `A3-gpt-4o-mini`, `A3-glm-4.6`, `A3-minimax-m2`. | `acceptance.py` (pod) |
| **A4** | Persistence sanity: `db: connected` **and** `store_model_in_db` is `true` (non-destructive — no restart needed). | `acceptance.py` (pod) |
| **A5** | `docker-compose.yml` is schema-valid. | `docker compose config` / Python YAML check (host) |

### B. Key governance + external

| ID | Criterion | Where checked |
| -- | --------- | ------------- |
| **B1** | `POST /key/generate` (master key) with `max_budget` + `models` restriction returns a response containing an `sk-` key. | `acceptance.py` (pod) |
| **B2** | That virtual key **can** call an allowed model (`gpt-4o-mini`) and is **blocked** (400/401/403) from a non-allowed model (`glm-4.6`). | `acceptance.py` (pod) |
| **B3** | External tunnel: `https://litellm.woowtech.io/health/readiness` reports healthy. | `WebFetch` (agent) |
| **B4** | Admin UI reachable: `GET /ui` returns HTTP `200`. | `acceptance.py` (pod) |

### C. Skill Hub

| ID | Criterion | Where checked |
| -- | --------- | ------------- |
| **C1** | Register a Claude Code skill via `POST /claude-code/plugins` (master key), then `GET /claude-code/plugins` lists it. | `acceptance.py` (pod) |
| **C2** | `GET /claude-code/marketplace.json` is retrievable (public, no auth). | `acceptance.py` (pod) |

> Skill Hub endpoints are **confirmed present** in LiteLLM `v1.83.14`
> (verified against the live pod's `/openapi.json`). If a future image lacks them,
> `acceptance.py` records the exact `404`/response in the `detail` field so the
> report can note it instead of silently passing.

## Security note

No real secrets live in this repo. `acceptance.py` reads the master key **only**
from the `MASTER_KEY` environment variable, and the gateway reads
`OPENROUTER_API_KEY` from the k8s Secret via `os.environ/OPENROUTER_API_KEY`.
Never commit the OpenRouter key or the LiteLLM master key.
