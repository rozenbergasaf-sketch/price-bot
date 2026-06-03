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
        "אני מחפש בשלוש דרכים:\n"
        "🔗 *לינק* — הדבק לינק למוצר מכל אתר\n"
        "🖼️ *תמונה* — שלח תמונה של המוצר\n"
        "✏️ *שם* — כתוב את שם המוצר\n\n"
        "ואני אמצא את *5 המחירים הכי זולים* ב-AliExpress!",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *עזרה*\n\n"
        "שלח אחד מהבאים:\n"
        "• לינק למוצר מכל אתר\n"
        "• תמונה של המוצר\n"
        "• שם המוצר בטקסט\n\n"
        "הבוט יחפש ב-AliExpress ויחזיר 5 תוצאות מהזול לביקר.",
        parse_mode="Markdown"
    )


async def _do_search(loading_msg, product_name: str = None, image_bytes: bytes = None, image_mime: str = None):
    """Core search logic — called from URL, text, and photo handlers."""
    try:
        result = await searcher.search_prices(
            product_name=product_name,
            image_bytes=image_bytes,
            image_mime=image_mime
        )

        if not result["success"]:
            await loading_msg.edit_text(
                f"⚠️ {result['error']}\n\nנסה שוב עם לינק, תמונה או שם המוצר."
            )
            return

        found_name = result.get("product_name", "המוצר שלך")
        prices = result.get("prices", [])

        response = f"🛍️ *{found_name[:100]}*\n\n"

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
            response += "😕 לא נמצאו תוצאות.\nלחץ על הכפתור לחיפוש ידני:"

        query = found_name.replace(" ", "+")
        ali_url = f"https://www.aliexpress.com/wholesale?SearchText={query}&SortType=total_tranpro_desc"
        keyboard = [[InlineKeyboardButton("🛒 חפש ב-AliExpress", url=ali_url)]]

        await loading_msg.edit_text(
            response,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"_do_search error: {e}")
        await loading_msg.edit_text("❌ אירעה שגיאה בחיפוש. נסה שוב.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    urls = re.findall(r'https?://[^\s]+', text)
    if not urls:
        await update.message.reply_text("❌ לא מצאתי לינק תקין.")
        return
    loading_msg = await update.message.reply_text(
        "🔍 מחפש ב-AliExpress...\n⏳ _מנתח את המוצר מהלינק..._",
        parse_mode="Markdown"
    )
    await _do_search(loading_msg, product_name=urls[0])


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) < 3:
        await update.message.reply_text("כתוב שם מוצר או שלח לינק/תמונה 🛍️")
        return
    loading_msg = await update.message.reply_text(
        f"🔍 מחפש *{text[:50]}* ב-AliExpress...\n⏳ _רגע אחד..._",
        parse_mode="Markdown"
    )
    await _do_search(loading_msg, product_name=text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loading_msg = await update.message.reply_text(
        "🖼️ מזהה את המוצר מהתמונה...\n⏳ _רגע אחד..._",
        parse_mode="Markdown"
    )
    try:
        # Get highest resolution photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        await _do_search(loading_msg, image_bytes=bytes(image_bytes), image_mime="image/jpeg")
    except Exception as e:
        logger.error(f"handle_photo error: {e}")
        await loading_msg.edit_text("❌ לא הצלחתי לעבד את התמונה. נסה שוב.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if "http" in text:
        await handle_url(update, context)
    else:
        await handle_text(update, context)


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Set TELEGRAM_BOT_TOKEN environment variable!")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot is running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
