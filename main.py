"""
Telegram 點餐 Bot 後端
運行需求：python-telegram-bot v20+ (async)

功能：
1. /start -> 顯示「🍽️ 開始點餐」鍵盤按鈕（開啟 Mini App 點餐頁面），
   並另外顯示一顆「🛑 結束點餐」Inline 按鈕。
   第一個在該聊天室輸入 /start 的人會被記錄為「本場發起人」
2. 接收 WebApp 傳回的點餐 JSON (Telegram.WebApp.sendData)
   -> 整理成單行訂單摘要（點餐人 品項 金額）送回聊天室，避免洗版
3. 「🛑 結束點餐」按鈕（或文字指令 /end）：
   只有發起人點擊/輸入才會真的結束，其他人點擊只會跳出提示
   「你不是開單發起人」。結束後會顯示每人明細 + 品項彙總、
   清除場次記憶、收回鍵盤按鈕，確保不會殘留到下一場

注意：場次記憶存在記憶體（一個 Python dict）裡，bot 重啟後會清空。
若之後要換到 Railway 這種平台常駐運作重新部署，記得留意重啟會導致
進行中的場次遺失，需要重新 /start。
"""

import json
import logging

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
# 專案設定
# ------------------------------------------------------------------

BOT_TOKEN = "8396988188:AAHnH2wRRu0IpnMB7gicvqwXc6bB8f-axso"

WEBAPP_URL = "https://168cod-del.github.io/ggman/"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 點餐場次記憶（存在記憶體中，bot 重啟就會清空）
# key: chat_id -> {
#   "initiator_id": 發起人 user id,
#   "initiator_name": 發起人姓名,
#   "orders": [單行訂單摘要文字, ...],
#   "grand_total": 累計金額,
#   "items_agg": {品項名稱: 累計數量, ...},
# }
# ------------------------------------------------------------------
active_sessions: dict[int, dict] = {}

END_ORDER_CALLBACK = "end_order"


def build_final_summary(session: dict) -> str:
    """把一場點餐的每人明細與品項彙總整理成單一則訊息文字。"""
    order_count = len(session["orders"])
    grand_total = session["grand_total"]

    detail_lines = "\n".join(session["orders"]) if session["orders"] else "（沒有人點餐）"

    if session["items_agg"]:
        agg_lines = "\n".join(
            f"・{name} x{qty}" for name, qty in session["items_agg"].items()
        )
    else:
        agg_lines = "（無）"

    return (
        f"✅ 點餐結束，共 {order_count} 筆訂單，合計 NT${grand_total}\n\n"
        f"【每人明細】\n{detail_lines}\n\n"
        f"【品項彙總】\n{agg_lines}"
    )


# ------------------------------------------------------------------
# /start 指令：顯示歡迎訊息 + 開啟 Mini App 按鈕
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # 重要：sendData() 只有在透過「Keyboard Button」開啟 Mini App 時才會生效，
    # 用 InlineKeyboardButton 或 BotFather 的 Menu Button 開啟的話，
    # 點餐完傳回的資料會被 Telegram 直接忽略，訂單永遠送不回聊天室。
    keyboard = [
        [
            KeyboardButton(
                text="🍽️ 開始點餐",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ]
    ]
    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True,
    )

    chat_id = update.effective_chat.id
    user = update.effective_user

    end_button_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 結束點餐", callback_data=END_ORDER_CALLBACK)]]
    )

    if chat_id not in active_sessions:
        # 這個聊天室目前沒有進行中的點餐 -> 這個人就是這一場的發起人
        active_sessions[chat_id] = {
            "initiator_id": user.id,
            "initiator_name": user.full_name,
            "orders": [],
            "grand_total": 0,
            "items_agg": {},
        }
        await update.message.reply_text(
            f"🍽️ 點餐請按下方按鈕\n（發起人：{user.full_name}）",
            reply_markup=reply_markup,
        )
    else:
        # 已經有人發起了，不覆蓋發起人，只是重新顯示按鈕方便這個人點餐
        initiator_name = active_sessions[chat_id]["initiator_name"]
        await update.message.reply_text(
            f"🍽️ 點餐請按下方按鈕\n（本場發起人：{initiator_name}）",
            reply_markup=reply_markup,
        )

    # Inline 按鈕跟鍵盤按鈕不能同時放在同一則訊息上，所以另外發一則
    await update.message.reply_text(
        "發起人可按下方按鈕結束點餐：",
        reply_markup=end_button_markup,
    )


# ------------------------------------------------------------------
# 處理 WebApp 傳回的資料 (Telegram.WebApp.sendData 觸發)
# ------------------------------------------------------------------
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if chat_id not in active_sessions:
        await update.message.reply_text("⚠️ 目前沒有進行中的點餐，請先請一位成員輸入 /start 發起。")
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

    # 訂單摘要：單行顯示「點餐人 品項 金額」，避免洗版
    customer_name = update.effective_user.full_name
    item_strs = [f"{item.get('name', '未知品項')}x{item.get('qty', 0)}" for item in items]

    summary_text = f"👤{customer_name} 🍽️{'、'.join(item_strs)} 💰NT${total}"

    await update.message.reply_text(summary_text)

    # 累加進這一場的紀錄，供 /end 結算用
    session = active_sessions[chat_id]
    session["orders"].append(summary_text)
    session["grand_total"] += total

    for item in items:
        name = item.get("name", "未知品項")
        qty = item.get("qty", 0)
        session["items_agg"][name] = session["items_agg"].get(name, 0) + qty

    logger.info("收到新訂單 from %s: %s", update.effective_user.id, order)


# ------------------------------------------------------------------
# /end 指令：只有發起人可以結束這一場點餐，結束後清除記憶、收回按鈕
# （保留文字指令當備用，主要操作建議直接用下方的「🛑 結束點餐」按鈕）
# ------------------------------------------------------------------
async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id not in active_sessions:
        await update.message.reply_text("目前沒有進行中的點餐。")
        return

    session = active_sessions[chat_id]

    if user.id != session["initiator_id"]:
        await update.message.reply_text(
            f"⚠️ 只有發起人「{session['initiator_name']}」可以結束這場點餐。"
        )
        return

    final_text = build_final_summary(session)

    # 結束並清除這一場的記憶，避免留到下一場
    del active_sessions[chat_id]

    await update.message.reply_text(
        final_text,
        reply_markup=ReplyKeyboardRemove(),
    )


# ------------------------------------------------------------------
# 「🛑 結束點餐」按鈕的回呼：只有發起人點擊才會真的結束，
# 其他人點擊只會跳出小提示「你不是開單發起人」，不會洗版
# ------------------------------------------------------------------
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

    await query.answer()  # 消掉按鈕上的載入中狀態

    final_text = build_final_summary(session)

    # 結束並清除這一場的記憶，避免留到下一場
    del active_sessions[chat_id]

    # 把按鈕從那則訊息收掉，避免結束後還能再按第二次
    await query.edit_message_reply_markup(reply_markup=None)

    await context.bot.send_message(
        chat_id=chat_id,
        text=final_text,
        reply_markup=ReplyKeyboardRemove(),
    )


# ------------------------------------------------------------------
# 一般文字訊息的簡單回覆（非必要，方便測試）
# ------------------------------------------------------------------
async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("請輸入 /start 開始點餐 🍔")


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("end", end_session))
    application.add_handler(
        CallbackQueryHandler(end_button_callback, pattern=f"^{END_ORDER_CALLBACK}$")
    )
    application.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    logger.info("Bot 啟動中...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
