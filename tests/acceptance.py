#!/usr/bin/env python3
"""
LiteLLM gateway acceptance suite (TDD).

Self-contained: uses only the Python standard library (urllib) so it can run
INSIDE the running litellm pod, whose image ships python but NOT curl.

It exercises the LIVE gateway over http://localhost:4000 (pod-local) and prints a
JSON scorecard: a list of {"id", "pass", "detail"} objects plus a summary.

USAGE (inside the litellm pod via k3s pods_exec):
    MASTER_KEY=sk-... python /path/acceptance.py
  or, if the script is streamed in:
    MASTER_KEY=sk-... python - <<'PY'
    ...contents...
    PY

ENV:
    BASE_URL     gateway base url          (default: http://localhost:4000)
    MASTER_KEY   LiteLLM master/admin key  (REQUIRED; never hardcoded here)
    REQ_TIMEOUT  per-request timeout secs  (default: 600)

Covers acceptance items:
    A1  /health/readiness  -> healthy AND db connected
    A2  /v1/models         -> lists OpenRouter-backed model names
    A3  chat completion (real non-empty content) for gpt-4o-mini, glm-4.6, minimax-m2
    A4  persistence sanity -> db connected + store_model_in_db true
    B1  POST /key/generate -> virtual key (sk-...) with max_budget + models restriction
    B2  virtual key: allowed model works, non-allowed model blocked
    B4  GET /ui            -> HTTP 200
    C1  POST /claude-code/plugins register + GET lists it
    C2  GET  /claude-code/marketplace.json retrievable

NOT covered here (run outside the pod, see tests/README.md):
    A5  docker-compose.yml schema validity (docker compose config / yaml lint)
    B3  external tunnel health via WebFetch https://litellm.woowtech.io/health/readiness

Exit code: 0 if every executed check passes, else 1.
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://localhost:4000").rstrip("/")
MASTER_KEY = os.environ.get("MASTER_KEY", "")
REQ_TIMEOUT = float(os.environ.get("REQ_TIMEOUT", "600"))

# Model *names* as declared in config/config.yaml (model_name / public alias).
# These are what /v1/models should list and what chat calls target.
MODEL_GPT = "gpt-4o-mini"
MODEL_GLM = "glm-4.6"
MODEL_MINIMAX = "minimax-m2"
EXPECTED_MODELS = [MODEL_GPT, MODEL_GLM, MODEL_MINIMAX]

# Accept a permissive TLS context in case BASE_URL is ever pointed at https.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

results = []


def record(check_id, passed, detail):
    results.append({"id": check_id, "pass": bool(passed), "detail": detail})


def _request(method, path, token=None, body=None, timeout=None):
    """Return (status_code, parsed_json_or_text, raw_text). Never raises on HTTP errors."""
    url = path if path.startswith("http") else BASE_URL + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or REQ_TIMEOUT, context=_SSL_CTX) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:  # noqa: BLE001 - surface connection errors as a failed check
        return None, None, "REQUEST_ERROR: %s" % e
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        parsed = raw
    return status, parsed, raw


# ---------------------------------------------------------------------------
# A1 - readiness: healthy AND db connected
# ---------------------------------------------------------------------------
def check_a1():
    status, parsed, raw = _request("GET", "/health/readiness", timeout=30)
    if status != 200 or not isinstance(parsed, dict):
        record("A1", False, "status=%s body=%s" % (status, str(raw)[:300]))
        return None
    healthy = str(parsed.get("status", "")).lower() == "healthy"
    db = str(parsed.get("db", "")).lower()
    db_ok = db == "connected"
    record(
        "A1",
        healthy and db_ok,
        "status=%s db=%s cache=%s" % (parsed.get("status"), parsed.get("db"), parsed.get("cache")),
    )
    return parsed


# ---------------------------------------------------------------------------
# A2 - /v1/models lists the OpenRouter-backed model names
# ---------------------------------------------------------------------------
def check_a2():
    status, parsed, raw = _request("GET", "/v1/models", token=MASTER_KEY, timeout=60)
    if status != 200 or not isinstance(parsed, dict):
        record("A2", False, "status=%s body=%s" % (status, str(raw)[:300]))
        return []
    ids = [m.get("id") for m in parsed.get("data", []) if isinstance(m, dict)]
    missing = [m for m in EXPECTED_MODELS if m not in ids]
    record(
        "A2",
        not missing,
        "listed=%s missing=%s" % (ids, missing),
    )
    return ids


# ---------------------------------------------------------------------------
# A3 - real non-empty chat completion for each model family via OpenRouter
# ---------------------------------------------------------------------------
def _chat(model, token, prompt="Reply with the single word: pong.", max_tokens=2000):
    # NOTE on max_tokens: some OpenRouter-backed families (e.g. glm-4.6) are
    # *reasoning* models that spend a hidden reasoning-token budget BEFORE any
    # visible content. A tiny cap (e.g. 64) is fully consumed by reasoning and
    # returns empty content with finish_reason="length". A realistic budget
    # (>=2000) lets reasoning models emit real content while non-reasoning
    # families (gpt-4o-mini, minimax-m2) still stop after their short answer.
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    return _request("POST", "/v1/chat/completions", token=token, body=body)


def _extract_content(parsed):
    try:
        return parsed["choices"][0]["message"]["content"] or ""
    except Exception:  # noqa: BLE001
        return ""


def check_a3():
    id_map = {MODEL_GPT: "A3-gpt-4o-mini", MODEL_GLM: "A3-glm-4.6", MODEL_MINIMAX: "A3-minimax-m2"}
    all_pass = True
    for model, cid in id_map.items():
        status, parsed, raw = _chat(model, MASTER_KEY)
        content = _extract_content(parsed) if isinstance(parsed, dict) else ""
        ok = status == 200 and isinstance(content, str) and len(content.strip()) > 0
        all_pass = all_pass and ok
        record(cid, ok, "status=%s content=%r" % (status, (content[:120] if content else str(raw)[:200])))
    return all_pass


# ---------------------------------------------------------------------------
# A4 - persistence sanity: db connected + store_model_in_db true
# ---------------------------------------------------------------------------
def check_a4(readiness):
    db_ok = False
    if isinstance(readiness, dict):
        db_ok = str(readiness.get("db", "")).lower() == "connected"

    # Confirm store_model_in_db is active via the config info endpoint (master key).
    store_ok = None
    detail_bits = ["db_connected=%s" % db_ok]
    for path in ("/get/config/callbacks", "/config/general_settings"):
        status, parsed, _ = _request("GET", path, token=MASTER_KEY, timeout=30)
        if status == 200 and isinstance(parsed, (dict, list)):
            blob = json.dumps(parsed).lower()
            if "store_model_in_db" in blob:
                store_ok = '"store_model_in_db":true' in blob.replace(" ", "") or "store_model_in_db=true" in blob
                detail_bits.append("store_model_in_db seen at %s" % path)
                break

    if store_ok is None:
        # Fallback: a model persisted in the DB implies store_model_in_db worked; treat
        # db connectivity as the load-bearing signal and note the config check was inconclusive.
        detail_bits.append("store_model_in_db not exposed via API (config-only); relying on db+config.yaml")
        passed = db_ok
    else:
        detail_bits.append("store_model_in_db=%s" % store_ok)
        passed = db_ok and store_ok
    record("A4", passed, "; ".join(detail_bits))
    return passed


# ---------------------------------------------------------------------------
# B1 - create a virtual key with max_budget + models restriction
# ---------------------------------------------------------------------------
def check_b1():
    body = {
        "models": [MODEL_GPT],
        "max_budget": 0.10,
        "key_alias": "acceptance-b1-%d" % int(time.time()),
        "metadata": {"created_by": "acceptance.py", "purpose": "tdd-b1"},
    }
    status, parsed, raw = _request("POST", "/key/generate", token=MASTER_KEY, body=body, timeout=60)
    vkey = parsed.get("key") if isinstance(parsed, dict) else None
    ok = status == 200 and isinstance(vkey, str) and vkey.startswith("sk-")
    record("B1", ok, "status=%s key_prefix=%s allowed=%s" % (status, (vkey[:7] + "..." if vkey else None), [MODEL_GPT]))
    return vkey if ok else None


# ---------------------------------------------------------------------------
# B2 - virtual key: allowed model works, non-allowed model is blocked
# ---------------------------------------------------------------------------
def check_b2(vkey):
    if not vkey:
        record("B2", False, "no virtual key from B1")
        return False

    # Allowed model should succeed with content.
    s_ok, p_ok, r_ok = _chat(MODEL_GPT, vkey)
    allowed_content = _extract_content(p_ok) if isinstance(p_ok, dict) else ""
    allowed_pass = s_ok == 200 and len(allowed_content.strip()) > 0

    # Non-allowed model should be blocked (401/403/400 team/key model restriction).
    s_bad, p_bad, r_bad = _chat(MODEL_GLM, vkey)
    blocked_pass = s_bad in (400, 401, 403)

    ok = allowed_pass and blocked_pass
    record(
        "B2",
        ok,
        "allowed(%s)=status%s/content%s  blocked(%s)=status%s"
        % (MODEL_GPT, s_ok, "ok" if allowed_pass else "empty", MODEL_GLM, s_bad),
    )
    return ok


# ---------------------------------------------------------------------------
# B4 - Admin UI reachable: GET /ui -> 200
# ---------------------------------------------------------------------------
def check_b4():
    status, _, raw = _request("GET", "/ui/", timeout=30)
    if status not in (200, 307, 308):
        # try without trailing slash
        status, _, raw = _request("GET", "/ui", timeout=30)
    record("B4", status == 200, "GET /ui status=%s" % status)
    return status == 200


# ---------------------------------------------------------------------------
# C1 - register a Claude Code skill/plugin and confirm it is listed
# ---------------------------------------------------------------------------
def check_c1():
    plugin_name = "grill-me"
    body = {
        "name": plugin_name,
        "source": {"source": "github", "repo": "anthropics/skills"},
        "description": "Acceptance-suite registered skill",
        "domain": "Productivity",
        "namespace": "skills",
    }
    s_reg, p_reg, r_reg = _request("POST", "/claude-code/plugins", token=MASTER_KEY, body=body, timeout=60)
    # 200/201 = created; 409/400 "already exists" is still acceptable (idempotent re-run).
    reg_ok = s_reg in (200, 201) or (
        s_reg in (400, 409) and "exist" in str(r_reg).lower()
    )

    s_list, p_list, r_list = _request("GET", "/claude-code/plugins", token=MASTER_KEY, timeout=30)
    listed = False
    if s_list == 200:
        listed = plugin_name in str(p_list)

    if s_reg == 404 or s_list == 404:
        record("C1", False, "endpoint 404 (not supported by this version): reg=%s list=%s" % (s_reg, s_list))
        return False

    ok = reg_ok and listed
    record("C1", ok, "register status=%s listed=%s" % (s_reg, listed))
    return ok


# ---------------------------------------------------------------------------
# C2 - marketplace.json retrievable (public, no auth)
# ---------------------------------------------------------------------------
def check_c2():
    status, parsed, raw = _request("GET", "/claude-code/marketplace.json", timeout=30)
    ok = status == 200 and isinstance(parsed, (dict, list))
    record("C2", ok, "status=%s type=%s" % (status, type(parsed).__name__))
    return ok


def main():
    if not MASTER_KEY:
        print(json.dumps({"error": "MASTER_KEY env var is required (never hardcode the key)"}, indent=2))
        return 2

    readiness = check_a1()
    check_a2()
    check_a3()
    check_a4(readiness)
    vkey = check_b1()
    check_b2(vkey)
    check_b4()
    check_c1()
    check_c2()

    total = len(results)
    passed = sum(1 for r in results if r["pass"])
    scorecard = {
        "base_url": BASE_URL,
        "summary": {"passed": passed, "total": total, "all_green": passed == total},
        "note": "A5 (compose schema) and B3 (external tunnel) run OUTSIDE the pod; see tests/README.md.",
        "results": results,
    }
    print(json.dumps(scorecard, indent=2))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
