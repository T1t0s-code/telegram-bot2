import os
import sqlite3
from typing import Optional, Set

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --------- ENV CONFIG (Railway Variables) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Railway Variables.")
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID is missing/invalid. Set ADMIN_ID (your numeric Telegram user id) in Railway Variables.")

ADMIN_ID = int(ADMIN_ID_RAW)

# --------- STORAGE (SQLite) ----------
DB_PATH = "bot.db"


def db_connect():
    return sqlite3.connect(DB_PATH)


def db_init():
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY
        )
        """
    )
    con.commit()
    con.close()


def whitelist_add(user_id: int) -> None:
    con = db_connect()
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (user_id,))
    con.commit()
    con.close()


def whitelist_remove(user_id: int) -> bool:
    con = db_connect()
    cur = con.cursor()
    cur.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed


def whitelist_all() -> Set[int]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT user_id FROM whitelist ORDER BY user_id ASC")
    rows = cur.fetchall()
    con.close()
    return {int(r[0]) for r in rows}


def whitelist_has(user_id: int) -> bool:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT 1 FROM whitelist WHERE user_id = ? LIMIT 1", (user_id,))
    row = cur.fetchone()
    con.close()
    return row is not None


# --------- BOT STATE (latest broadcast content) ----------
pending_text: Optional[str] = None
pending_photo_file_id: Optional[str] = None

BUTTON_GET_TEXT = "GET_TEXT"


def approved_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📩 Send / Get text", callback_data=BUTTON_GET_TEXT)]]
    )


# --------- HELPERS ----------
def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


async def send_admin_notice(context: ContextTypes.DEFAULT_TYPE, msg: str) -> None:
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg)
    except Exception:
        # avoid crashing on admin notification errors
        pass


# --------- COMMANDS ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if whitelist_has(uid):
        await update.message.reply_text(
            "Approved.\nUse the button below to get the latest text after you receive a photo.\nYou can also type /send.",
            reply_markup=approved_keyboard(),
        )
    else:
        await update.message.reply_text(
            "Not approved.\nAsk the admin to approve you."
        )


async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not whitelist_has(uid):
        await update.message.reply_text("You are not approved.")
        return

    if not pending_text:
        await update.message.reply_text("No text is currently available.")
        return

    await update.message.reply_text(pending_text)
    await send_admin_notice(context, f"✅ Sent text to user {uid} via /send")


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /approve USER_ID")
        return

    user_id = int(context.args[0])
    whitelist_add(user_id)
    await update.message.reply_text(f"✅ Approved {user_id} (saved).")


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /remove USER_ID")
        return

    user_id = int(context.args[0])
    changed = whitelist_remove(user_id)
    await update.message.reply_text(
        f"✅ Removed {user_id}." if changed else f"{user_id} was not in whitelist."
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    users = sorted(list(whitelist_all()))
    if not users:
        await update.message.reply_text("Whitelist is empty.")
        return

    # Telegram message length is limited; for small lists it’s fine.
    text = "Whitelist user IDs:\n" + "\n".join(str(u) for u in users)
    await update.message.reply_text(text)


# --------- ADMIN INPUT (photo/text) ----------
async def admin_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin sends a photo:
    - If it has a caption: set pending_text to caption
    - Always broadcast photo ONLY to whitelist
    - Store photo file_id (optional, for reference)
    """
    if not is_admin(update):
        return

    global pending_text, pending_photo_file_id

    photo = update.message.photo[-1]
    pending_photo_file_id = photo.file_id

    caption = (update.message.caption or "").strip()
    if caption:
        pending_text = caption

    users = whitelist_all()
    if not users:
        await update.message.reply_text("Whitelist is empty. Add users with /approve first.")
        return

    # Broadcast photo only
    sent = 0
    failed = 0
    for uid in users:
        try:
            await context.bot.send_photo(chat_id=uid, photo=pending_photo_file_id)
            sent += 1
        except Exception:
            failed += 1

    if caption:
        await update.message.reply_text(f"📸 Photo sent to {sent} users. Text updated from caption.")
    else:
        await update.message.reply_text(
            f"📸 Photo sent to {sent} users.\nNow send the text as a normal message (or send photo+caption next time)."
        )

    if failed:
        await update.message.reply_text(f"⚠️ Failed to send to {failed} users (they may have blocked the bot).")


async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin sends a normal text message:
    - Update pending_text
    - Does NOT broadcast anything
    """
    if not is_admin(update):
        return

    global pending_text
    text = (update.message.text or "").strip()
    if not text:
        return

    # Prevent accidental commands being treated as broadcast text
    if text.startswith("/"):
        return

    pending_text = text
    await update.message.reply_text("✅ Text saved. Users can now press the button or use /send to receive it.")


# --------- BUTTON HANDLER ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id

    if query.data != BUTTON_GET_TEXT:
        return

    if not whitelist_has(uid):
        await query.message.reply_text("You are not approved.")
        return

    if not pending_text:
        await query.message.reply_text("No text is currently available.")
        return

    await query.message.reply_text(pending_text)
    await send_admin_notice(context, f"✅ Sent text to user {uid} via button")


# --------- MAIN ----------
def main():
    db_init()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("send", send_cmd))

    # Admin commands
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))

    # Admin photo + admin text handlers
    app.add_handler(MessageHandler(filters.PHOTO, admin_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))

    # Buttons
    app.add_handler(CallbackQueryHandler(button_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
