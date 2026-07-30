# Telegram 點餐 Bot — 專案說明文件（簡化版）

這份文件彙整了目前這個專案所有需要記住的資訊：金鑰、帳密、部署設定、操作方式。
請自行保管好這份文件，不要外流（裡面有實際可用的密鑰）。

> **這次做了大幅簡化**：拿掉了整個「餐廳管理」「菜單照片」「後台網頁」功能。
> 現在的邏輯是——開單之後，大家自己在聊天室講要吃什麼、自己貼照片參考，
> bot 完全不管這件事，只負責把「品項 金額 備註」格式的訊息記錄下來，結單時彙整。

---

## 1. 服務位置

| 項目 | 內容 |
|---|---|
| Render 服務網址 | https://ggman.onrender.com |
| 保活健康檢查端點 | https://ggman.onrender.com/health |
| GitHub Repo | `168cod-del/ggman`（branch: `main`） |
| 部署平台 | Render（Free 方案） |

> `/admin` 後台網頁已經拿掉了，現在整個服務只剩 `/telegram-webhook` 和 `/health` 兩個網址。

---

## 2. 金鑰與密碼（機密資訊）

| 項目 | 值 | 用途 |
|---|---|---|
| Telegram Bot Token | `8396988188:AAHnH2wRRu0IpnMB7gicvqwXc6bB8f-axso` | bot 登入憑證，寫在 `main.py` 的 `BOT_TOKEN` |
| Webhook 密鑰 | `3d5503dd54239bc4e701d1645cb9e456` | 驗證 webhook 推送真的來自 Telegram，寫在 `WEBHOOK_SECRET_TOKEN` |
| DATABASE_URL | Neon 給的 PostgreSQL 連線字串 | 設定成 Render 的 Environment Variable，讓場次資料重啟不會消失。實際值請自行到 Neon 後台查看 |

### ⚠️ 安全性提醒
Bot Token 已經在對話紀錄裡曝光過，建議找時間去 [@BotFather](https://t.me/BotFather)
用 `/revoke` 重新產生一組新的，並改用 Render 的 Environment Variables 存放，
不要寫死在 `main.py` 裡。

> 之前提過的 Google AI Studio (Gemini) API Key 已經完全用不到了（照片辨識菜單的功能
> 從頭到尾都放棄了），可以不用再管它。
> 後台管理密碼（原本是 `a123456`）也隨著後台網頁一起拿掉了，不用再記。

---

## 3. Render 部署設定

| 設定項目 | 值 |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Runtime | Python 3 |
| Environment Variables | `PYTHON_VERSION` = `3.11.9`（避免新版 Python 編譯套件失敗）<br>`DATABASE_URL` = Neon 的連線字串（要讓場次資料持久化才需要設定） |
| Instance Type | Free |

**Free 方案的限制**：閒置約 15 分鐘會自動休眠，有新訊息時會透過 webhook 自動喚醒，
但第一則訊息可能延遲 30–60 秒。建議用 [UptimeRobot](https://uptimerobot.com)
（免費）每 5 分鐘打一次 `https://ggman.onrender.com/health`，降低喚醒延遲。

---

## 4. BotFather 設定狀態

| 設定 | 狀態 |
|---|---|
| Group Privacy | **Disabled**（必須關閉，否則群組裡的點餐文字訊息 bot 收不到） |
| Menu Button | 之前設定過但已經完全用不到了，可以去 BotFather 移除掉 |

### 指令選單（「/」圖示點開的那個清單）

去 BotFather 打 `/setcommands`，選這個 bot，貼上：

```
start - 開單（發起點餐，第一個打的人是發起人）
delorder - 刪除我的訂單
help - 使用說明
end - 結束點餐（限發起人）
```

（之前清單裡的 `/newmenu`、`/admin` 已經拿掉，因為對應的功能都刪除了）

---

## 5. 專案檔案結構

```
your-repo/
├── main.py              # 後端主程式（Bot + FastAPI 網站）
└── requirements.txt      # Python 套件清單
```

> `static/` 資料夾（`admin.html`、`select_restaurant.html`、`history.html` 等）
> 已經完全沒有用了，repo 裡如果還留著可以整個資料夾刪除。

---

## 6. 使用者操作指令一覽

### 開始點餐
打 `/开单`、`/開單` 或 `/start` 皆可。該聊天室裡**第一個**打這個指令的人，
會被記錄為本場「發起人」。開單後不需要選餐廳、不需要等任何人上傳菜單——
想吃什麼、要參考哪間店，大家自己在聊天室講、自己貼照片就好，bot 不會處理照片，
純粹讓大家自己看。

### 點餐（直接在聊天室打字，金額為必填）

| 格式 | 範例 |
|---|---|
| 單品項 | `牛排X2 300 五分熟` |
| 多品項＋各自金額 | `地瓜球 紅茶 30+30 少冰` |
| 多品項（+ 號連接）＋各自金額 | `牛肉麵+豬頭皮+酸菜 30+30+30` |
| 多品項（無分隔符號） | `牛肉麵30豬頭皮30酸菜30` |
| 多品項＋共用總金額 | `牛肉麵 豬頭皮 酸菜 90` |
| 多品項＋共用總金額（＝符號） | `牛肉麵 豬頭皮 酸菜＝90` |

品項之間可以用 `+`、`/`、`.` 或空白分隔。同一人重複輸入會**覆蓋**前一筆訂單。
沒有金額或看起來不像點餐內容的訊息，一律安靜忽略，不會有任何回應。

### 刪除自己的訂單
打 `/删`、`/删单`、`/删除`（或繁體 `/刪`、`/刪單`、`/刪除`），或正式指令 `/delorder`，
不需要二次確認，成功會回覆「刪單成功」四個字。

### 查看使用說明
打 `/help` 會回覆一份精簡的操作說明。

### 結束點餐（限發起人）
打 `/结单`、`/結單`、`/end`，或直接按「🛑 結束點餐」按鈕皆可。
結束後會顯示：
- 每人明細（誰點了什麼、備註、多少錢）
- 品項彙總（同名品項自動加總數量）
- 合計金額

結束後場次記憶會清除，下次要重新 `/开单`。

---

## 7. 重要限制與已知行為

- **場次資料**：有設定 `DATABASE_URL` 的話會同步存進 Neon 資料庫，Render 重新部署
  或重啟不會清空，啟動時會自動載回記憶體。沒設定的話就是純記憶體模式，重啟會清空。
- Telegram 規定「web_app 型態的按鈕」只能在私訊使用，群組裡用會直接報錯——
  這支程式現在完全沒有用到 Mini App / WebApp，「結束點餐」用的是一般的 Inline
  按鈕（callback_data），不受這個限制影響。
- 點餐用的文字辨識只在「該聊天室有進行中場次」時才會運作，其餘時間 bot 對群組裡
  的任何對話（包含照片）都不會有反應。

---

## 8. 之後可能的優化方向（尚未實作）

- 輪替目前已曝光的 Bot Token，改用環境變數管理
- 如果之後又想加回「照片辨識菜單」「餐廳管理」之類的功能，這份文件保留的歷史脈絡
  可以參考，但目前判斷這類功能實際使用率低、維護成本高，暫不建議恢復
