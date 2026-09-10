# MCP 流量工作台操作指南

MCP 是左側導覽中的獨立工作台。每個任務各自保存網站範圍、捕獲工作階段、原始請求、重送、Agent 測試與報告；它不會建立或接管既有的 Web／Internal 掃描。

第一版採用手動設定的瀏覽器代理。網頁按下「啟動代理」後，還需要將測試瀏覽器的 HTTP／HTTPS 代理指向顯示的位址；單純開啟 MCP 頁面不會改變瀏覽器的網路設定。

## 安裝與啟動

Console 的 Python 環境須依專案 lockfile 安裝，前端須重新建置。Docker daemon 必須可由執行 Console 的帳號存取。捕獲使用獨立的 mitmproxy 映像，不需要先啟動 Strix 掃描容器。

在專案目錄執行：

```bash
npm --prefix console/web run build
uv sync --frozen --no-dev
docker pull ghcr.io/mitmproxy/mitmproxy:12.2.3@sha256:00b77b5d8804c8ad18cb6caefbf9d5849e895e8986c5ce011f4ae30f4385962f
```

啟動單一 Console process，不需要先設定 MCP 環境變數：

```bash
uv run --no-dev strixops-console --host 127.0.0.1 --port 8300
```

MCP 預設使用 `~/.strixops/mcp_tasks`，與 `STRIX_RUNS` 分開。需要自訂儲存位置時，才設定 `STRIXOPS_MCP_ROOT`；例如服務帳號擁有的 `/var/lib/strixops/mcp_tasks`。升級時沿用現有資料目錄。

