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
        "👋 שלום! אני בוט חיפוש מחירים.\n\n"
        "📦 שלח לי *לינק למוצר* מכל אתר קניות (Amazon, eBay, AliExpress, Zara, כלשהו)\n"
        "ואני אחפש לך היכן ניתן למצוא אותו *הכי זול*! 🔍\n\n"
        "פשוט הדבק את הלינק בצ'אט ✨",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *עזרה*\n\n"
        "1️⃣ מצא מוצר שאתה רוצה לקנות\n"
        "2️⃣ העתק את הלינק שלו\n"
        "3️⃣ הדבק אותו כאן\n"
        "4️⃣ קבל השוואת מחירים!\n\n"
        "*אתרים נתמכים:*\n"
        "• Amazon 🛒\n"
        "• eBay 🏷️\n"
        "• AliExpress 📦\n"
        "• כל לינק מוצר אחר!\n\n"
        "_הבוט יחלץ את שם המוצר ויחפש מחירים בגוגל שופינג_",
        parse_mode="Markdown"
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # Extract URL from message
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, text)
    
    if not urls:
        await update.message.reply_text(
            "❌ לא מצאתי לינק בהודעה.\nאנא שלח לינק מלא (מתחיל ב-http:// או https://)"
        )
        return
    
    url = urls[0]
    
    # Send loading message
    loading_msg = await update.message.reply_text(
        "🔍 מחפש מחירים... רגע אחד!\n"
        "⏳ _מנתח את המוצר ומחפש בכל האינטרנט..._",
        parse_mode="Markdown"
    )
    
    try:
        result = await searcher.search_prices(url)
        
        if not result["success"]:
            await loading_msg.edit_text(
                f"⚠️ {result['error']}\n\n"
                "נסה לינק אחר או בדוק שהלינק תקין."
            )
            return
        
        # Format response
        product_name = result.get("product_name", "המוצר שלך")
        prices = result.get("prices", [])
        
        response = f"🛍️ *{product_name}*\n\n"
        response += "💰 *תוצאות חיפוש מחיר:*\n\n"
        
        if prices:
            for i, item in enumerate(prices[:8], 1):
                emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                name = item.get("store", "חנות לא ידועה")
                price = item.get("price", "מחיר לא זמין")
                link = item.get("link", "")
                
                if link:
                    response += f"{emoji} *{name}*\n   💵 {price}\n   🔗 [לחץ לרכישה]({link})\n\n"
                else:
                    response += f"{emoji} *{name}*\n   💵 {price}\n\n"
            
            # Add cheapest highlight
            if prices[0].get("price"):
                response += f"✅ *הכי זול:* {prices[0].get('store')} - {prices[0].get('price')}"
        else:
            response += "😕 לא נמצאו תוצאות מחיר.\nנסה לחפש ישירות ב-Google Shopping."
        
        # Add Google Shopping search button
        search_query = product_name.replace(" ", "+")
        google_shopping_url = f"https://www.google.com/search?tbm=shop&q={search_query}"
        pricespy_url = f"https://www.pricespy.co.uk/search?search={search_query}"
        
        keyboard = [
            [
                InlineKeyboardButton("🛒 Google Shopping", url=google_shopping_url),
                InlineKeyboardButton("📊 PriceSpy", url=pricespy_url),
            ],
        ]
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
            "❌ אירעה שגיאה בחיפוש.\n"
            "נסה שוב מאוחר יותר או שלח לינק אחר."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "http" in text:
        await handle_url(update, context)
    else:
        await update.message.reply_text(
            "שלח לי לינק למוצר ואני אחפש את המחיר הכי זול! 🛍️\n"
            "לעזרה הקלד /help"
        )


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Set TELEGRAM_BOT_TOKEN environment variable!")
        print("Get a token from @BotFather on Telegram")
        return
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()