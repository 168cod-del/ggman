# Telegram 點餐 Bot — 專案說明文件（公開版，已移除機密資訊）

> 這份是可以放心分享給別人看的版本。實際的金鑰、密碼請參考你自己保管的
> 完整版 README，不要把那份外流。

> 目前的架構：開單之後，大家自己在聊天室講要吃什麼、自己貼照片參考，
> bot 完全不管這件事，只負責把「品項 金額 備註」格式的訊息記錄下來，結單時彙整。
> 沒有餐廳管理、沒有菜單照片管理、沒有後台網頁。

---

## 1. 會用到的所有網址

| 用途 | 網址 |
|---|---|
| Bot 服務本體（Render） | https://ggman.onrender.com |
| 保活健康檢查端點 | https://ggman.onrender.com/health |
| GitHub Repo | https://github.com/168cod-del/ggman |
| Render Dashboard（管理部署、環境變數、看 Logs） | https://dashboard.render.com |
| Neon Console（管理資料庫） | https://console.neon.tech |
| UptimeRobot（管理保活監控） | https://uptimerobot.com/dashboard |
| BotFather（Telegram 官方設定機器人） | https://t.me/BotFather |

---

## 2. Neon 資料庫是做什麼用的

**角色：幫忙保存「進行中的點餐場次」，避免 Render 重啟時資料遺失。**

- 平常大家 `/开单`、點餐、刪單，資料本來只存在程式的記憶體裡
- Render 免費方案閒置一段時間會自動休眠、重啟，一重啟記憶體就會清空——
  如果那時候剛好有一場點餐進行到一半，訂單資料會整個消失
- 接上 Neon 後，每次開單／點餐／刪單／結單，都會同步寫一份到資料庫；
  服務重啟時會自動把還在進行中的場次讀回記憶體，接續原本的進度
- 它純粹是「保平安」用的，不參與點餐邏輯本身，沒有它 bot 一樣能正常運作，
  只是重啟時機不巧會遺失當下那場資料

---

## 3. 需要的金鑰與密碼（實際值請另外查看，不寫在這份文件）

| 項目 | 說明 | 在哪裡找 |
|---|---|---|
| Telegram Bot Token | bot 的登入憑證 | 寫在 `main.py` 的 `BOT_TOKEN`，或去 BotFather 查詢 |
| Webhook 密鑰 | 驗證 webhook 推送真的來自 Telegram | 寫在 `main.py` 的 `WEBHOOK_SECRET_TOKEN` |
| DATABASE_URL | Neon 的 PostgreSQL 連線字串 | 設定成 Render 的 Environment Variable；實際值到 Neon Console 查看 |

### ⚠️ 安全性提醒
- 這幾組憑證都具有實際操作權限，請勿公開分享、勿提交到公開的 GitHub repo 裡
- 建議定期輪替 Bot Token（BotFather → `/revoke`）

---

## 4. Render 部署設定

前往：[Render Dashboard](https://dashboard.render.com) → 選 `ggman` 服務

| 設定項目 | 值 |
|---|---|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Runtime | Python 3 |
| Environment Variables | `PYTHON_VERSION` = `3.11.9`<br>`DATABASE_URL` = （見上方，實際值不公開） |
| Instance Type | Free |

**Free 方案的限制**：閒置約 15 分鐘會自動休眠，有新訊息時會透過 webhook 自動喚醒，
但第一則訊息可能延遲 30–60 秒，靠 UptimeRobot 保活可以緩解（見下方）。

---

## 5. UptimeRobot 保活設定

前往：[uptimerobot.com](https://uptimerobot.com/dashboard)

| 設定項目 | 值 |
|---|---|
| Monitor Type | HTTP(s) |
| URL | https://ggman.onrender.com/health |
| Monitoring Interval | 5 分鐘 |

每 5 分鐘自動打一次健康檢查端點，讓 Render 服務盡量保持清醒，降低使用者第一句話要等 30-60 秒的機率。

---

## 6. BotFather 設定狀態

前往：[BotFather](https://t.me/BotFather)

| 設定 | 狀態 |
|---|---|
| Group Privacy | **Disabled**（必須關閉，否則群組裡的點餐文字訊息 bot 收不到；`/mybots` → 選 bot → Bot Settings → Group Privacy） |
| Menu Button | 已經完全用不到了，可以移除掉 |

### 指令選單（「/」圖示點開的那個清單）

打 `/setcommands`，選這個 bot，貼上：

```
start - 開單（發起點餐，第一個打的人是發起人）
delorder - 刪除我的訂單
help - 使用說明
end - 結束點餐（限發起人）
```

---

## 7. 專案檔案結構

```
your-repo/
├── main.py              # 後端主程式（Bot + FastAPI 網站）
└── requirements.txt      # Python 套件清單
```

---

## 8. 使用者操作指令一覽

### 開始點餐
打 `/开单`、`/開單` 或 `/start` 皆可。該聊天室裡**第一個**打這個指令的人，
會被記錄為本場「發起人」。開單後想吃什麼、要參考哪間店，大家自己在聊天室講、
自己貼照片就好，bot 不處理照片，純粹讓大家自己看。

### 點餐（直接在聊天室打字，金額為必填）

| 格式 | 範例 |
|---|---|
| 單品項 | `牛排X2 300 五分熟` |
| 多品項＋各自金額 | `地瓜球 紅茶 30+30 少冰` |
| 多品項（+ / . 、分隔）＋各自金額 | `牛肉麵+豬頭皮+酸菜 30+30+30` |
| 多品項（無分隔符號） | `牛肉麵30豬頭皮30酸菜30` |
| 多品項＋共用總金額 | `牛肉麵 豬頭皮 酸菜 90` |
| 多品項＋共用總金額（＝符號） | `牛肉麵、豬頭皮、酸菜＝90` |
| 多品項＋共用總金額＋備註 | `牛排、雞排 300 加辣` |

品項之間可以用 `+`、`/`、`.`、`、`（頓號）或空白分隔。同一人重複輸入會**覆蓋**前一筆訂單。
沒有金額或看起來不像點餐內容的訊息，一律安靜忽略。

### 刪除自己的訂單
打 `/删`、`/删单`、`/删除`（或繁體 `/刪`、`/刪單`、`/刪除`），或正式指令 `/delorder`，
不需要二次確認，成功回覆「刪單成功」四個字。

### 查看使用說明
打 `/help` 會回覆完整格式列表。

### 結束點餐（限發起人）
打 `/结单`、`/結單`、`/end`，或按「🛑 結束點餐」按鈕。結束後顯示：
每人明細（誰點了什麼、備註、多少錢）、品項彙總（同名品項自動加總）、合計金額。
結束後場次記憶清除，下次要重新 `/开单`。

---

## 9. 重要限制與已知行為

- 場次資料有設定 `DATABASE_URL` 才會持久化；沒設定的話是純記憶體模式，重啟會清空
- Telegram 規定「web_app 型態的按鈕」只能在私訊使用，這支程式完全沒用到，
  「結束點餐」用的是一般 Inline 按鈕（callback_data），不受此限制
- 點餐文字辨識只在「該聊天室有進行中場次」時才會運作，其餘時間 bot 對群組裡
  的任何對話（包含照片）都不會有反應

---

## 10. 之後可能的優化方向（尚未實作）

- 輪替目前已曝光的 Bot Token，改用環境變數管理
- 如果之後又想加回「照片辨識菜單」「餐廳管理」之類的功能，可以參考先前的開發紀錄，
  但目前判斷這類功能實際使用率低、維護成本高，暫不建議恢復
