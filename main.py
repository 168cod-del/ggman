"""
Telegram 點餐 Bot 後端（文字點餐版）

這支程式同時做兩件事：
1. Telegram Bot（webhook 模式，適合 Render 免費方案的休眠機制）
2. 一個小型網站伺服器（FastAPI），提供：
   - /admin             後台管理頁面（新增餐廳、上傳/管理菜單照片）
   - /telegram-webhook  Telegram 推送新訊息的接收端點
   - /health            保活用的健康檢查端點
   - /api/...           給後台頁面呼叫的資料介面

功能總覽：
1. /start（或「/开单」「/開單」）
   - 該聊天室第一個發起的人 = 本場「發起人」
   - 顯示一排餐廳按鈕 +「🛑 結束點餐」（都是一般 Inline 按鈕，不是 Mini App）
2. 發起人點選其中一顆餐廳按鈕，選定後會把該餐廳的菜單照片直接發到聊天室，
   讓大家對照著點餐
3. 大家直接在聊天室打字點餐，支援多種格式（金額必填）：
   單品項：品項X數量 金額 [備註]              例：牛排X2 300 五分熟
   多品項＋各自金額：品項1 品項2 金額1+金額2   例：地瓜球 紅茶 30+30 少冰
                     品項1+品項2+品項3 金額1+金額2+金額3
                     品項1品項1金額品項2金額...（無分隔符號也可以）
   多品項＋共用總金額（金額需放最後）：品項1 品項2 總金額
                     品項1 品項2＝總金額
   （品項之間可以用 + / . 或空白分隔）
   每人每場只會保留「最新一筆」訂單，重複輸入會覆蓋前一筆
4. 想刪除自己這場的訂單，直接打「/删」「/删单」「/删除」（或繁體「/刪」「/刪單」「/刪除」）
   即可，不用二次確認，成功會回覆「刪單成功」
5. 發起人按「🛑 結束點餐」：顯示每人明細 + 品項彙總、清除場次記憶
6. /newmenu 餐廳名稱（或「/新增菜單 餐廳名稱」「/新增菜单 餐厅名称」）
   → bot 會請「發出這個指令的人」接著上傳一張菜單照片，
   上傳成功才算真的新增完成（不需要再打任何品項文字）
7. /admin 後台：新增餐廳、上傳/管理菜單照片（電腦、手機瀏覽器都可以直接上傳）、刪除餐廳

嚴格的回應規則（很重要）：
- 只有「/start /开单 /開單 /end /admin /newmenu /新增菜單 /新增菜单 /删 /删单 /删除
  /刪 /刪單 /刪除」這些指令，以及「剛打完新增菜單指令的那個人所上傳的下一張照片」，
  bot 才會在任何時候回應。
- 除此之外，只有在該聊天室「有正在進行中的點餐場次，且已選好餐廳」時，
  bot 才會嘗試把文字訊息解析成「品項 金額 備註」的點餐內容；
  解析不出來的一般聊天一律安靜忽略，不回應、不打擾。
- 沒有任何進行中場次時，群組裡的其他任何訊息（包含沒有配對到的照片）一律不理會。

注意：
- Telegram 規定「web_app 型態的按鈕」不管是 Inline 按鈕還是 Keyboard 按鈕，
  一律只能在私訊使用，群組裡用就會直接報錯（BadRequest）。
  所以「選餐廳」用一般的 Inline 按鈕清單（callback_data 類型，沒有這個限制），
  不用 Mini App 也能限定只有發起人選擇有效。
- 場次記憶、餐廳/菜單資料、新增菜單的暫存狀態，目前都存在「記憶體」裡，
  這支程式一重啟（Render 重新部署）就會清空。
  等之後要正式上線、資料不能不見的話，要幫你接上真正的資料庫。
- 因為點餐是直接看群組裡的文字訊息辨識，
  bot 必須關閉 Telegram 的隱私模式（BotFather → /setprivacy → Disable），
  否則群組裡的一般文字訊息（包含點餐內容）不會被 bot 收到。
"""

from __future__ import annotations

import base64
import io
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
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

