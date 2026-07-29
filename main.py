"""
Telegram 點餐 Bot 後端（含多餐廳菜單管理 + 網頁 API）

這支程式同時做兩件事：
1. Telegram Bot（用 python-telegram-bot v20+，webhook 模式 —— 由 Telegram 主動推送新訊息過來，
   而不是 bot 自己一直去問，這樣才能搭配 Render 免費方案的休眠機制正常運作）
2. 一個小型網站伺服器（用 FastAPI），提供：
   - /order            點餐頁面（Mini App，透過 Keyboard Button 開啟）
   - /select-restaurant 選餐廳頁面（Mini App，透過 Inline 按鈕開啟）
   - /admin            後台管理頁面（一般網頁，不需要透過 Telegram 開啟）
   - /telegram-webhook Telegram 推送新訊息的接收端點
   - /health           保活用的健康檢查端點（給 UptimeRobot 之類的服務定時打）
   - /api/...          給以上頁面呼叫的資料介面

功能總覽：
1. /start
   - 該聊天室第一個打 /start 的人 = 本場「發起人」
   - 顯示「🍽️ 開始點餐」鍵盤按鈕（點餐頁面）
   - 顯示「🏠 選擇餐廳」「🛑 結束點餐」兩顆 Inline 按鈕
2. 發起人先用「🏠 選擇餐廳」選好餐廳（滾輪式選單），其他人才能開始點餐
3. 大家用「🍽️ 開始點餐」點餐，送出後單行顯示在聊天室
4. 發起人按「🛑 結束點餐」：顯示每人明細 + 品項彙總、清除場次記憶
5. /admin 後台：新增餐廳、手動編輯菜單、上傳照片用 AI（Gemini）辨識菜單後手動校正再儲存

注意：
- 場次記憶、餐廳/菜單資料目前都存在「記憶體」裡（Python 的 dict），
  這支程式一重啟（Railway 重新部署）就會清空、回到預設的 5 間測試餐廳。
  等之後要正式上線、資料不能不見的話，要幫你接上真正的資料庫。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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

# 部署到 Render 拿到網址後，把這裡換成那個網址
# 例如 https://ggman.onrender.com（不要加結尾斜線）
BASE_URL = "https://ggman.onrender.com"

# Telegram webhook 用的密鑰，隨便打一串英數字就好，不用跟任何人說
# （Telegram 每次推訊息過來都會帶著這組密鑰，用來確認真的是 Telegram 送來的）
WEBHOOK_SECRET_TOKEN = "3d5503dd54239bc4e701d1645cb9e456"

# 後台管理密碼：可以自己改成想要的密碼，/admin 頁面會用這組密碼保護
ADMIN_PASSWORD = "changeme123"

# Google AI Studio (Gemini) API Key，給後台「照片辨識菜單」功能用
GEMINI_API_KEY = "AQ.Ab8RN6L1AAOo15rPaF0lnlZfQph6-3zdFZ-MUJID9ATO1EyYnQ"
GEMINI_MODEL = "gemini-3.5-flash"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


# ------------------------------------------------------------------
# 資料儲存（記憶體版本，測試階段夠用）
# ------------------------------------------------------------------

# restaurants: { 餐廳名稱: [ {category, name, price}, ... ] }
restaurants: dict[str, list[dict]] = {
    "阿明快炒": [
        {"category": "熱炒", "name": "客家小炒", "price": 180},
        {"category": "熱炒", "name": "宮保雞丁", "price": 160},
        {"category": "湯品", "name": "酸辣湯", "price": 60},
    ],
    "巷口便當": [
        {"category": "便當", "name": "招牌雞腿便當", "price": 100},
        {"category": "便當", "name": "排骨便當", "price": 90},
        {"category": "湯品", "name": "貢丸湯", "price": 20},
    ],
    "涼夏冷飲": [
        {"category": "茶飲", "name": "古早味紅茶", "price": 25},
        {"category": "茶飲", "name": "冬瓜檸檬", "price": 35},
        {"category": "特調", "name": "百香雙響炮", "price": 50},
    ],
    "深夜燒烤": [
        {"category": "串烤", "name": "雞屁股", "price": 30},
        {"category": "串烤", "name": "杏鮑菇", "price": 35},
        {"category": "飲料", "name": "台灣啤酒", "price": 80},
    ],
    "晨光早餐店": [
        {"category": "蛋餅系列", "name": "招牌蛋餅", "price": 35},
        {"category": "吐司系列", "name": "總匯吐司", "price": 55},
        {"category": "飲料", "name": "大冰奶", "price": 30},
    ],
}

# active_sessions: { chat_id: {
#   "initiator_id": int,
#   "initiator_name": str,
#   "restaurant": str | None,
#   "orders": [單行訂單摘要文字, ...],
#   "grand_total": number,
#   "items_agg": {品項名稱: 數量},
# } }
active_sessions: dict[int, dict] = {}

END_ORDER_CALLBACK = "end_order"


def build_final_summary(session: dict) -> str:
    """把一場點餐的每人明細與品項彙總整理成單一則訊息文字。"""
    order_count = len(session["orders"])
    grand_total = session["grand_total"]
    restaurant = session.get("restaurant") or "（未選擇）"

    detail_lines = "\n".join(session["orders"]) if session["orders"] else "（沒有人點餐）"

    if session["items_agg"]:
        agg_lines = "\n".join(
            f"・{name} x{qty}" for name, qty in session["items_agg"].items()
        )
    else:
        agg_lines = "（無）"

    return (
        f"✅ 點餐結束\n"
        f"🏠 餐廳：{restaurant}\n"
        f"共 {order_count} 筆訂單，合計 NT${grand_total}\n\n"
        f"【每人明細】\n{detail_lines}\n\n"
        f"【品項彙總】\n{agg_lines}"
    )


# ------------------------------------------------------------------
# Telegram initData 驗證（Telegram 官方規定的算法）
# 用來確認「真的是這個 Telegram 使用者本人在操作」，避免有人偽造身份
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
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > max_age_seconds:
        return None

    user_json = parsed.get("user")
    user = json.loads(user_json) if user_json else None
    return {"user": user, "auth_date": auth_date}


# ------------------------------------------------------------------
# /start 指令
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id not in active_sessions:
        active_sessions[chat_id] = {
            "initiator_id": user.id,
            "initiator_name": user.full_name,
            "restaurant": None,
            "orders": [],
            "grand_total": 0,
            "items_agg": {},
        }
        initiator_name = user.full_name
    else:
        initiator_name = active_sessions[chat_id]["initiator_name"]

    # 重要：sendData() 只有透過「Keyboard Button」開啟才會生效
    order_url = f"{BASE_URL}/order?chat_id={chat_id}"
    keyboard = [[KeyboardButton(text="🍽️ 開始點餐", web_app=WebAppInfo(url=order_url))]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

    await update.message.reply_text(
        f"🍽️ 點餐請按下方按鈕\n（本場發起人：{initiator_name}）",
        reply_markup=reply_markup,
    )

    # 「選餐廳」跟「結束點餐」都用 Inline 按鈕，放同一則訊息，避免洗版
    select_url = f"{BASE_URL}/select-restaurant?chat_id={chat_id}"
    inline_markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏠 選擇餐廳（限發起人）", web_app=WebAppInfo(url=select_url))],
            [InlineKeyboardButton("🛑 結束點餐（限發起人）", callback_data=END_ORDER_CALLBACK)],
        ]
    )
    await update.message.reply_text(
        "發起人請先選擇餐廳，點餐結束後請按下方按鈕結束：",
        reply_markup=inline_markup,
    )


# ------------------------------------------------------------------
# 處理 WebApp 傳回的點餐資料
# ------------------------------------------------------------------
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if chat_id not in active_sessions:
        await update.message.reply_text("⚠️ 目前沒有進行中的點餐，請先請一位成員輸入 /start 發起。")
        return

    session = active_sessions[chat_id]

    if not session.get("restaurant"):
        await update.message.reply_text("⚠️ 發起人還沒選擇餐廳，請稍候。")
        return

    raw_data = update.effective_message.web_app_data.data

    try:
        order = json.loads(raw_data)
    except (json.JSONDecodeError, TypeError):
        logger.exception("無法解析 WebApp 傳回的資料: %s", raw_data)
        await update.message.reply_text("⚠️ 訂單資料格式錯誤，請重新點餐。")
        return

    items = order.get("items", [])
    total = order.get("total", 0)

    if not items:
        await update.message.reply_text("⚠️ 你的購物車是空的，請至少選擇一項餐點。")
        return

    customer_name = update.effective_user.full_name
    item_strs = [f"{item.get('name', '未知品項')}x{item.get('qty', 0)}" for item in items]
    summary_text = f"👤{customer_name} 🍽️{'、'.join(item_strs)} 💰NT${total}"

    await update.message.reply_text(summary_text)

    session["orders"].append(summary_text)
    session["grand_total"] += total
    for item in items:
        name = item.get("name", "未知品項")
        qty = item.get("qty", 0)
        session["items_agg"][name] = session["items_agg"].get(name, 0) + qty

    logger.info("收到新訂單 from %s: %s", update.effective_user.id, order)


# ------------------------------------------------------------------
# 結束點餐：文字指令 /end（備用）與 Inline 按鈕共用同一套邏輯
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

    await update.message.reply_text(final_text, reply_markup=ReplyKeyboardRemove())


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
    await context.bot.send_message(
        chat_id=chat_id,
        text=final_text,
        reply_markup=ReplyKeyboardRemove(),
    )


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("請輸入 /start 開始點餐 🍔")


async def admin_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"🔑 後台管理連結：\n{BASE_URL}/admin")


# ------------------------------------------------------------------
# FastAPI app + 把 Telegram bot 用 polling 方式跑在同一個程式裡
# ------------------------------------------------------------------
telegram_app: Application | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("end", end_session))
    telegram_app.add_handler(CommandHandler("admin", admin_link))
    telegram_app.add_handler(
        CallbackQueryHandler(end_button_callback, pattern=f"^{END_ORDER_CALLBACK}$")
    )
    telegram_app.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data)
    )
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

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
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET_TOKEN:
        raise HTTPException(403, "invalid secret token")

    data = await request.json()
    update = Update.de_json(data=data, bot=telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


@app.get("/health")
async def health():
    # 給 UptimeRobot / cron-job.org 這類保活服務定時打的端點，
    # 每 10 分鐘打一次，就能避免 Render 免費方案 15 分鐘沒流量自動休眠
    return {"status": "ok"}


# ---------------- 頁面 ----------------

@app.get("/order")
async def order_page():
    return FileResponse(STATIC_DIR / "order.html")


@app.get("/select-restaurant")
async def select_restaurant_page():
    return FileResponse(STATIC_DIR / "select_restaurant.html")


@app.get("/admin")
async def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


# ---------------- 公開讀取 API（點餐頁面 / 選餐廳頁面用） ----------------

@app.get("/api/restaurants")
async def api_list_restaurants():
    return {"restaurants": list(restaurants.keys())}


@app.get("/api/menu/{restaurant}")
async def api_get_menu(restaurant: str):
    if restaurant not in restaurants:
        raise HTTPException(404, "餐廳不存在")
    return {"restaurant": restaurant, "items": restaurants[restaurant]}


@app.get("/api/session/{chat_id}")
async def api_get_session(chat_id: int):
    session = active_sessions.get(chat_id)
    if not session:
        return {"active": False}
    return {
        "active": True,
        "restaurant": session["restaurant"],
    }


@app.get("/api/session/{chat_id}/is-initiator")
async def api_is_initiator(chat_id: int, x_telegram_init_data: str = Header(default="")):
    validated = validate_init_data(x_telegram_init_data, BOT_TOKEN)
    if not validated or not validated.get("user"):
        raise HTTPException(401, "無法驗證身份，請透過 Telegram 開啟此頁面")

    session = active_sessions.get(chat_id)
    if not session:
        return {"active": False, "is_initiator": False}

    return {
        "active": True,
        "is_initiator": validated["user"]["id"] == session["initiator_id"],
        "restaurant": session["restaurant"],
    }


class SelectRestaurantBody(BaseModel):
    restaurant: str


@app.post("/api/session/{chat_id}/restaurant")
async def api_select_restaurant(
    chat_id: int,
    body: SelectRestaurantBody,
    x_telegram_init_data: str = Header(default=""),
):
    validated = validate_init_data(x_telegram_init_data, BOT_TOKEN)
    if not validated or not validated.get("user"):
        raise HTTPException(401, "無法驗證身份，請透過 Telegram 開啟此頁面")

    telegram_user_id = validated["user"]["id"]

    session = active_sessions.get(chat_id)
    if not session:
        raise HTTPException(404, "找不到進行中的點餐場次")

    if telegram_user_id != session["initiator_id"]:
        raise HTTPException(403, "只有發起人可以選擇餐廳")

    if body.restaurant not in restaurants:
        raise HTTPException(400, "餐廳不存在")

    session["restaurant"] = body.restaurant
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
    restaurants[name] = []
    return {"ok": True}


class MenuItemBody(BaseModel):
    category: str = "未分類"
    name: str
    price: float


class SaveMenuBody(BaseModel):
    items: list[MenuItemBody]


@app.post("/api/admin/menu/{restaurant}")
async def api_save_menu(
    restaurant: str, body: SaveMenuBody, x_admin_password: str = Header(default="")
):
    check_admin_password(x_admin_password)
    if restaurant not in restaurants:
        raise HTTPException(404, "餐廳不存在，請先新增餐廳")
    restaurants[restaurant] = [item.dict() for item in body.items]
    return {"ok": True, "count": len(body.items)}


class OcrBody(BaseModel):
    image_base64: str


@app.post("/api/admin/menu/ocr")
async def api_menu_ocr(body: OcrBody, x_admin_password: str = Header(default="")):
    check_admin_password(x_admin_password)

    image_data = body.image_base64
    if image_data.startswith("data:") and "," in image_data:
        image_data = image_data.split(",", 1)[1]

    prompt = (
        "這是一張餐廳菜單的照片。請幫我辨識上面的每一個品項，"
        "輸出成 JSON 陣列，每個元素要有：\n"
        "- name：品項名稱（繁體中文）\n"
        "- price：數字，只留數字，不要貨幣符號或逗號\n"
        "- category：這個品項的分類（例如主食、小菜、飲料），"
        "如果看不出分類就填「未分類」\n"
        "只回傳 JSON 陣列本身，不要有其他說明文字，也不要用 markdown 的 ``` 包起來。"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_data}},
                ]
            }
        ]
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code != 200:
        logger.error("Gemini API 錯誤: %s", resp.text)
        raise HTTPException(502, f"AI 辨識失敗：{resp.text[:200]}")

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise HTTPException(502, "AI 回傳格式異常，請改用手動輸入")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]

    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Gemini 回傳無法解析: %s", text)
        raise HTTPException(502, "AI 回傳的內容無法解析，請改用手動輸入或重新拍一張更清楚的照片")

    return {"items": items}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
