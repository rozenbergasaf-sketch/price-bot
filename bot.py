import os
import re
import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from price_searcher import PriceSearcher

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
searcher = PriceSearcher()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 שלום! אני בוט חיפוש מחירים ב-AliExpress.\n\n"
        "📦 שלח לי *לינק למוצר* מכל אתר\n"
        "ואני אמצא לך את *5 המחירים הכי זולים* ב-AliExpress! 🔍\n\n"
        "פשוט הדבק את הלינק בצ'אט ✨",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *עזרה*\n\n"
        "1️⃣ מצא מוצר שאתה רוצה לקנות\n"
        "2️⃣ העתק את הלינק שלו\n"
        "3️⃣ הדבק אותו כאן\n"
        "4️⃣ קבל 5 תוצאות מ-AliExpress מהזול לביקר!\n\n"
        "_הבוט מחלץ את שם המוצר ומחפש עבורך ב-AliExpress_",
        parse_mode="Markdown"
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, text)

    if not urls:
        await update.message.reply_text(
            "❌ לא מצאתי לינק בהודעה.\nאנא שלח לינק מלא (מתחיל ב-http:// או https://)"
        )
        return

    url = urls[0]

    loading_msg = await update.message.reply_text(
        "🔍 מחפש ב-AliExpress... רגע אחד!\n"
        "⏳ _מנתח את המוצר..._",
        parse_mode="Markdown"
    )

    try:
        result = await searcher.search_prices(url)

        if not result["success"]:
            await loading_msg.edit_text(
                f"⚠️ {result['error']}\n\nנסה לינק אחר או בדוק שהלינק תקין."
            )
            return

        product_name = result.get("product_name", "המוצר שלך")
        prices = result.get("prices", [])

        response = f"🛍️ *{product_name[:100]}*\n\n"

        if prices and prices[0].get("price") != "לחץ לחיפוש":
            response += "💰 *5 המחירים הזולים ביותר ב-AliExpress:*\n\n"
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            for i, item in enumerate(prices[:5]):
                medal = medals[i] if i < len(medals) else f"{i+1}."
                price = item.get("price", "?")
                link = item.get("link", "")
                title = item.get("title", "")

                if link:
                    response += f"{medal} *{price}*"
                    if title:
                        response += f"\n    _{title[:60]}_"
                    response += f"\n    🔗 [לרכישה]({link})\n\n"
                else:
                    response += f"{medal} *{price}*\n\n"

            cheapest = prices[0].get("price", "")
            if cheapest:
                response += f"✅ *הכי זול:* {cheapest}"
        else:
            response += "😕 לא נמצאו תוצאות מחיר.\nלחץ על הכפתור כדי לחפש ידנית:"

        query = product_name.replace(" ", "+")
        ali_url = f"https://www.aliexpress.com/wholesale?SearchText={query}&SortType=total_tranpro_desc"

        keyboard = [[InlineKeyboardButton("🛒 חפש ב-AliExpress", url=ali_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await loading_msg.edit_text(
            response,
            parse_mode="Markdown",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Error processing URL: {e}")
        await loading_msg.edit_text(
            "❌ אירעה שגיאה בחיפוש.\nנסה שוב מאוחר יותר או שלח לינק אחר."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "http" in text:
        await handle_url(update, context)
    else:
        await update.message.reply_text(
            "שלח לי לינק למוצר ואני אמצא את המחירים הכי זולים ב-AliExpress! 🛍️\n"
            "לעזרה הקלד /help"
        )


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Set TELEGRAM_BOT_TOKEN environment variable!")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot is running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