# restaurants: { 餐廳名稱: {
#   "photo_file_id": str | None,       # 從 Telegram 聊天室上傳照片時會有值
#   "photo_bytes_base64": str | None,  # 從網頁後台上傳照片時會有值
# } }
restaurants: dict[str, dict] = {
    "阿明快炒": {"photo_file_id": None, "photo_bytes_base64": None},
    "巷口便當": {"photo_file_id": None, "photo_bytes_base64": None},
    "涼夏冷飲": {"photo_file_id": None, "photo_bytes_base64": None},
    "深夜燒烤": {"photo_file_id": None, "photo_bytes_base64": None},
    "晨光早餐店": {"photo_file_id": None, "photo_bytes_base64": None},
}
# 這 5 間是測試用的預設餐廳，還沒有菜單照片；
# 可以直接用 /新增菜單 在聊天室補一次，或到後台網頁上傳照片

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

END_ORDER_CALLBACK = "end_order"
SELECT_RESTAURANT_PREFIX = "select_restaurant:"


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


def record_order(
    chat_id: int,
    user_id: int,
    user_name: str,
    items: list[dict],
    note: str,
    raw_text: str,
    total_override=None,
):
    """把解析好的訂單記錄進場次（同一人重複點餐會覆蓋前一筆）。
    回傳要貼回聊天室的單行摘要文字；若場次不存在或餐廳未選，回傳 None。"""
    session = active_sessions.get(chat_id)
    if not session or not session.get("restaurant"):
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

    return build_order_line(user_name, order_record)


def build_order_line(user_name: str, order_record: dict) -> str:
    item_strs = [f"{it['name']}x{it['qty']}" for it in order_record["items"]]
    note_part = f"（{order_record['note']}）" if order_record.get("note") else ""
    total = order_record["total"]
    price_part = f" 💰NT${total}" if total > 0 else ""
    return f"👤{user_name} 🍽️{'、'.join(item_strs)}{note_part}{price_part}"


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

    total_line = f"合計 NT${grand_total}" if grand_total > 0 else "（沒有輸入金額，請自行加總）"

    return (
        f"✅ 點餐結束\n"
        f"🏠 餐廳：{restaurant}\n"
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
            "restaurant": None,
            "orders_by_user": {},
        }
        initiator_name = user.full_name
    else:
        initiator_name = active_sessions[chat_id]["initiator_name"]

    # 重要：Telegram 規定「web_app 型態的按鈕」不管是 Inline 還是 Keyboard，
    # 一律只能在私訊使用，群組裡用了會直接報錯。所以選餐廳改成一般的按鈕清單
    # （callback_data 類型，沒有這個限制），不用 Mini App 也能做到限定發起人選擇。
    restaurant_names = list(restaurants.keys())
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"{SELECT_RESTAURANT_PREFIX}{name}")]
        for name in restaurant_names
    ]
    buttons.append([InlineKeyboardButton("🛑 結束點餐（限發起人）", callback_data=END_ORDER_CALLBACK)])

    await update.message.reply_text(
        f"🍽️ 點餐開始！發起人：{initiator_name}\n"
        "發起人請從下方選一間餐廳，選好後大家直接打「品項 金額 備註」點餐即可（例：牛排X2 300 五分熟）\n"
        "想刪除自己的訂單可以打「/删」「/删单」「/删除」",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ------------------------------------------------------------------
# 文字點餐（一般文字訊息）
# ------------------------------------------------------------------
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text or ""
    stripped = text.strip()
    chat_id = update.effective_chat.id
    user = update.effective_user
    is_private = update.effective_chat.type == "private"

    # 中文替代指令：不用打 /start / /newmenu，直接打這兩個也可以（繁簡都支援）
    if stripped in ("/开单", "/開單"):
        await start(update, context)
        return
    if stripped.startswith("/新增菜单") or stripped.startswith("/新增菜單"):
        await new_menu(update, context)
        return

    # 刪除自己在本場的訂單，不用二次確認（繁簡都支援）
    if stripped in ("/删", "/删单", "/删除", "/刪", "/刪單", "/刪除"):
        session = active_sessions.get(chat_id)
        if session and session["orders_by_user"].pop(user.id, None) is not None:
            await update.message.reply_text("刪單成功")
        return

    session = active_sessions.get(chat_id)
    if not session or not session.get("restaurant"):
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
    summary = record_order(chat_id, user.id, user.full_name, items, note, text, total_override)
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
    restaurants[restaurant_name] = {"photo_file_id": photo.file_id, "photo_bytes_base64": None}
    del pending_menu_uploads[user_id]

    await update.message.reply_text(f"✅ 已新增「{restaurant_name}」，菜單照片已儲存。")


async def admin_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"🔑 後台管理連結：\n{BASE_URL}/admin")


