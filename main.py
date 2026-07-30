"""
Telegram 點餐 Bot 後端（簡化版：純文字點餐，沒有餐廳/菜單管理功能）

這支程式同時做兩件事：
1. Telegram Bot（webhook 模式，適合 Render 免費方案的休眠機制）
2. 一個小型網站伺服器（FastAPI），只提供：
   - /telegram-webhook  Telegram 推送新訊息的接收端點
   - /health            保活用的健康檢查端點

功能總覽：
1. /start（或「/开单」「/開單」）
   - 該聊天室第一個發起的人 = 本場「發起人」
   - 回覆點餐格式說明 +「🛑 結束點餐」按鈕
   - 大家要吃什麼、要看哪間店的菜單，都自己在聊天室講、自己貼照片，
     bot 完全不管這件事，只負責把「品項 金額 備註」這種格式的訊息記下來
2. 大家直接在聊天室打字點餐，支援多種格式（金額必填）：
   單品項：品項X數量 金額 [備註]              例：牛排X2 300 五分熟
   多品項＋各自金額：品項1 品項2 金額1+金額2   例：地瓜球 紅茶 30+30 少冰
                     品項1+品項2+品項3 金額1+金額2+金額3
                     品項1品項1金額品項2金額...（無分隔符號也可以）
   多品項＋共用總金額（金額需放最後）：品項1 品項2 總金額
                     品項1 品項2＝總金額
   （品項之間可以用 + / . 或空白分隔）
   每人每場只會保留「最新一筆」訂單，重複輸入會覆蓋前一筆
3. 想刪除自己這場的訂單，直接打「/删」「/删单」「/删除」（或繁體「/刪」「/刪單」「/刪除」）
   或正式指令 /delorder 皆可，不用二次確認，成功會回覆「刪單成功」
4. 發起人按「🛑 結束點餐」（或打 /end、/结单、/結單）：
   顯示每人明細 + 品項彙總 + 合計金額、清除場次記憶
5. /help：查看使用說明

嚴格的回應規則（很重要）：
- 只有「/start /开单 /開單 /end /结单 /結單 /delorder /help
  /删 /删单 /删除 /刪 /刪單 /刪除」這些指令，bot 才會在任何時候回應。
- 除此之外，只有在該聊天室「有正在進行中的點餐場次」時，
  bot 才會嘗試把文字訊息解析成「品項 金額 備註」的點餐內容；
  解析不出來的一般聊天一律安靜忽略，不回應、不打擾。
- 沒有任何進行中場次時，群組裡的其他任何訊息一律不理會（包含照片——
  使用者自己貼的菜單照片、參考圖等，bot 完全不處理，純粹讓大家自己看）。

注意：
- 場次記憶目前存在「記憶體」裡（如果有設定 DATABASE_URL 則同步存進資料庫，
  重啟後會自動載回）。
- 因為點餐是直接看群組裡的文字訊息辨識，
  bot 必須關閉 Telegram 的隱私模式（BotFather → /setprivacy → Disable），
  否則群組裡的一般文字訊息（包含點餐內容）不會被 bot 收到。
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------
# 專案設定 —— 部署完成後記得回來改這幾個值
# ------------------------------------------------------------------

BOT_TOKEN = "8396988188:AAHnH2wRRu0IpnMB7gicvqwXc6bB8f-axso"

# 部署到 Render 拿到網址後，把這裡換成那個網址（不要加結尾斜線）
BASE_URL = "https://ggman.onrender.com"

# Telegram webhook 用的密鑰，不用跟任何人說
WEBHOOK_SECRET_TOKEN = "3d5503dd54239bc4e701d1645cb9e456"

# 資料庫連線字串——不要寫在這裡！去 Render 的 Environment 頁籤新增一個環境變數
# 名稱叫 DATABASE_URL，值貼上 Neon 給你的連線字串（含密碼）。
# 這裡只是讀取那個環境變數，沒有設定的話會是 None，程式會自動退回記憶體模式運作。
DATABASE_URL = os.environ.get("DATABASE_URL")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 資料儲存
# ------------------------------------------------------------------

# active_sessions: { chat_id: {
#   "initiator_id": int,
#   "initiator_name": str,
#   "orders_by_user": { user_id: {
#        "user_name": str, "items": [{"name","qty","price"}],
#        "note": str, "total": number, "raw_text": str,
#   } },
# } }
active_sessions: dict[int, dict] = {}

END_ORDER_CALLBACK = "end_order"


# ------------------------------------------------------------------
# 資料庫（可選）：有設定 DATABASE_URL 才會啟用，讓場次資料在重啟後不會消失
# 沒設定的話下面每個函式都會安靜跳過，跟之前一樣純記憶體運作
# ------------------------------------------------------------------
db_pool: asyncpg.Pool | None = None


async def db_init():
    global db_pool
    if not DATABASE_URL:
        logger.warning("沒有設定 DATABASE_URL，資料只存在記憶體裡，重啟會清空。")
        return

    db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    await db_pool.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            chat_id BIGINT PRIMARY KEY,
            initiator_id BIGINT NOT NULL,
            initiator_name TEXT NOT NULL,
            orders_by_user JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )

    session_rows = await db_pool.fetch(
        "SELECT chat_id, initiator_id, initiator_name, orders_by_user FROM sessions"
    )
    for row in session_rows:
        raw_orders = row["orders_by_user"]
        orders_by_user = json.loads(raw_orders) if isinstance(raw_orders, str) else (raw_orders or {})
        active_sessions[row["chat_id"]] = {
            "initiator_id": row["initiator_id"],
            "initiator_name": row["initiator_name"],
            "orders_by_user": {int(k): v for k, v in orders_by_user.items()},
        }

    logger.info("資料庫連線成功，已載入 %d 個進行中場次", len(active_sessions))


async def db_close():
    if db_pool:
        await db_pool.close()


async def db_upsert_session(chat_id: int, session: dict):
    if not db_pool:
        return
    try:
        await db_pool.execute(
            """
            INSERT INTO sessions (chat_id, initiator_id, initiator_name, orders_by_user)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (chat_id) DO UPDATE
            SET initiator_id = $2, initiator_name = $3, orders_by_user = $4::jsonb
            """,
            chat_id,
            session["initiator_id"],
            session["initiator_name"],
            json.dumps(session["orders_by_user"]),
        )
    except Exception:
        logger.exception("寫入場次資料到資料庫失敗")


async def db_delete_session(chat_id: int):
    if not db_pool:
        return
    try:
        await db_pool.execute("DELETE FROM sessions WHERE chat_id = $1", chat_id)
    except Exception:
        logger.exception("從資料庫刪除場次失敗")


# ------------------------------------------------------------------
# 文字點餐解析
# ------------------------------------------------------------------
# 品項清單常見的分隔符號：加號、斜線、句點、空白（空白已經由 text.split() 處理）
_ITEM_SEP_RE = re.compile(r"[+/.]")


def _expand_plus(tokens: list[str]) -> list[str]:
    """把像「牛肉麵+豬頭皮/酸菜.貢丸湯」這種用 + / . 黏在一起的詞，拆成多個獨立品項。"""
    out = []
    for t in tokens:
        if _ITEM_SEP_RE.search(t):
            out.extend(p for p in _ITEM_SEP_RE.split(t) if p)
        else:
            out.append(t)
    return out


def _build_items(name_tokens: list[str], prices: list) -> list[dict]:
    if len(name_tokens) == 1 and len(prices) == 1:
        m = re.match(r"^(.+?)[xX](\d+)$", name_tokens[0])
        if m:
            return [{"name": m.group(1), "qty": int(m.group(2)), "price": prices[0]}]
        return [{"name": name_tokens[0], "qty": 1, "price": prices[0]}]
    return [{"name": nm, "qty": 1, "price": p} for nm, p in zip(name_tokens, prices)]


def parse_order_text(text: str):
    """
    嘗試把一則文字訊息解析成點餐內容。金額是必填的——完全沒有數字的訊息
    一律當成一般聊天安靜忽略，這樣才能在開單期間也不會誤觸發、洗版。

    支援的格式（金額可放最前面或最後面）：
      單品項：品項[X數量] 金額 [備註]        例：牛排X2 300 五分熟
      多品項＋各自金額：
        品項1 品項2 金額1+金額2 [備註]        例：地瓜球 紅茶 30+30 少冰
        品項1+品項2 金額1+金額2               例：牛肉麵+豬頭皮+酸菜 30+30+30
        品項1品項1金額品項2金額...（無分隔）   例：牛肉麵30豬頭皮30酸菜30
      多品項＋共用一個總金額（金額須放最後面）：
        品項1 品項2 總金額                    例：牛肉麵 豬頭皮 酸菜 90
        品項1+品項2 總金額                    例：牛肉麵+豬頭皮+酸菜 90
        品項1 品項2＝總金額 或 品項1 品項2=總金額  例：牛肉麵 豬頭皮 酸菜＝90

    回傳：
      None       -> 完全沒有數字，不像點餐內容，呼叫端應安靜忽略
      "MISMATCH" -> 看起來像點餐但格式兜不起來，呼叫端應提示格式錯誤
      (items, note, total_override) -> 解析成功
        total_override 是 None 時，總金額 = 各品項金額加總；
        不是 None 時（多品項共用總金額的情況），總金額直接用這個數字，
        此時各品項的 price 都會是 0（只是為了記錄品項名稱用）
    """
    text = text.strip()
    if not text:
        return None

    # ---- 「品項＝總金額」或「品項=總金額」----
    for eq in ("＝", "="):
        if eq in text:
            left, _, right = text.partition(eq)
            left, right = left.strip(), right.strip()
            if not left or not re.fullmatch(r"\d+(\.\d+)?", right):
                return "MISMATCH"
            names = [n for n in re.split(r"[+/.\s]+", left) if n]
            if not names:
                return "MISMATCH"
            total = float(right) if "." in right else int(right)
            items = [{"name": n, "qty": 1, "price": 0} for n in names]
            return (items, "", total)

    if not re.search(r"\d", text):
        return None  # 完全沒有數字，不像點餐內容，安靜忽略

    tokens = text.split()

    price_idx = None
    for i, tok in enumerate(tokens):
        if re.fullmatch(r"\d+(\.\d+)?(\+\d+(\.\d+)?)*", tok):
            price_idx = i
            break

    if price_idx is None:
        # 數字沒有被空白獨立出來，嘗試辨識「品項數字品項數字...」黏在一起的寫法
        pairs = list(re.finditer(r"([^\d]+?)(\d+(?:\.\d+)?)", text))
        if pairs and pairs[0].start() == 0:
            remainder = text[pairs[-1].end():].strip()
            if not remainder or not re.search(r"\d", remainder):
                items = [
                    {
                        "name": m.group(1).strip(),
                        "qty": 1,
                        "price": float(m.group(2)) if "." in m.group(2) else int(m.group(2)),
                    }
                    for m in pairs if m.group(1).strip()
                ]
                if items:
                    return (items, remainder, None)
        return "MISMATCH"

    before = tokens[:price_idx]
    price_tok = tokens[price_idx]
    after = tokens[price_idx + 1:]

    prices = [float(p) if "." in p else int(p) for p in price_tok.split("+")]
    n = len(prices)

    expanded_before = _expand_plus(before)
    expanded_after = _expand_plus(after)

    if len(expanded_before) == n:
        return (_build_items(expanded_before, prices), " ".join(after), None)

    if len(expanded_after) == n:
        return (_build_items(expanded_after, prices), " ".join(before), None)

    # 只有在金額是整句最後一個詞、且前面有品項時，才當作「多品項共用一個總金額」
    if n == 1 and not after:
        combined = [t for t in expanded_before if t]
        if combined:
            items = [{"name": t, "qty": 1, "price": 0} for t in combined]
            return (items, "", prices[0])

    return "MISMATCH"


async def record_order(
    chat_id: int,
    user_id: int,
    user_name: str,
    items: list[dict],
    note: str,
    raw_text: str,
    total_override=None,
):
    """把解析好的訂單記錄進場次（同一人重複點餐會覆蓋前一筆）。
    回傳要貼回聊天室的單行摘要文字；若場次不存在，回傳 None。"""
    session = active_sessions.get(chat_id)
    if not session:
        return None

    total = total_override if total_override is not None else sum(item["price"] for item in items)

    order_record = {
        "user_name": user_name,
        "items": items,
        "note": note,
        "total": total,
        "raw_text": raw_text,
    }
    session["orders_by_user"][user_id] = order_record
    await db_upsert_session(chat_id, session)

    return build_order_line(user_name, order_record)


def build_order_line(user_name: str, order_record: dict) -> str:
    item_strs = [f"{it['name']}x{it['qty']}" for it in order_record["items"]]
    note_part = f"（{order_record['note']}）" if order_record.get("note") else ""
    total = order_record["total"]
    price_part = f" 💰NT${total}" if total > 0 else ""
    return f"👤{user_name} 🍽️{'、'.join(item_strs)}{note_part}{price_part}"


def build_final_summary(session: dict) -> str:
    orders = list(session["orders_by_user"].items())

    if not orders:
        detail_lines = "（沒有人點餐）"
        agg_lines = "（無）"
        grand_total = 0
    else:
        detail_lines_list = []
        items_agg: dict[str, int] = {}
        grand_total = 0
        for _user_id, record in orders:
            detail_lines_list.append(build_order_line(record["user_name"], record))
            grand_total += record["total"]
            for it in record["items"]:
                items_agg[it["name"]] = items_agg.get(it["name"], 0) + it["qty"]
        detail_lines = "\n".join(detail_lines_list)
        agg_lines = "\n".join(f"・{name} x{qty}" for name, qty in items_agg.items())

    total_line = f"合計 NT${grand_total}" if grand_total > 0 else "（沒有輸入金額，請自行加總）"

    return (
        f"✅ 點餐結束\n"
        f"共 {len(orders)} 筆訂單，{total_line}\n\n"
        f"【每人明細】\n{detail_lines}\n\n"
        f"【品項彙總】\n{agg_lines}"
    )


# ------------------------------------------------------------------
# /start
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id not in active_sessions:
        active_sessions[chat_id] = {
            "initiator_id": user.id,
            "initiator_name": user.full_name,
            "orders_by_user": {},
        }
        await db_upsert_session(chat_id, active_sessions[chat_id])

    await update.message.reply_text(
        "品項 金額 備註（例：牛排X2 300 五分熟）\n"
        "/删 /删单 /删除 可刪除自己的訂單",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🛑 結束點餐（限發起人）", callback_data=END_ORDER_CALLBACK)]]
        ),
    )


async def delete_my_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    session = active_sessions.get(chat_id)
    if session and session["orders_by_user"].pop(user.id, None) is not None:
        await db_upsert_session(chat_id, session)
        await update.message.reply_text("刪單成功")


HELP_TEXT = (
    "開單：/start 或 /开单／開單\n"
    "點餐：直接打「品項 金額 備註」，例如 牛排X2 300 五分熟\n"
    "刪單：/delorder 或 /删／删单／删除\n"
    "結單：/end 或 /结单／結單（限發起人）"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


# ------------------------------------------------------------------
# 文字點餐（一般文字訊息）
# ------------------------------------------------------------------
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    stripped = text.strip()
    chat_id = update.effective_chat.id
    user = update.effective_user
    is_private = update.effective_chat.type == "private"

    # 中文替代指令：不用打 /start / /end，直接打這幾個也可以（繁簡都支援）
    if stripped in ("/开单", "/開單"):
        await start(update, context)
        return
    if stripped in ("/结单", "/結單"):
        await end_session(update, context)
        return

    # 刪除自己在本場的訂單，不用二次確認（繁簡都支援）
    if stripped in ("/删", "/删单", "/删除", "/刪", "/刪單", "/刪除"):
        await delete_my_order(update, context)
        return

    session = active_sessions.get(chat_id)
    if not session:
        if is_private:
            await update.message.reply_text("目前沒有進行中的點餐，請先 /start（或直接打「/开单」「/開單」）發起。")
        return  # 群組裡安靜忽略，避免洗版

    parsed = parse_order_text(text)
    if parsed is None:
        return  # 不像點餐內容，當一般聊天，安靜忽略
    if parsed == "MISMATCH":
        await update.message.reply_text("⚠️ 格式錯誤，請重新輸入（需包含品項與金額）")
        return

    items, note, total_override = parsed
    summary = await record_order(chat_id, user.id, user.full_name, items, note, text, total_override)
    if summary:
        await update.message.reply_text(summary)


# ------------------------------------------------------------------
# 結束點餐
# ------------------------------------------------------------------
async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id not in active_sessions:
        await update.message.reply_text("目前沒有進行中的點餐。")
        return

    session = active_sessions[chat_id]
    if user.id != session["initiator_id"]:
        await update.message.reply_text(f"⚠️ 只有發起人「{session['initiator_name']}」可以結束這場點餐。")
        return

    final_text = build_final_summary(session)
    del active_sessions[chat_id]
    await db_delete_session(chat_id)
    await update.message.reply_text(final_text)


async def end_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat.id
    user = query.from_user

    if chat_id not in active_sessions:
        await query.answer("目前沒有進行中的點餐。", show_alert=True)
        return

    session = active_sessions[chat_id]
    if user.id != session["initiator_id"]:
        await query.answer("你不是開單發起人", show_alert=True)
        return

    await query.answer()
    final_text = build_final_summary(session)
    del active_sessions[chat_id]
    await db_delete_session(chat_id)

    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(chat_id=chat_id, text=final_text)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # 任何一個訊息處理失敗，都只記錄下來，絕對不能讓整個服務跟著崩潰
    # （崩潰會導致 webhook 被意外取消設定，之後所有訊息都收不到）
    logger.exception("處理更新時發生未預期的錯誤", exc_info=context.error)


# ------------------------------------------------------------------
# FastAPI app + Telegram webhook
# ------------------------------------------------------------------
telegram_app: Application | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app

    await db_init()

    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("end", end_session))
    telegram_app.add_handler(CommandHandler("delorder", delete_my_order))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(
        CallbackQueryHandler(end_button_callback, pattern=f"^{END_ORDER_CALLBACK}$")
    )
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    telegram_app.add_error_handler(global_error_handler)

    await telegram_app.initialize()

    await telegram_app.bot.set_webhook(
        url=f"{BASE_URL}/telegram-webhook",
        secret_token=WEBHOOK_SECRET_TOKEN,
        allowed_updates=Update.ALL_TYPES,
    )
    await telegram_app.start()
    logger.info("Bot 啟動中（webhook 模式）...")

    yield

    # 注意：這裡故意「不」呼叫 bot.delete_webhook()。
    # Render 免費方案閒置一段時間會自動暫停服務，暫停時一樣會觸發這段關閉流程；
    # 如果這裡把 webhook 刪掉，Telegram 手上就沒有網址了，
    # 之後「有新訊息時自動把休眠服務叫醒」這個機制會整個失效，
    # 只能手動重新部署才能恢復——這正是之前反覆斷線的根本原因。
    await telegram_app.stop()
    await telegram_app.shutdown()
    await db_close()


app = FastAPI(lifespan=lifespan)


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET_TOKEN:
        raise HTTPException(403, "invalid secret token")

    data = await request.json()
    update = Update.de_json(data=data, bot=telegram_app.bot)

    try:
        await telegram_app.process_update(update)
    except Exception:
        # 保底防護：就算 process_update 本身出了意料之外的錯誤，
        # 這個請求還是要正常回 200，絕對不能讓例外往上傳、拖垮整個服務
        logger.exception("webhook 處理更新時發生錯誤")

    return {"ok": True}


@app.get("/health")
async def health():
    # 順便檢查 webhook 有沒有還在，萬一之前某次意外被取消設定，
    # 這裡會自動補回去（搭配 UptimeRobot 之類的保活服務定期呼叫這個端點）
    try:
        info = await telegram_app.bot.get_webhook_info()
        if not info.url:
            logger.warning("偵測到 webhook 網址是空的，自動重新設定")
            await telegram_app.bot.set_webhook(
                url=f"{BASE_URL}/telegram-webhook",
                secret_token=WEBHOOK_SECRET_TOKEN,
                allowed_updates=Update.ALL_TYPES,
            )
    except Exception:
        logger.exception("健康檢查時嘗試修復 webhook 失敗")

    return {"status": "ok"}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
