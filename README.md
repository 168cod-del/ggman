# Telegram 點餐 Bot — 專案說明文件

這份文件彙整了目前這個專案所有需要記住的資訊：金鑰、帳密、部署設定、操作方式。
請自行保管好這份文件，不要外流（裡面有實際可用的密鑰）。

---

## 1. 服務位置

| 項目 | 內容 |
|---|---|
| Render 服務網址 | https://ggman.onrender.com |
| 後台管理網址 | https://ggman.onrender.com/admin |
| 保活健康檢查端點 | https://ggman.onrender.com/health |
| GitHub Repo | `168cod-del/ggman`（branch: `main`） |
| 部署平台 | Render（Free 方案） |

---

## 2. 金鑰與密碼（機密資訊）

| 項目 | 值 | 用途 |
|---|---|---|
| Telegram Bot Token | `8396988188:AAHnH2wRRu0IpnMB7gicvqwXc6bB8f-axso` | bot 登入憑證，寫在 `main.py` 的 `BOT_TOKEN` |
| Webhook 密鑰 | `3d5503dd54239bc4e701d1645cb9e456` | 驗證 webhook 推送真的來自 Telegram，寫在 `WEBHOOK_SECRET_TOKEN` |
| 後台管理密碼 | `a123456` | 登入 `/admin` 網頁用，寫在 `ADMIN_PASSWORD` |
| Google AI Studio (Gemini) API Key | `AQ.Ab8RN6L1AAOo15rPaF0lnlZfQph6-3zdFZ-MUJID9ATO1EyYnQ` | 原本要做「照片辨識菜單」，**這個功能後來放棄了，目前程式碼裡完全沒有用到這組 key** |

### ⚠️ 安全性提醒
- Bot Token 跟 Gemini API Key 都曾經直接貼在對話紀錄裡，等於已經曝光過。
  建議找時間：
  1. 去 [@BotFather](https://t.me/BotFather) 用 `/revoke` 重新產生一組新的 Bot Token
  2. 去 Google AI Studio 重新產生一組新的 API Key（如果之後還要用）
  3. 改用 Render 的 **Environment Variables** 存放這些值，不要寫死在 `main.py` 裡
- 目前是「堪用但不夠安全」的狀態，正式對外大量使用前建議處理掉這幾點。

---

## 3. Render 部署設定

| 設定項目 | 值 |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Runtime | Python 3 |
| Environment Variables | `PYTHON_VERSION` = `3.11.9`（避免新版 Python 編譯 pydantic 失敗） |
| Instance Type | Free |

**Free 方案的限制**：閒置約 15 分鐘會自動休眠，有新訊息時會透過 webhook 自動喚醒，
但第一則訊息可能延遲 30–60 秒。建議用 [UptimeRobot](https://uptimerobot.com)
（免費）每 5–10 分鐘打一次 `/health` 端點，可以大幅降低喚醒延遲。

---

## 4. BotFather 設定狀態

| 設定 | 狀態 |
|---|---|
| Group Privacy | **Disabled**（必須關閉，否則群組裡的點餐文字訊息 bot 收不到） |
| Menu Button | 之前設定過，但現在的架構已經不需要它了，網址也是舊的（GitHub Pages 時期）。
可以去 BotFather → `/mybots` → 選 bot → Bot Settings → Menu Button → **Remove Menu Button** 清掉，不影響現在的功能 |

### 指令選單（「/」圖示點開的那個清單）

Telegram 規定指令「名稱」只能是英文字母數字，不能是中文，所以清單上的指令一定是英文，
但後面的說明文字可以是中文。去 BotFather 打 `/setcommands`，選這個 bot，貼上：

```
start - 開單（發起點餐，第一個打的人是發起人）
delorder - 刪除我的訂單
help - 使用說明
end - 結束點餐（限發起人）
newmenu - 新增餐廳菜單（貼餐廳名稱後上傳照片）
admin - 後台管理連結
```

貼上後選單就會顯示這 6 個指令跟中文說明。

---

## 5. 專案檔案結構

```
your-repo/
├── main.py              # 後端主程式（Bot + FastAPI 網站）
├── requirements.txt      # Python 套件清單
└── static/
    └── admin.html        # 後台管理網頁
```

> 之前開發過程中出現過的 `select_restaurant.html`、`history.html`、舊版 `index.html`
> 現在都已經沒有用到，repo 裡如果還留著可以直接刪除。

---

## 6. 使用者操作指令一覽

### 開始點餐
打 `/开单`、`/開單` 或 `/start` 皆可。該聊天室裡**第一個**打這個指令的人，
會被記錄為本場「發起人」。

### 選餐廳
發起人從跳出來的餐廳按鈕清單裡點選一個。選定後會把該餐廳的菜單照片
（如果有上傳過）直接發到聊天室，讓大家對照著點餐。

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
- 每人明細（誰點了什麼、多少錢）
- 品項彙總（同名品項自動加總數量）
- 合計金額

結束後場次記憶會清除，下次要重新 `/开单`。

---

## 7. 新增餐廳（兩種方式都可以）

### 方式一：在 Telegram 聊天室
1. 打 `/新增菜單 餐廳名稱`（或 `/新增菜单`、`/newmenu`）
2. bot 會請你接著上傳一張菜單照片
3. **同一個人**傳一張照片過去，收到「已新增」的回覆就完成了

### 方式二：後台網頁
1. 開啟 https://ggman.onrender.com/admin，輸入密碼 `a123456`
2. 在「新增餐廳」欄位輸入名稱、按新增
3. 在清單裡按該餐廳的「📷 上傳照片」，選擇圖片（電腦、手機都可以直接用原生的選檔案/相簿介面）

後台也可以查看每間餐廳有沒有照片、刪除餐廳。

---

## 8. 重要限制與已知行為

- **資料都存在記憶體裡**：餐廳清單、菜單照片、進行中的點餐場次，
  只要 Render 重新部署或重啟就會**全部清空**，回到程式碼裡預設的 5 間測試餐廳
  （阿明快炒、巷口便當、涼夏冷飲、深夜燒烤、晨光早餐店，皆無照片）。
  之後要正式長期使用、資料不能不見的話，需要接上真正的資料庫。
- Telegram 規定「web_app 型態的按鈕」只能在私訊使用，群組裡用會直接報錯，
  所以選餐廳、結束點餐都用一般的 Inline 按鈕（callback_data），不是 Mini App。
- 點餐用的文字辨識只在「該聊天室有進行中場次、且已選好餐廳」時才會運作，
  其餘時間 bot 對群組裡的任何對話都不會有反應。
- webhook 模式搭配 Render 免費方案的休眠機制設計時特別注意：
  服務關閉時**不會**取消 webhook 設定（避免休眠後 bot 永久失聯，這是修過的一個重要 bug）。

---

## 9. 之後可能的優化方向（尚未實作）

- 把記憶體資料改成真正的資料庫（餐廳、菜單照片、歷史場次不會因重啟消失）
- 輪替目前已曝光的 Bot Token 與 Gemini API Key，改用環境變數管理
- 如果之後想加回「查看菜單品項明細」「點餐歷史」等功能，需要重新設計（先前的
  Mini App 方案在群組情境下有 Telegram 平台限制，需要用不同的技術路線）
