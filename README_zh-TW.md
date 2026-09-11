# Woow_k3s_litellm — LiteLLM 閘道 Helm Chart

[English](README.md)

WoowTech 的 LiteLLM 閘道：用一組跟 OpenAI 相容的 API 接多家模型，全部經由
**OpenRouter** 轉發。Postgres 存 virtual key、預算、用量，以及從管理介面新增的模型；
Cloudflare Tunnel 負責對外開放閘道和 MCP 管理介面；每天用 `pg_dump` 備份資料庫。

這個 chart 就是 **woow-k3s** 叢集（kubectl context `woow-k3s`）上正在跑的版本：
Helm release `litellm`，namespace `litellm`。預設值會產生跟實際部署一模一樣的內容，
可以用 `scripts/check-drift.sh` 驗證。

| 用途 | 網址 |
|---|---|
| 閘道 API（`/v1/*`） | https://litellm.woowtech.io |
| 管理介面（帳號 `admin`，密碼是 master key） | https://litellm.woowtech.io/ui |
| 健康檢查（會檢查資料庫，不需金鑰） | https://litellm.woowtech.io/health/readiness |
| MCP 管理介面 | https://litellm-mcp.woowtech.io |

## 架構

```
 使用者 ──► Cloudflare ── tunnel ──► cloudflared ×2（ns litellm）
                                       ├─ litellm.woowtech.io     ──► litellm :4000 ──► litellm-postgres :5432（Longhorn 5Gi）
                                       └─ litellm-mcp.woowtech.io ──► litellm-mcp-admin :8080（ns litellm-mcp）
 litellm ──► openrouter.ai        litellm-postgres-backup（每天台北時間 03:15）──► litellm-backups PVC（保留 14 天）
```

模型：`gpt-4o-mini`、`glm-4.6`、`minimax-m2`、`claude-sonnet-4.5`、`llama-3.3-70b`，
定義在 `config/config.yaml`。chart 直接讀取這個檔案，不再有第二份需要手動同步。

## 安裝

helm 一律帶 `--kube-context`，kubectl 一律帶 `--context`。

**方式 A：由 chart 建立 Secret。** 放真實金鑰的 values 檔要存在 repo 以外的地方，
格式見 [README.md](README.md#a-let-the-chart-create-the-secrets)：

```bash
# 直接從 GitHub 安裝，不必 clone
helm --kube-context woow-k3s install litellm \
  https://github.com/WOOWTECH/Woow_k3s_litellm/archive/refs/heads/main.tar.gz \
  -n litellm --create-namespace -f ~/secure/litellm-secrets.values.yaml
```

之後每次 `helm upgrade` 都要帶同一個 `-f` 檔；少帶的話 `required` 檢查會直接擋下，
不會把金鑰清空。

**方式 B：Secret 由 Helm 以外管理（woow-k3s 目前的做法，預設 `secrets.create=false`）。**
chart 完全不碰 Secret，所以任何升級都不可能蓋掉真實金鑰：

```bash
kubectl --context woow-k3s create namespace litellm
kubectl --context woow-k3s create namespace litellm-mcp
cp examples/secrets.example.yaml ~/secure/secrets.yaml      # 把所有 REPLACE_ME 換成真值
kubectl --context woow-k3s apply -f ~/secure/secrets.yaml
helm --kube-context woow-k3s install litellm . -n litellm --take-ownership
```

裝好後等第一次啟動跑完資料庫遷移（大約 2 分鐘），再驗證：

```bash
kubectl --context woow-k3s -n litellm rollout status deploy/litellm --timeout=10m
helm --kube-context woow-k3s test litellm -n litellm --logs
```

Tunnel 的網址對應設定在 Cloudflare 後台：`litellm.woowtech.io` → `http://litellm:4000`，
`litellm-mcp.woowtech.io` → `http://litellm-mcp-admin.litellm-mcp.svc.cluster.local:8080`。
chart 裡的資源名稱是寫死的，就是為了保持這些對應。
**同一組 tunnel token 只能有一套部署在跑**；測試安裝請加 `--set cloudflared.enabled=false`。

## 常用設定

完整說明見 [README.md 的 Key values](README.md#key-values) 與 [`values.yaml`](values.yaml)。

- `secrets.create`（預設 false）：要不要由 chart 產生 Secret。
- `keepOnUninstall`（預設 true）：替 namespace、PVC 和 chart 建立的 Secret 加上 `helm.sh/resource-policy: keep`。
- `cloudflared.enabled`、`backup.enabled`、`mcp.enabled`、`networkPolicy.enabled`：各元件開關。
- `storageClassName`（預設 `longhorn`，回收策略 Retain）。

## 驗證與維運

```bash
helm --kube-context woow-k3s test litellm -n litellm --logs     # 唯讀煙霧測試
scripts/check-drift.sh                                           # 比對 repo、Helm release、實際物件三方是否一致
```

- 改模型：修改 `config/config.yaml` → `helm upgrade` → `kubectl rollout restart deploy/litellm`
  （只改 config 不會自動重啟）。
- 備份：每天台北時間 03:15 寫進 `litellm-backups` PVC，保留 14 天。
  **`LITELLM_SALT_KEY` 一定要另外備份到叢集以外**，否則備份檔裡的憑證解不開。
  還原步驟見 [README.md](README.md#backup-and-restore)（尚未實際演練過）。
- 移除：`helm uninstall` 只會刪掉工作負載；因為 keep 設定，namespace `litellm-mcp`、
  所有 PVC 和 chart 建立的 Secret 都會留下，namespace `litellm` 本來就不歸 release 管。

## 從 kubectl manifest 轉換過來

2026-09-11 以前，這個 repo 放的是用 kubectl 套用的 `k8s/*.yaml`。chart 用預設值產生的內容
跟當時的 manifest 逐欄位一致，刻意的差異只有四點：

1. namespace `litellm` 因為就是 release namespace，所以不由 chart 產生。
2. namespace、PVC、chart 建立的 Secret 加上 keep 設定。
3. 拿掉 `woowtech.io/source` 這個註記。
4. ConfigMap 直接讀 `config/config.yaml`。

pod template、selector、volumeClaimTemplates 完全沒變，所以既有的 kubectl 部署可以用
`helm upgrade --install litellm . -n litellm --take-ownership` 直接接管，不會重啟任何 pod。
舊的 manifest 保留在 git 歷史（tag `kubectl-manifests`）。

## 沿革

- **2026-07-23**：第一次部署在本機筆電的 k3s（local-path 儲存）。
- **2026-08-04**：寫成 `Woow_litellm_docker_compose`，但內容跟實際在跑的不一致，也從沒被套用。
- **2026-09-11**：本機叢集的節點失聯，資料庫一起遺失。改部署到 woow-k3s，資料庫從零開始；
  repo 改名為 `Woow_k3s_litellm`、改寫成這個 Helm chart，並用 Helm 接管了正在跑的部署。

## 相關 repo

- [Woow_podman_litellm](https://github.com/WOOWTECH/Woow_podman_litellm)：同一套服務的單機 Podman 版
- [Woow_litellm_mcp_server](https://github.com/WOOWTECH/Woow_litellm_mcp_server)：MCP 管理介面的原始碼