def _has_photo(restaurant_data: dict) -> bool:
    return bool(restaurant_data.get("photo_file_id") or restaurant_data.get("photo_bytes_base64"))


async def send_restaurant_menu(bot, chat_id: int, restaurant: str) -> None:
    """把餐廳的菜單照片發到聊天室；沒有照片就發文字提示。
    菜單照片可能是從 Telegram 聊天室上傳（photo_file_id）或從網頁後台上傳
    （photo_bytes_base64），兩種來源都要能正常發送。"""
    data = restaurants[restaurant]
    photo_file_id = data.get("photo_file_id")
    photo_bytes_b64 = data.get("photo_bytes_base64")
    caption = f"🍽️ {restaurant} 菜單，請大家對照著點餐"

    if photo_file_id:
        await bot.send_photo(chat_id=chat_id, photo=photo_file_id, caption=caption)
    elif photo_bytes_b64:
        photo_bytes = base64.b64decode(photo_bytes_b64)
        await bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(io.BytesIO(photo_bytes), filename="menu.jpg"),
            caption=caption,
        )
    else:
        await bot.send_message(chat_id=chat_id, text=f"🏠 已選擇「{restaurant}」（這間餐廳目前還沒有菜單照片）")


# ------------------------------------------------------------------
# 選餐廳按鈕（一般 Inline 按鈕 + callback_data，群組完全合法，沒有 web_app 限制）
# ------------------------------------------------------------------
async def select_restaurant_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat.id
    user = query.from_user

    session = active_sessions.get(chat_id)
    if not session:
        await query.answer("目前沒有進行中的點餐。", show_alert=True)
        return

    if user.id != session["initiator_id"]:
        await query.answer("只有發起人可以選擇餐廳", show_alert=True)
        return

    restaurant = query.data[len(SELECT_RESTAURANT_PREFIX):]
    if restaurant not in restaurants:
        await query.answer("餐廳不存在。", show_alert=True)
        return

    await query.answer()
    session["restaurant"] = restaurant
    await query.edit_message_reply_markup(reply_markup=None)

    await send_restaurant_menu(context.bot, chat_id, restaurant)


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


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # 任何一個訊息處理失敗，都只記錄下來，絕對不能讓整個服務跟著崩潰
    # （崩潰會導致 webhook 被意外取消設定，之後所有訊息都收不到）
    logger.exception("處理更新時發生未預期的錯誤", exc_info=context.error)


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
    telegram_app.add_handler(
        CallbackQueryHandler(select_restaurant_callback, pattern=f"^{SELECT_RESTAURANT_PREFIX}")
    )
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
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


# ---------------- 頁面 ----------------

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
    return {"restaurant": restaurant, "has_photo": _has_photo(restaurants[restaurant])}


@app.get("/api/session/{chat_id}")
async def api_get_session(chat_id: int):
    session = active_sessions.get(chat_id)
    if not session:
        return {"active": False}
    return {"active": True, "restaurant": session["restaurant"]}


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
    restaurants[name] = {"photo_file_id": None, "photo_bytes_base64": None}
    return {"ok": True}


@app.get("/api/admin/restaurants")
async def api_admin_list_restaurants(x_admin_password: str = Header(default="")):
    check_admin_password(x_admin_password)
    return {
        "restaurants": [
            {"name": name, "has_photo": _has_photo(data)}
            for name, data in restaurants.items()
        ]
    }


class UploadPhotoBody(BaseModel):
    image_base64: str


@app.post("/api/admin/restaurants/{restaurant}/photo")
async def api_upload_restaurant_photo(
    restaurant: str, body: UploadPhotoBody, x_admin_password: str = Header(default="")
):
    check_admin_password(x_admin_password)
    if restaurant not in restaurants:
        raise HTTPException(404, "餐廳不存在，請先新增餐廳")

    image_data = body.image_base64
    if image_data.startswith("data:") and "," in image_data:
        image_data = image_data.split(",", 1)[1]

    try:
        base64.b64decode(image_data)
    except Exception:
        raise HTTPException(400, "圖片資料格式錯誤")

    restaurants[restaurant]["photo_bytes_base64"] = image_data
    restaurants[restaurant]["photo_file_id"] = None  # 換成新照片後，舊的 file_id 版本失效
    return {"ok": True}


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
