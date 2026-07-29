"""
Telegram 點餐 Bot 後端（文字點餐版）

這支程式同時做兩件事：
1. Telegram Bot（webhook 模式，適合 Render 免費方案的休眠機制）
2. 一個小型網站伺服器（FastAPI），提供：
   - /select-restaurant 選餐廳頁面（Mini App，限發起人）
   - /history           「我的點餐」頁面（Mini App，本場訂單修改/刪除 + 常用紀錄快速重送）
   - /admin             後台管理頁面（手動新增餐廳、編輯菜單）
   - /telegram-webhook  Telegram 推送新訊息的接收端點
   - /health            保活用的健康檢查端點
   - /api/...           給以上頁面呼叫的資料介面

功能總覽：
1. /start（或「/开单」「/開單」）
   - 該聊天室第一個發起的人 = 本場「發起人」
   - 顯示使用說明 + 三顆 Inline 按鈕：選擇餐廳／我的點餐／結束點餐
2. 發起人用「🏠 選擇餐廳」選好餐廳（滾輪式選單），選定後會把該餐廳的
   菜單照片直接發到聊天室，讓大家對照著點餐
3. 大家直接在聊天室打字點餐，支援兩種格式：
   單品項：品項X數量 金額 [備註]        例：牛排X2 300 五分熟
   多品項：品項1 品項2 ... 金額1+金額2...[備註]   例：地瓜球 紅茶 30+30 少冰
   （金額視為該品項這一行的總金額，不會再乘以數量）
   每人每場只會保留「最新一筆」訂單，重複輸入會覆蓋前一筆
4. 使用者可以打開「📋 我的點餐」Mini App，修改/刪除本場自己的訂單，
   或從最近 4 筆常用紀錄快速重新送出
5. 發起人按「🛑 結束點餐」：顯示每人明細 + 品項彙總、清除場次記憶
6. /newmenu 餐廳名稱（或「/新增菜單 餐廳名稱」「/新增菜单 餐厅名称」）
   → bot 會請「發出這個指令的人」接著上傳一張菜單照片，
   上傳成功才算真的新增完成（不需要再打任何品項文字）
7. /admin 後台：查看目前有哪些餐廳、有沒有菜單照片、可以刪除餐廳

嚴格的回應規則（很重要）：
- 只有「/start /开单 /開單 /end /admin /newmenu /新增菜單 /新增菜单」這些指令，
  以及「剛打完新增菜單指令的那個人所上傳的下一張照片」，bot 才會在任何時候回應。
- 除此之外，只有在該聊天室「有正在進行中的點餐場次，且已選好餐廳」時，
  bot 才會嘗試把文字訊息解析成「品項 金額 備註」的點餐內容；
  解析不出來的一般聊天一律安靜忽略，不回應、不打擾。
- 沒有任何進行中場次時，群組裡的其他任何訊息（包含沒有配對到的照片）一律不理會。

注意：
- 場次記憶、餐廳/菜單資料、使用者個人歷史、新增菜單的暫存狀態，
  目前都存在「記憶體」裡，這支程式一重啟（Render 重新部署）就會清空。
  等之後要正式上線、資料不能不見的話，要幫你接上真正的資料庫。
- 因為點餐是直接看群組裡的文字訊息辨識，
  bot 必須關閉 Telegram 的隱私模式（BotFather → /setprivacy → Disable），
  否則群組裡的一般文字訊息（包含點餐內容）不會被 bot 收到。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
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

# 後台管理密碼，建議自己改掉
ADMIN_PASSWORD = "a123456"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR  # html 檔案跟 main.py 放同一層


# ------------------------------------------------------------------
# 資料儲存（記憶體版本）
# ------------------------------------------------------------------

# restaurants: { 餐廳名稱: {"photo_file_id": str | None} }
restaurants: dict[str, dict] = {
    "阿明快炒": {"photo_file_id": None},
    "巷口便當": {"photo_file_id": None},
    "涼夏冷飲": {"photo_file_id": None},
    "深夜燒烤": {"photo_file_id": None},
    "晨光早餐店": {"photo_file_id": None},
}
# 這 5 間是測試用的預設餐廳，還沒有菜單照片；
# 想要有照片可以直接用 /新增菜單 重新加一次（同名會覆蓋）

# pending_menu_uploads: { user_id: 餐廳名稱 }
# 記錄「剛打完新增菜單指令、正在等待上傳菜單照片」的人
pending_menu_uploads: dict[int, str] = {}

# active_sessions: { chat_id: {
#   "initiator_id": int,
#   "initiator_name": str,
#   "restaurant": str | None,
#   "orders_by_user": { user_id: {
#        "user_name": str, "items": [{"name","qty","price"}],
#        "note": str, "total": number, "raw_text": str,
#   } },
# } }
active_sessions: dict[int, dict] = {}

# user_history: { user_id: [ {"raw_text","items","note","total"}, ... ] }（最多留 4 筆，最新在前）
user_history: dict[int, list[dict]] = {}

END_ORDER_CALLBACK = "end_order"


# ------------------------------------------------------------------
# 文字點餐解析
# ------------------------------------------------------------------
def parse_order_text(text: str):
    """
    嘗試把一則文字訊息解析成點餐內容。

    支援：
      單品項：品項[X數量] 金額 [備註]           例：牛排X2 300 五分熟
      多品項：品項1 品項2 ... 金額1+金額2...[備註]  例：地瓜球 紅茶 30+30 少冰

    回傳：
      None       -> 完全不像點餐內容（沒有數字），呼叫端應安靜忽略
      "MISMATCH" -> 看起來像是要點餐但格式兜不起來，呼叫端應提示格式
      (items, note) -> 解析成功
    """
    text = text.strip()
    if not text:
        return None

    tokens = text.split()

    price_idx = None
    for i, tok in enumerate(tokens):
        if re.fullmatch(r"\d+(\.\d+)?(\+\d+(\.\d+)?)*", tok):
            price_idx = i
            break

    if price_idx is None:
        return None

    name_tokens = tokens[:price_idx]
    price_tok = tokens[price_idx]
    note = " ".join(tokens[price_idx + 1:])

    if not name_tokens:
        return "MISMATCH"

    prices = [float(p) if "." in p else int(p) for p in price_tok.split("+")]

    if len(name_tokens) == 1 and len(prices) == 1:
        m = re.match(r"^(.+?)[xX](\d+)$", name_tokens[0])
        if m:
            name, qty = m.group(1), int(m.group(2))
        else:
            name, qty = name_tokens[0], 1
        return ([{"name": name, "qty": qty, "price": prices[0]}], note)

    if len(name_tokens) == len(prices):
        items = [{"name": n, "qty": 1, "price": p} for n, p in zip(name_tokens, prices)]
        return (items, note)

    return "MISMATCH"


def record_order(chat_id: int, user_id: int, user_name: str, items: list[dict], note: str, raw_text: str):
    """把解析好的訂單記錄進場次（同一人重複點餐會覆蓋前一筆），並存進個人歷史。
    回傳要貼回聊天室的單行摘要文字；若場次不存在或餐廳未選，回傳 None。"""
    session = active_sessions.get(chat_id)
    if not session or not session.get("restaurant"):
        return None

    total = sum(item["price"] for item in items)

    order_record = {
        "user_name": user_name,
        "items": items,
        "note": note,
        "total": total,
        "raw_text": raw_text,
    }
    session["orders_by_user"][user_id] = order_record

    history = [h for h in user_history.get(user_id, []) if h["raw_text"] != raw_text]
    history.insert(0, {"raw_text": raw_text, "items": items, "note": note, "total": total})
    user_history[user_id] = history[:4]

    return build_order_line(user_name, order_record)


def build_order_line(user_name: str, order_record: dict) -> str:
    item_strs = [f"{it['name']}x{it['qty']}" for it in order_record["items"]]
    note_part = f"（{order_record['note']}）" if order_record.get("note") else ""
    return f"👤{user_name} 🍽️{'、'.join(item_strs)}{note_part} 💰NT${order_record['total']}"


def build_final_summary(session: dict) -> str:
    restaurant = session.get("restaurant") or "（未選擇）"
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

    return (
        f"✅ 點餐結束\n"
        f"🏠 餐廳：{restaurant}\n"
        f"共 {len(orders)} 筆訂單，合計 NT${grand_total}\n\n"
        f"【每人明細】\n{detail_lines}\n\n"
        f"【品項彙總】\n{agg_lines}"
    )


# ------------------------------------------------------------------
# Telegram initData 驗證
# ------------------------------------------------------------------
def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400):
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > max_age_seconds:
        return None

    user_json = parsed.get("user")
    user = json.loads(user_json) if user_json else None
    return {"user": user, "auth_date": auth_date}


def require_telegram_user(x_telegram_init_data: str) -> dict:
    validated = validate_init_data(x_telegram_init_data, BOT_TOKEN)
    if not validated or not validated.get("user"):
        raise HTTPException(401, "無法驗證身份，請透過 Telegram 開啟此頁面")
    return validated["user"]


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
            "restaurant": None,
            "orders_by_user": {},
        }
        initiator_name = user.full_name
    else:
        initiator_name = active_sessions[chat_id]["initiator_name"]

    select_url = f"{BASE_URL}/select-restaurant?chat_id={chat_id}"
    history_url = f"{BASE_URL}/history?chat_id={chat_id}"

    inline_markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏠 選擇餐廳（限發起人）", web_app=WebAppInfo(url=select_url))],
            [InlineKeyboardButton("📋 我的點餐", web_app=WebAppInfo(url=history_url))],
            [InlineKeyboardButton("🛑 結束點餐（限發起人）", callback_data=END_ORDER_CALLBACK)],
        ]
    )

    await update.message.reply_text(
        f"🍽️ 點餐開始！（本場發起人：{initiator_name}）\n\n"
        "發起人請先按「🏠 選擇餐廳」。\n"
        "選好之後，大家直接在這裡打字點餐即可，格式：\n"
        "・單品項：品項X數量 金額 備註\n"
        "　例：牛排X2 300 五分熟\n"
        "・多品項：品項1 品項2 金額1+金額2 備註\n"
        "　例：地瓜球 紅茶 30+30 少冰\n"
        "（同一人重複輸入會覆蓋前一筆訂單）\n\n"
        "小提醒：之後也可以直接打「/开单」或「/開單」代替 /start，\n"
        "貼菜單也可以直接打「/新增菜單 餐廳名稱」或「/新增菜单 餐厅名称」代替 /newmenu。",
        reply_markup=inline_markup,
    )


# ------------------------------------------------------------------
# 文字點餐（一般文字訊息）
# ------------------------------------------------------------------
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    stripped = text.strip()

    # 中文替代指令：不用打 /start / /newmenu，直接打這兩個也可以（繁簡都支援）
    if stripped in ("/开单", "/開單"):
        await start(update, context)
        return
    if stripped.startswith("/新增菜单") or stripped.startswith("/新增菜單"):
        await new_menu(update, context)
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    is_private = update.effective_chat.type == "private"

    session = active_sessions.get(chat_id)
    if not session or not session.get("restaurant"):
        if is_private:
            await update.message.reply_text("目前沒有進行中的點餐，請先 /start（或直接打「/开单」「/開單」）發起。")
        return  # 群組裡安靜忽略，避免洗版

    parsed = parse_order_text(text)
    if parsed is None:
        return  # 不像點餐內容，當一般聊天，安靜忽略
    if parsed == "MISMATCH":
        await update.message.reply_text(
            "⚠️ 品項數量跟金額數量對不起來，請確認格式：\n"
            "單品項：品項X數量 金額 備註\n"
            "多品項：品項1 品項2 金額1+金額2 備註"
        )
        return

    items, note = parsed
    summary = record_order(chat_id, user.id, user.full_name, items, note, text)
    if summary:
        await update.message.reply_text(summary)


# ------------------------------------------------------------------
# /newmenu 貼上菜單
# ------------------------------------------------------------------
async def new_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    first_line = re.sub(r"^/newmenu(@\w+)?\s*", "", text)
    first_line = re.sub(r"^/新增菜單\s*", "", first_line)
    first_line = re.sub(r"^/新增菜单\s*", "", first_line).strip()

    if not first_line:
        await update.message.reply_text(
            "請用這個格式：\n/newmenu 餐廳名稱\n\n"
            "（也可以直接打「/新增菜單 餐廳名稱」或「/新增菜单 餐厅名称」代替 /newmenu，"
            "不用加任何品項文字）"
        )
        return

    restaurant_name = first_line
    pending_menu_uploads[update.effective_user.id] = restaurant_name

    await update.message.reply_text(
        f"📷 請直接上傳「{restaurant_name}」的菜單照片（傳一張圖片過來即可），"
        "上傳成功後才算真的新增完成。"
    )


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    restaurant_name = pending_menu_uploads.get(user_id)
    if not restaurant_name:
        return  # 沒有人在等待上傳菜單照片，完全不理會，避免亂反應

    photo = update.effective_message.photo[-1]  # 取最大尺寸
    restaurants[restaurant_name] = {"photo_file_id": photo.file_id}
    del pending_menu_uploads[user_id]

    await update.message.reply_text(f"✅ 已新增「{restaurant_name}」，菜單照片已儲存。")


async def admin_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"🔑 後台管理連結：\n{BASE_URL}/admin")


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

    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(chat_id=chat_id, text=final_text)


# ------------------------------------------------------------------
# FastAPI app + Telegram webhook
# ------------------------------------------------------------------
telegram_app: Application | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("end", end_session))
    telegram_app.add_handler(CommandHandler("admin", admin_link))
    telegram_app.add_handler(CommandHandler("newmenu", new_menu))
    telegram_app.add_handler(
        CallbackQueryHandler(end_button_callback, pattern=f"^{END_ORDER_CALLBACK}$")
    )
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))

    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(
        url=f"{BASE_URL}/telegram-webhook",
        secret_token=WEBHOOK_SECRET_TOKEN,
        allowed_updates=Update.ALL_TYPES,
    )
    await telegram_app.start()
    logger.info("Bot 啟動中（webhook 模式）...")

    yield

    await telegram_app.bot.delete_webhook()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET_TOKEN:
        raise HTTPException(403, "invalid secret token")
    data = await request.json()
    update = Update.de_json(data=data, bot=telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------- 頁面 ----------------

@app.get("/select-restaurant")
async def select_restaurant_page():
    return FileResponse(STATIC_DIR / "select_restaurant.html")


@app.get("/history")
async def history_page():
    return FileResponse(STATIC_DIR / "history.html")


@app.get("/admin")
async def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


# ---------------- 公開讀取 API ----------------

@app.get("/api/restaurants")
async def api_list_restaurants():
    return {"restaurants": list(restaurants.keys())}


@app.get("/api/menu/{restaurant}")
async def api_get_menu(restaurant: str):
    if restaurant not in restaurants:
        raise HTTPException(404, "餐廳不存在")
    return {"restaurant": restaurant, "has_photo": bool(restaurants[restaurant].get("photo_file_id"))}


@app.get("/api/session/{chat_id}")
async def api_get_session(chat_id: int):
    session = active_sessions.get(chat_id)
    if not session:
        return {"active": False}
    return {"active": True, "restaurant": session["restaurant"]}


@app.get("/api/session/{chat_id}/is-initiator")
async def api_is_initiator(chat_id: int, x_telegram_init_data: str = Header(default="")):
    tg_user = require_telegram_user(x_telegram_init_data)
    session = active_sessions.get(chat_id)
    if not session:
        return {"active": False, "is_initiator": False}
    return {
        "active": True,
        "is_initiator": tg_user["id"] == session["initiator_id"],
        "restaurant": session["restaurant"],
    }


class SelectRestaurantBody(BaseModel):
    restaurant: str


@app.post("/api/session/{chat_id}/restaurant")
async def api_select_restaurant(
    chat_id: int, body: SelectRestaurantBody, x_telegram_init_data: str = Header(default="")
):
    tg_user = require_telegram_user(x_telegram_init_data)
    session = active_sessions.get(chat_id)
    if not session:
        raise HTTPException(404, "找不到進行中的點餐場次")
    if tg_user["id"] != session["initiator_id"]:
        raise HTTPException(403, "只有發起人可以選擇餐廳")
    if body.restaurant not in restaurants:
        raise HTTPException(400, "餐廳不存在")

    session["restaurant"] = body.restaurant

    photo_file_id = restaurants[body.restaurant].get("photo_file_id")
    if telegram_app:
        if photo_file_id:
            await telegram_app.bot.send_photo(
                chat_id=chat_id,
                photo=photo_file_id,
                caption=f"🍽️ {body.restaurant} 菜單，請大家對照著點餐",
            )
        else:
            await telegram_app.bot.send_message(
                chat_id=chat_id,
                text=f"🏠 已選擇「{body.restaurant}」（這間餐廳目前還沒有菜單照片）",
            )

    return {"ok": True}


# ---------------- 我的點餐 API ----------------

@app.get("/api/session/{chat_id}/my-order")
async def api_get_my_order(chat_id: int, x_telegram_init_data: str = Header(default="")):
    tg_user = require_telegram_user(x_telegram_init_data)
    session = active_sessions.get(chat_id)
    if not session:
        return {"active": False, "order": None}
    record = session["orders_by_user"].get(tg_user["id"])
    return {"active": True, "order": record}


class UpsertOrderBody(BaseModel):
    raw_text: str


@app.post("/api/session/{chat_id}/my-order")
async def api_upsert_my_order(
    chat_id: int, body: UpsertOrderBody, x_telegram_init_data: str = Header(default="")
):
    tg_user = require_telegram_user(x_telegram_init_data)
    session = active_sessions.get(chat_id)
    if not session or not session.get("restaurant"):
        raise HTTPException(404, "目前沒有進行中的點餐，或發起人還沒選餐廳")

    parsed = parse_order_text(body.raw_text)
    if parsed is None or parsed == "MISMATCH":
        raise HTTPException(400, "格式無法解析，請確認「品項 金額 備註」的格式")

    items, note = parsed
    user_name = tg_user.get("first_name", "") + (
        f" {tg_user['last_name']}" if tg_user.get("last_name") else ""
    )
    summary = record_order(chat_id, tg_user["id"], user_name.strip() or "使用者", items, note, body.raw_text)

    if summary and telegram_app:
        await telegram_app.bot.send_message(chat_id=chat_id, text=f"✏️ {summary}")

    return {"ok": True}


@app.post("/api/session/{chat_id}/my-order/delete")
async def api_delete_my_order(chat_id: int, x_telegram_init_data: str = Header(default="")):
    tg_user = require_telegram_user(x_telegram_init_data)
    session = active_sessions.get(chat_id)
    if not session:
        raise HTTPException(404, "目前沒有進行中的點餐")

    record = session["orders_by_user"].pop(tg_user["id"], None)
    if record and telegram_app:
        await telegram_app.bot.send_message(
            chat_id=chat_id, text=f"❌ {record['user_name']} 已取消訂單"
        )
    return {"ok": True}


@app.get("/api/my-history")
async def api_get_my_history(x_telegram_init_data: str = Header(default="")):
    tg_user = require_telegram_user(x_telegram_init_data)
    return {"history": user_history.get(tg_user["id"], [])}


class ReorderBody(BaseModel):
    raw_text: str


@app.post("/api/session/{chat_id}/order-from-history")
async def api_order_from_history(
    chat_id: int, body: ReorderBody, x_telegram_init_data: str = Header(default="")
):
    tg_user = require_telegram_user(x_telegram_init_data)
    session = active_sessions.get(chat_id)
    if not session or not session.get("restaurant"):
        raise HTTPException(404, "目前沒有進行中的點餐，或發起人還沒選餐廳")

    parsed = parse_order_text(body.raw_text)
    if parsed is None or parsed == "MISMATCH":
        raise HTTPException(400, "這筆歷史紀錄格式異常，無法重新送出")

    items, note = parsed
    user_name = tg_user.get("first_name", "") + (
        f" {tg_user['last_name']}" if tg_user.get("last_name") else ""
    )
    summary = record_order(chat_id, tg_user["id"], user_name.strip() or "使用者", items, note, body.raw_text)

    if summary and telegram_app:
        await telegram_app.bot.send_message(chat_id=chat_id, text=summary)

    return {"ok": True}


# ---------------- 後台管理 API（需要密碼） ----------------

def check_admin_password(x_admin_password: str = Header(default="")):
    if x_admin_password != ADMIN_PASSWORD:
        raise HTTPException(401, "密碼錯誤")


class NewRestaurantBody(BaseModel):
    name: str


@app.post("/api/admin/restaurants")
async def api_add_restaurant(body: NewRestaurantBody, x_admin_password: str = Header(default="")):
    check_admin_password(x_admin_password)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "餐廳名稱不能空白")
    if name in restaurants:
        raise HTTPException(400, "這間餐廳已經存在")
    restaurants[name] = {"photo_file_id": None}
    return {"ok": True}


@app.get("/api/admin/restaurants")
async def api_admin_list_restaurants(x_admin_password: str = Header(default="")):
    check_admin_password(x_admin_password)
    return {
        "restaurants": [
            {"name": name, "has_photo": bool(data.get("photo_file_id"))}
            for name, data in restaurants.items()
        ]
    }


class DeleteRestaurantBody(BaseModel):
    name: str


@app.post("/api/admin/restaurants/delete")
async def api_delete_restaurant(body: DeleteRestaurantBody, x_admin_password: str = Header(default="")):
    check_admin_password(x_admin_password)
    if body.name not in restaurants:
        raise HTTPException(404, "餐廳不存在")
    del restaurants[body.name]
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
