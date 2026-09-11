# Acceptance suite

`acceptance.py` checks the live LiteLLM gateway (namespace `litellm` on
woow-k3s). It uses only the Python standard library, because the LiteLLM image
ships Python but not curl, so it runs **inside the litellm pod** and prints a
JSON scorecard. Exit code 0 means every check passed.

## Run

The master key never leaves the pod; the script reads the pod's own environment:

```bash
kubectl --context woow-k3s -n litellm exec -i deploy/litellm -c litellm -- \
  sh -c 'MASTER_KEY="$LITELLM_MASTER_KEY" python -' < tests/acceptance.py
```

The external tunnel check (B3) runs from any machine:

```bash
curl -s https://litellm.woowtech.io/health/readiness   # expect "status":"healthy","db":"connected"
```

## Checks

| ID | Check |
|---|---|
| A1 | `GET /health/readiness` reports `healthy` and `db: connected` |
| A2 | `GET /v1/models` (master key) lists `gpt-4o-mini`, `glm-4.6`, `minimax-m2` |
| A3 | A real chat completion returns non-empty content for each of those three models |
| A4 | Database connected and `store_model_in_db` on |
| B1 | `POST /key/generate` returns an `sk-` virtual key with a budget and a model allow-list |
| B2 | That key can call `gpt-4o-mini` and is refused for `glm-4.6` |
| B3 | Public `https://litellm.woowtech.io/health/readiness` is healthy (curl, outside the pod) |
| B4 | `GET /ui` returns 200 |
| C1 | `POST /claude-code/plugins` registers `grill-me`, and `GET` lists it |
| C2 | `GET /claude-code/marketplace.json` is retrievable |

## Side effects

This suite runs against the **production** database. It is not read-only:

- A3 and B2 make real model calls, which cost OpenRouter credits.
- B1 creates a virtual key (`acceptance-b1-<timestamp>`, budget $0.10) on every run and does not delete it.
- C1 registers the `grill-me` plugin. Re-running is idempotent.

Remove leftover test keys from the Admin UI (`/ui` → Virtual Keys) when needed.

Never probe plain `/health`: it calls every configured model on each request.
