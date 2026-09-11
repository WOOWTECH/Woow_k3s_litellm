# Woow_k3s_litellm — 部署在 K3s 上的 LiteLLM 閘道

[English](README.md)

WoowTech 的 LiteLLM 閘道：用一組跟 OpenAI 相容的 API 接多家模型，全部經由
**OpenRouter** 轉發。Postgres 存 virtual key、預算、用量，以及從管理介面新增的模型；
Cloudflare Tunnel 負責對外開放閘道和 MCP 管理介面；每天用 `pg_dump` 備份資料庫。

`k8s/` 裡的檔案就是 **woow-k3s** 叢集（kubectl context `woow-k3s`）上正在跑的內容，
可以用 `scripts/check-drift.sh` 驗證兩邊一致。

| 用途 | 網址 |
|---|---|
| 閘道 API（`/v1/*`） | https://litellm.woowtech.io |
| 管理介面（帳號 `admin`，密碼是 master key） | https://litellm.woowtech.io/ui |
| 健康檢查（會檢查資料庫，不需金鑰） | https://litellm.woowtech.io/health/readiness |
| MCP 管理介面 | https://litellm-mcp.woowtech.io |

---

## 架構

```
 使用者（OpenAI SDK、Claude Code、curl）
        │ Bearer sk-...
        ▼
 Cloudflare ── tunnel ──► cloudflared ×2 ─────────────┐  （ns litellm）
                            │ litellm.woowtech.io      │ litellm-mcp.woowtech.io
                            ▼                          ▼
                     litellm :4000              litellm-mcp-admin :8080  （ns litellm-mcp）
                       │        │                      │
        openrouter.ai ◄┘        │ DATABASE_URL         └──► litellm.litellm.svc:4000
                                ▼
                     litellm-postgres :5432  ◄── NetworkPolicy：只允許 app=litellm / app=litellm-backup
                       │ Longhorn 5Gi（Retain）
                       ▼
                     litellm-postgres-backup（每天台北時間 03:15）──► litellm-backups PVC（保留 14 天）
```

## 模型

全部經由 OpenRouter：`gpt-4o-mini`、`glm-4.6`、`minimax-m2`、`claude-sonnet-4.5`、`llama-3.3-70b`
（對照表見 [README.md](README.md#models)）。也可以在管理介面新增模型，會存進資料庫。

## 部署

kubectl 一律明確帶 `--context`。

```bash
# 1. 建立 namespace
kubectl --context woow-k3s apply -f k8s/00-namespaces.yaml

# 2. Secret：複製到 repo 以外的地方，把所有 REPLACE_ME 換成真值，再套用那份副本
cp examples/secrets.example.yaml /secure/path/secrets.yaml
kubectl --context woow-k3s apply -f /secure/path/secrets.yaml

# 3. 其餘全部（k8s/ 裡沒有 Secret，整個目錄套用是安全的）
kubectl --context woow-k3s apply -f k8s/

# 4. 等待就緒（第一次啟動會跑資料庫遷移，大約 2 分鐘）
kubectl --context woow-k3s -n litellm rollout status deploy/litellm --timeout=10m
```

Tunnel 的網址對應設定在 Cloudflare 後台，不在這個 repo 裡：

| 網址 | 對應服務（這些名稱不能改） |
|---|---|
| `litellm.woowtech.io` | `http://litellm:4000` |
| `litellm-mcp.woowtech.io` | `http://litellm-mcp-admin.litellm-mcp.svc.cluster.local:8080` |

**同一組 tunnel token 只能有一套部署在跑。** 多一套連線端，Cloudflare 會把流量拆給兩邊。

改模型：修改 `config/config.yaml`，把同樣的內容貼進 `k8s/03-litellm-config.yaml`（CI 會檢查兩者一致），
套用後再執行 `kubectl --context woow-k3s -n litellm rollout restart deploy/litellm`
（只改 ConfigMap 不會自動重啟）。

## 驗證

```bash
curl -s https://litellm.woowtech.io/health/readiness
kubectl --context woow-k3s -n litellm exec -i deploy/litellm -c litellm -- \
  sh -c 'MASTER_KEY="$LITELLM_MASTER_KEY" python -' < tests/acceptance.py
scripts/check-drift.sh        # exit 0 表示 repo 跟叢集完全一致
```

`acceptance.py` 會實際呼叫模型（花 OpenRouter 額度），也會在資料庫留下一把限額的 virtual key
和一個 `grill-me` 外掛。

## 備份與還原

- 每天台北時間 03:15 把 `pg_dump -Fc` 的結果寫進 `litellm-backups` PVC，保留 14 天。
- 想立刻備份一次：
  `kubectl --context woow-k3s -n litellm create job backup-$(date +%s) --from=cronjob/litellm-postgres-backup`
- **`LITELLM_SALT_KEY` 一定要另外備份到叢集以外的地方。** 備份檔裡的供應商憑證是用它加密的；
  一旦更換或遺失就解不開，而且 LiteLLM 不會報錯，只是默默讀不到。
- 還原步驟見 [README.md](README.md#backup-and-restore)（尚未實際演練過，請先演練再依賴它）。

## 設計重點

這份設定結合了原本的 k3s manifest、實際跑過的版本，以及
[Woow_podman_litellm](https://github.com/WOOWTECH/Woow_podman_litellm) 的做法：

- `DISABLE_SCHEMA_UPDATE=false`：設成 true 的話，空資料庫永遠建不出資料表。原版 manifest 寫的是 true，但那個值從來沒上線過。
- `startupProbe` 40×15 秒：第一次啟動最多給 10 分鐘跑遷移；之後的檢查逾時都是 10 秒。
- initContainer 用 `pg_isready` 等資料庫，而不是只測連接埠有沒有開。
- Postgres 設了 NetworkPolicy；備份工作會先等 `pg_isready` 成功，因為 woow-k3s 上新開的 pod 一開始可能還沒被 NetworkPolicy 放行。
- cloudflared 加上 `--metrics 0.0.0.0:2000`，確保 `/ready` 檢查打得到。
- 儲存用 Longhorn（Retain）：刪掉 PVC 也不會連資料一起刪。

## 沿革

- **2026-07-23**：第一次部署在本機筆電的 k3s（context `default`，儲存用 local-path）。
- **2026-08-04**：以 `Woow_litellm_docker_compose` 的名稱寫成這個 repo。當時的 manifest 跟實際在跑的不一致，除了 08-05 的模型修正之外從來沒被套用過。
- **2026-09-11**：本機叢集的節點失聯，存在 local-path 上的資料庫一起遺失。改從合併後的設定部署到 woow-k3s，**資料庫從零開始**：模型和 master key 不變，舊的 virtual key 與用量紀錄都沒了。repo 同時改名為 `Woow_k3s_litellm`，docker-compose 的部分移除，單機部署請改用 Woow_podman_litellm。

## 相關 repo

- [Woow_podman_litellm](https://github.com/WOOWTECH/Woow_podman_litellm)：同一套服務的單機 Podman 版
- [Woow_litellm_mcp_server](https://github.com/WOOWTECH/Woow_litellm_mcp_server)：MCP 管理介面的原始碼