開啟 [本機 Console](http://127.0.0.1:8300)，在左側選擇 MCP。初次驗證建議由 Console 同一個來源提供前端和 API；開發環境另行使用 Next 時，需要保留 MCP API 的同源請求條件。

使用 Next 的本機 `3100` 開發伺服器時，在啟動 Console 的終端明確設定開發來源，再啟動前端：

```bash
export STRIXOPS_MCP_TRUSTED_ORIGINS="http://localhost:3100,http://127.0.0.1:3100"
uv run --no-dev strixops-console --host 127.0.0.1 --port 8300
```

```bash
npm --prefix console/web run dev
```

這個設定只允許由 loopback 上游轉送、且來源完全符合清單的開發請求，不是全面開啟跨來源存取。開發代理預設轉送到 `http://127.0.0.1:8300`；若 API 使用其他埠，啟動 Next 時可設定 `STRIXOPS_MCP_DEV_API`。

**目前以單一 Console worker 運作。** 捕獲監看、測試執行與取消訊號由該 process 管理，不要對相同 MCP 資料目錄同時啟動多個 Console worker。

## 建立任務並連接瀏覽器

1. 按「建立 MCP 任務」，填入名稱與允許網站；如有需要，再填排除主機。
2. 按「啟動代理」，等待顯示監聽位址及可下載的 CA 憑證。使用遠端 Console 位址時，代理帳密會自動產生，可從連線資訊中顯示及複製。
3. 在專用測試瀏覽器的代理設定中，將 HTTP 與 HTTPS 都指向該位址。
4. 下載平台共用的公開 CA，匯入該測試瀏覽器的受信任憑證庫，並勾選信任此 CA 識別網站。相同 MCP 資料目錄下的所有任務共用此 CA，只需匯入一次。
5. 以該瀏覽器登入、切換頁面及操作功能。範圍內的實際網路流量會持續顯示在工作台。

從本機 Console 位址啟動時，代理只綁定 loopback；從遠端 IP 位址啟動時，會自動綁定主機介面，顯示該 IP，並產生獨立的代理帳密。每個工作階段使用不同的可用連接埠。若透過 SSH 通道開啟本機 Console 位址，代理也維持 loopback；例如工作階段顯示的伺服器連接埠為 `49152`：

```bash
ssh -N -L 18080:127.0.0.1:49152 user@your-server
```

此時瀏覽器代理設定為本機 `127.0.0.1:18080`。Console 頁面可另依主 [README 的遠端存取說明](../README.md#remote-access) 建立通道。

「停止捕獲」會關閉該監聽器；需要繼續一般瀏覽時，將瀏覽器切回直連。再次啟動會建立新的工作階段與連接埠，但沿用同一張平台 CA；只需更新代理位址及新工作階段的代理帳密，不必重新匯入憑證。建立或刪除任務也不會更換 CA。

從舊版升級時，平台會優先沿用已有的有效 CA；已在執行的舊代理保留原憑證，直到下次啟動。若舊代理與共用 CA 不同，介面會提示重新監聽以切換。共用 CA 只提供公開憑證下載及 SHA-256 指紋；私鑰由平台保存。更換資料目錄、明確更換 CA 或憑證到期時，才需要重新匯入。

下載端點只提供 `mitmproxy-ca-cert.pem` 公開憑證。相鄰的 `mitmproxy-ca.pem` 含私鑰，不應用於下載或分享。

## 網站範圍

允許與排除規則支援以下形式，每行一條：

| 範例 | 意義 |
|---|---|
| `example.com` | 精確主機名稱；不自動包含子網域 |
| `*.example.com` | 子網域；不包含裸網域 `example.com` |
| `https://api.example.com` | 限 HTTPS 與預設 443 埠 |
| `https://api.example.com:8443` | 限指定協定及連接埠 |
| `127.0.0.1:8080` | 指定 IP 與連接埠；IP 的意義取決於容器網路位置 |

規則不接受路徑、query、帳號或密碼。排除規則優先。變更設定會增加 scope revision；新請求採用新規則，已開始的請求與對應回應維持開始時的範圍判斷，歷史資料不會因此被刪除。

不在範圍內的瀏覽器流量仍會轉送，但不寫入捕獲資料。新建立的範圍外 HTTPS 連線會以加密通道轉送，不解密其內容。已建立的 TLS 連線不會因修改規則而重新進行握手。

捕獲範圍決定可以蒐集哪些網站；按下「Agent 測試」時，還會固定本次選取的請求、當時的範圍與測試預算。Agent 不會因為主機列在允許清單中，就開始遍歷整個網站。

## 檢視流量與端點

工作台將流量分成頁面、API、資源及其他類型，可依主機、路徑、方法與來源篩選。來源區分為瀏覽器、手動重送及 Agent；重送永遠新增證據，原始請求不會被覆寫。

點選一筆流量可查看 request／response、重複 headers、body、HTTP 版本、狀態與耗時。預設遮罩常見憑證欄位；需要檢視原始內容時，可使用介面的揭露操作。遮罩是顯示層行為，原始證據仍保存在此主機的任務資料中。

端點清單會將數字或類似識別碼的路徑區段彙整，例如 `/api/items/123` 與 `/api/items/124` 可能分成 `/api/items/{id}`。這是觀察資料的推測分組，不代表後端正式路由規格，也不代表該端點所有參數與權限情境都已被測試。

本版蒐集的是經過代理的網路請求。SPA 純前端路由、快取或 service worker 直接回應的內容，可能不產生可捕獲的網路請求；沒有經過代理的 client 也不在清單中。介面沒有提供 DOM 執行或瀏覽器導覽錄製。

## 重送與 Agent 測試

手動「重送請求」可修改方法、同來源 URL、一般 headers 及 body。傳輸層管理 Host、Content-Length、Connection 等 framing／proxy headers；不提供 CONNECT、原始 request smuggling 或跨來源攜帶登入憑證的重送。原始 request body 尚未完成或已被截斷時，不能重送。

單筆或批次選取請求後，可按「Agent 測試」：

- 選擇 Settings 中已有的模型設定檔。這裡讀取其 Web 模型路由設定，但執行的是獨立的請求測試 runner，不會啟動 Web 掃描。
- 選擇相容的 HTTP 測試 skills，並填入這次希望驗證的問題。
- 設定請求次數與時間預算。預設最多 12 次重送、120 秒；每次工作最多選取 50 筆原始請求。
- 執行後查看進度、測試產生的流量、覆蓋記錄、finding 與未完成原因。

模型設定檔和 skill 內容共用既有管理介面；每次 TestJob 會保存當時的 prompt／skill 快照及雜湊。任務預設設定之後的變更不會改寫已建立的測試快照。

目前 runner 提供選取請求的查看、受限制重送及證據記錄；沒有 shell、原始碼存取、DOM／瀏覽器執行、OAST callback、子 Agent 或任意 HTTP 探索工具。需要其他身份、更新登入狀態、CSRF token、未選取的前置流程或上述能力時，結果應保留為需要後續處理，不算測試通過。

「刪除任務」可從任務清單或任務詳情操作。確認後先停止該任務的代理並取消、等待進行中的測試，再移除任務、流量、測試結果、報告與工作階段檔案。刪除無法復原；平台共用 CA、其他 MCP 任務及原有 Web／Internal 任務不受影響。若停止容器或清理檔案失敗，畫面會回報錯誤，保留可重試的任務紀錄。

「取消測試」只取消該 TestJob，不停止瀏覽器捕獲。「停止捕獲」後仍可對已保存的完整請求重送或測試。「結束任務」會停止代理並取消進行中的工作，保留歷史證據；結束後不能再送出測試請求。

每筆 HTTP 重送在自己的短期容器中執行；容器不接收 Docker socket，也不執行模型產生的 shell。容器隔離工具執行環境，對目標重送的寫入型請求仍可能改變該目標的資料。

## 報告

「產生新報告」會以目前保存的端點與測試結果建立不可變版本，並可下載 Markdown。產生報告本身不再掃描目標，也不再次呼叫模型。

報告包含觀察範圍、端點樣本、測試狀態、原始請求與新證據的識別碼、finding 及未完成的覆蓋項目。沒有執行測試時，報告會明確說明目前只有流量盤點。

`source_sha256` 識別該版本的端點摘要與測試結果快照；它不是所有原始封包檔案的完整性簽章。較晚的捕獲、測試或設定變更不會更新已產生的報告；需要時再建立新版本。

## MCP 連線與存取設定

供相容 MCP client 使用的 Streamable HTTP 端點為：

```text
http://127.0.0.1:8300/api/mcp/transport
```

它與前端操作同一份任務資料，提供建立／刪除任務、共用 CA 資訊、啟停捕獲、查詢請求與端點、重送、建立／取消測試及報告工具。MCP client 斷線不會自動關閉捕獲工作階段。

### 自動初始化

從 Console 的 IP 或 localhost 開啟 MCP 時，前端會自動初始化存取設定，將控制 Token 保存於 MCP 資料目錄的 `access.json`，瀏覽器在目前分頁的 `sessionStorage` 保存使用副本。重新開啟或重新啟動 Console 會沿用同一份設定；新增／刪除任務不會更換控制 Token 或共用 CA。伺服器檔案權限為 `0600`；內容損壞時會回報錯誤，不會默默覆寫已有設定。

初始化沿用整個 Console 的部署存取邊界：**能夠開啟此 Console 的使用者，也能初始化 MCP 並操作共用任務**，沒有另外建立使用者帳號或租戶隔離。初始化只接受符合 Host／protocol 的同來源請求及指定 header；任意 DNS 網域、跨網站或未獲允許的跨來源初始化會被拒絕。明確列入可信來源的本機開發轉送保留例外，允許其瀏覽器 Origin 與 loopback 上游 Host 不同。這項防護處理瀏覽器跨來源呼叫，不是獨立的身份驗證機制。

後續 MCP 請求與 CA 下載透過 header 傳送 Token，不放進 URL。若明確設定 `STRIXOPS_MCP_TOKEN`，則優先使用手動 Token 登入，自動初始化不會繞過該設定。Web／Internal 的流程保持不變。

### 直接連接主機 IP

例如主機 IP 是 `192.168.1.20`，Console 已監聽 `0.0.0.0:8300`，不需要新增 MCP 環境變數：

```bash
uv run --no-dev strixops-console --host 0.0.0.0 --port 8300
```

1. 開啟 `http://192.168.1.20:8300/mcp`，等待自動初始化。
2. 建立任務並啟動代理，將測試瀏覽器的 HTTP／HTTPS 代理設為頁面顯示的主機與埠。
3. 在連線資訊中顯示及複製自動產生的代理帳密，填入瀏覽器的代理驗證提示。
4. 下載並信任共用 CA 一次，即可捕獲範圍內的 HTTPS。

控制 Token 與代理帳密用途不同。代理帳密在每次新捕獲工作階段產生，保存於該工作階段的私有 `proxy-auth.json`。重新整理頁面或重啟 Console 後，可再次顯示既有工作階段的帳密；停止後再啟動會建立新的帳密。帳密不包含在任務輪詢、報告、連線 URL 或捕獲 journal 中，只有明確點選顯示時才讀取。未提供有效代理帳密時，遠端代理回應 HTTP 407。

主機防火牆仍須允許測試電腦連到動態代理埠。HTTP 範例用於可信內網；對外部署應沿用整個 Console 的 HTTPS 與身份驗證層。升級不會重啟已執行的捕獲；要套用自動遠端設定，請停止並重新啟動該任務的代理。

### 進階覆寫與反向代理

以下全部為選用設定；一般 IP 直連工作流程不需要設定。

| 環境變數 | 用途 |
|---|---|
| `STRIXOPS_MCP_ROOT` | MCP SQLite、控制 Token、CA 與任務資料的根目錄 |
| `STRIXOPS_MCP_TOKEN` | 明確指定控制 Token，切回手動登入；API／MCP client 可用 `Authorization: Bearer ...` 或 `X-MCP-Token` |
| `STRIXOPS_MCP_TRUSTED_ORIGINS` | 逗號分隔的完整 Console 來源；允許該 DNS 來源自動初始化，並保留既有 loopback 可信代理／開發來源豁免 |
| `STRIXOPS_MCP_PROXY_BIND_HOST` | 覆寫新代理綁定的 IP；未設定時依啟動請求的 Console 位址選擇 loopback 或 IPv4／IPv6 萬用介面 |
| `STRIXOPS_MCP_PROXY_PUBLIC_HOST` | 覆寫瀏覽器應使用的 IP／主機名稱；供 NAT 或不同代理主機使用，不填 URL、埠號或萬用位址 |
| `STRIXOPS_MCP_PROXY_AUTH` | 覆寫代理帳密，格式 `username:password`；未設定時非 loopback 代理自動產生。明確設定的帳密不透過前端揭露 |
| `STRIXOPS_CONSOLE_CONFIG` | 共用模型設定檔位置，沿用 Console 設定 |

若透過 DNS 網域及 Nginx／Caddy 開啟 Console，將完整來源（例如 `https://console.example.com`）加入 `STRIXOPS_MCP_TRUSTED_ORIGINS`，並保留同來源 UI 與 `/api/mcp`，讓轉送的 protocol／Host 與瀏覽器一致。此清單本身不是登入機制；應由既有部署層負責驗證身份。

若已明確設定 MCP Token，可在 MCP 頁面輸入，或由完成身份驗證的代理覆寫並注入 `X-MCP-Token`。Uvicorn 還原 `X-Forwarded-For` 後，自動初始化仍可發出 Token，不依賴 loopback 來源豁免。直接以 MCP client 工具啟動捕獲時沒有瀏覽器位址提示，預設維持 loopback；若需要遠端代理，可用上列進階設定指定其綁定及公開位址。

## 儲存、限制與故障處理

資料目錄結構如下：

```text
mcp_tasks/
├── traffic.sqlite3
├── access.json                 # 自動產生的控制 Token（0600）
├── ca/                         # 平台共用 CA（含私鑰，須持久保存）
└── tasks/<task_id>/captures/<session_id>/
    ├── scope.json
    ├── runtime.json
    ├── proxy-auth.json         # 自動產生的該工作階段代理帳密（0600）
    ├── events.jsonl
    ├── capture-status.json
    └── ca/                     # 只有舊版工作階段保留此目錄
```

SQLite 保存任務、flow、job、report 與事件；capture journal 保存增量資料。`runtime.json` 包含用來核對容器所有權的私有資料，讓 Console 在啟動途中重啟後仍可復原及停止正確容器。

目前不自動清理歷史工作階段；只有明確刪除任務時會一併清除其資料。備份時先停止捕獲並等待測試結束，再備份完整 MCP 資料目錄；不要只複製正在寫入的 SQLite 主檔而漏掉其 WAL 資料。

| 項目 | 本版界限 |
|---|---|
| 捕獲協定 | HTTP／HTTPS，包含 HTTP/2；重送使用 HTTP/1.1 |
| 捕獲 body | 每個 request／response 保存最多 256 KiB 前綴；超過會標示截斷，流量仍繼續轉送 |
| Journal | 每個捕獲工作階段最多 128 MiB；達上限後停止記錄並顯示錯誤，代理仍轉送 |
| 串流回應 | headers 到達即可顯示，持續保存有界前綴；不代表完整 SSE 或 WebSocket 訊息測試能力 |
| 重送 | 不跟隨重新導向；request 最多 1 MiB，response 最多 256 KiB 前綴 |
| 單次重送生命週期 | worker 最多約 30 秒，Console 外層最多 40 秒；完成後自動移除該容器 |
| TLS 上游 | 驗證目標憑證；企業私有 CA 需另行安排受信任 CA，沒有提供停用驗證的開關 |
| 完整網站盤點 | 僅限觀察到的網路流量；未操作、未經代理、DOM／快取路徑不保證可見 |
| 進階功能 | 暫停攔截、完整 WebSocket frame 工作台、HTTP/3 測試及託管瀏覽器不在本版 |

| 狀況 | 處理方式 |
|---|---|
| 顯示映像尚未安裝 | 執行上方固定版本與 digest 的 `docker pull`；啟動按鈕不會自動拉取映像 |
| Docker 無法存取 | 確認 daemon 已啟動，並檢查執行 Console 帳號的 Docker 權限 |
| 有代理位址但沒有流量 | 確認瀏覽器實際使用該 proxy、SSH 通道連接埠正確，且目標符合允許規則 |
| MCP 自動初始化失敗 | 確認前後端皆已更新、使用主機 IP 或已設定的可信網域同源開啟；檢查 MCP 資料目錄的寫入權限 |
| 仍顯示手動 Token 登入 | 檢查服務是否保留明確的 `STRIXOPS_MCP_TOKEN`；它會優先使用手動模式。要改用自動模式，移除該覆寫後重啟 Console |
| 代理要求登入／回應 407 | 從該任務連線資訊顯示並複製自動帳密；若使用明確的環境覆寫，則使用其帳密。不能以控制 Token 代替 |
| HTTPS 憑證錯誤 | 確認已信任平台共用 CA；舊版代理則確認其 CA 是否相同。另須分辨瀏覽器不信任代理，還是代理不信任目標上游憑證 |
| 網頁切換卻沒有新頁面 | 檢查是否為 SPA 路由或快取回應；單次前端路由切換不必然產生 HTTP request |
| Journal 達上限或磁碟寫入失敗 | 保留現有資料，處理容量後停止並建立新捕獲工作階段；勿將仍在轉送視為仍在記錄 |
| Console 重啟時有測試進行中 | 該 TestJob 會標示未完成；短期 request worker 自行逾時並清理。檢視既有證據後，另啟新測試 |

## 開發驗證

以下可重跑本機測試。Docker contract 測試只建立自己的容器與暫時本機 HTTP／HTTPS fixture，模型 provider 使用替身，不對外部目標執行請求：

```bash
uv sync --frozen --dev
.venv/bin/python -m pytest tests/unit/test_mcp* tests/contract/test_mcp* -q
STRIXOPS_MCP_DOCKER_TESTS=1 .venv/bin/python -m pytest tests/contract/test_mcp_docker.py tests/contract/test_mcp_shared_ca_docker.py tests/contract/test_mcp_delete_docker.py tests/contract/test_mcp_remote_proxy_docker.py -q
```

本次實際 Docker 驗證涵蓋 HTTP／HTTPS、HTTP/2、範圍外 TLS 原樣轉送、重複 headers、binary body、停止捕獲後重送、雙任務隔離、Console metadata 復原、資料遮罩、流量識別碼與報告雜湊持久化。共用 CA 測試以同一份 TLS 信任設定連接兩個任務，再停止、刪除其中一個任務並建立新工作階段，確認憑證保持一致；刪除測試確認取消 Agent、停止所屬容器、清除資料並保留其他任務與 CA。

另已透過 headless Chromium 驗證建立任務、實際瀏覽器捕獲、停止、重送、Agent 表單與結果（模型替身）、報告下載、共用 CA 下載、清單／詳情刪除與取消確認。長網址、header 和任務名稱涵蓋 320–1536px 版面；真實模型輸出品質仍需依部署的模型與目標驗證。
