"""
Telegram 點餐 Bot 後端
運行需求：python-telegram-bot v20+ (async)

功能：
1. /start -> 顯示歡迎訊息 + 開啟 Mini App 點餐頁面的按鈕
2. 接收 WebApp 傳回的點餐 JSON (Telegram.WebApp.sendData) -> 整理成訂單摘要送回聊天室
"""

import json
import logging

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
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------
# 專案設定
# ------------------------------------------------------------------

BOT_TOKEN = "8396988188:AAHnH2wRRu0IpnMB7gicvqwXc6bB8f-axso"

# 部署完成後，把這裡換成你的 Mini App 網址 (必須是 https)
# 例如用 GitHub Pages / Vercel / Netlify 部署 index.html 後拿到的網址
WEBAPP_URL = "https://168cod-del.github.io/ggman/"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# /start 指令：顯示歡迎訊息 + 開啟 Mini App 按鈕
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            InlineKeyboardButton(
                text="🍽️ 開始點餐",
                web_app=WebAppInfo(url=WEBAPP_URL),
            )
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    user_name = update.effective_user.first_name or "顧客"
    await update.message.reply_text(
        f"👋 嗨 {user_name}，歡迎光臨！\n\n"
        "請點擊下方按鈕開啟點餐頁面，選好餐點後按「確認點餐」，\n"
        "訂單就會自動回傳到這裡讓我們為你處理。",
        reply_markup=reply_markup,
    )


# ------------------------------------------------------------------
# 處理 WebApp 傳回的資料 (Telegram.WebApp.sendData 觸發)
# ------------------------------------------------------------------
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    # 組合訂單摘要文字：只保留「點單人、品項、金額」
    customer_name = update.effective_user.full_name
    item_strs = [f"{item.get('name', '未知品項')} x{item.get('qty', 0)}" for item in items]

    summary_text = (
        f"👤 {customer_name}\n"
        f"🍽️ {'、'.join(item_strs)}\n"
        f"💰 NT${total}"
    )

    await update.message.reply_text(summary_text)

    # 這裡可以擴充：把訂單寫入資料庫、轉發到店家群組、串接金流等
    logger.info("收到新訂單 from %s: %s", update.effective_user.id, order)


# ------------------------------------------------------------------
# 一般文字訊息的簡單回覆（非必要，方便測試）
# ------------------------------------------------------------------
async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("請輸入 /start 開始點餐 🍔")


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

    logger.info("Bot 啟動中...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
