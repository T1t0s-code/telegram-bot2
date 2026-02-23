import os
import sqlite3
from typing import Optional, Set

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in Railway Variables")
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID missing or invalid")

ADMIN_ID = int(ADMIN_ID_RAW)

DB_PATH = "bot.db"


def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()


def whitelist_add(user_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO whitelist VALUES (?)", (user_id,))
    con.commit()
    con.close()


def whitelist_all() -> Set[int]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT user_id FROM whitelist")
    rows = cur.fetchall()
    con.close()
    return {r[0] for r in rows}


def whitelist_has(user_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM whitelist WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row is not None


pending_text: Optional[str] = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if whitelist_has(uid):
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📩 Get Text", callback_data="GET_TEXT")]]
        )
        await update.message.reply_text("Approved.", reply_markup=keyboard)
    else:
        await update.message.reply_text("Not approved.")


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /approve USER_ID")
        return

    whitelist_add(int(context.args[0]))
    await update.message.reply_text("User approved.")


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    users = whitelist_all()
    await update.message.reply_text("\n".join(str(u) for u in users) or "Empty")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    global pending_text

    caption = (update.message.caption or "").strip()
    if caption:
        pending_text = caption

    photo = update.message.photo[-1].file_id

    for uid in whitelist_all():
        await context.bot.send_photo(uid, photo)

    await update.message.reply_text("Photo broadcasted.")


async def send_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not whitelist_has(uid):
        return

    if not pending_text:
        await update.message.reply_text("No text saved.")
        return

    await update.message.reply_text(pending_text)
    await context.bot.send_message(ADMIN_ID, f"Sent text to {uid}")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id

    # NOT WHITELISTED
    if not whitelist_has(uid):
        await query.answer("❌ You are not approved.", show_alert=True)

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🚨 Non-whitelisted user tried bot: {uid}"
        )
        return

    # NO TEXT SAVED
    if not pending_text:
        await query.answer("No text saved yet.", show_alert=True)
        return

    # APPROVED USER
    await query.message.reply_text(pending_text)

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"✅ Sent text to approved user {uid}"
    )

def main():
    db_init()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("send", send_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(button))

    app.run_polling()


if __name__ == "__main__":
    main()
