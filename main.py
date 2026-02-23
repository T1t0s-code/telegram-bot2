import os
import sqlite3
from typing import Optional, Set

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
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
    cur.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (user_id,))
    con.commit()
    con.close()

def whitelist_remove(user_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed

def whitelist_all() -> Set[int]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT user_id FROM whitelist ORDER BY user_id ASC")
    rows = cur.fetchall()
    con.close()
    return {int(r[0]) for r in rows}

def whitelist_has(user_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM whitelist WHERE user_id = ? LIMIT 1", (user_id,))
    row = cur.fetchone()
    con.close()
    return row is not None


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /remove USER_ID")
        return

    user_id = int(context.args[0])
    removed = whitelist_remove(user_id)

    if removed:
        await update.message.reply_text(f"✅ Removed {user_id} from whitelist.")
    else:
        await update.message.reply_text(f"⚠️ {user_id} was not in the whitelist.")




def inline_send_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📩 Send", callback_data="GET_TEXT")]]
    )

def reply_send_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/send")]],
        resize_keyboard=True
    )

pending_text: Optional[str] = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Press 📩 Send (or /send) to receive the latest text (approved users only).",
        reply_markup=inline_send_keyboard(),
    )


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

    photo_file_id = update.message.photo[-1].file_id

    users = whitelist_all()
    if not users:
        await update.message.reply_text("Whitelist is empty. Add users with /approve first.")
        return

    sent_to = []
    failed_to = []

    for uid in sorted(users):
        try:
            # Photo with inline button
            await context.bot.send_photo(
                chat_id=uid,
                photo=photo_file_id,
                caption="Tap 📩 Send or use /send to get the text.",
                reply_markup=inline_send_keyboard(),
            )    
            sent_to.append(uid)
        except Exception:
            failed_to.append(uid)

    msg = f"📸 Broadcast complete.\n✅ Sent to: {sent_to if sent_to else 'none'}"
    if failed_to:
        msg += f"\n⚠️ Failed: {failed_to} (they may not have started the bot or blocked it)"
    if caption:
        msg += "\n📝 Text updated from photo caption."
    else:
        msg += "\n📝 No caption. Send a normal text message now to set the text."

    await update.message.reply_text(msg)

async def send_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not whitelist_has(uid):
        await update.message.reply_text("❌ You are not approved.")
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🚨 Non-whitelisted user tried /send: {uid}"
        )
        return

    if not pending_text:
        await update.message.reply_text("No text saved yet.")
        return

    await update.message.reply_text(pending_text)
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"✅ Sent text to approved user {uid} via /send"
    )


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

async def addme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    name = user.full_name
    username = f"@{user.username}" if user.username else "(no username)"

    # Tell the user their request was sent
    await update.message.reply_text("Request sent to admin. Wait for approval.")

    # Alert admin with info + instructions
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "📥 Approval request\n"
            f"ID: {uid}\n"
            f"Name: {name}\n"
            f"Username: {username}\n\n"
            "Approve with:\n"
            f"/approve {uid}\n"
            "OR reply to one of their messages with /approve_reply"
        )
    )


async def approve_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin-only
    if update.effective_user.id != ADMIN_ID:
        return

    # Must be used as a reply to a user's message
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("Use this by replying to a user's message: reply then type /approve_reply")
        return

    target = update.message.reply_to_message.from_user
    whitelist_add(target.id)

    username = f"@{target.username}" if target.username else "(no username)"
    await update.message.reply_text(f"✅ Approved {target.full_name} {username} ({target.id})")

    # Optional: notify the user
    try:
        await context.bot.send_message(chat_id=target.id, text="✅ You have been approved.")
    except Exception:
        pass


def main():
    db_init()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("send", send_text))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(CommandHandler("addme", addme))
    app.add_handler(CommandHandler("approve_reply", approve_reply))

    app.run_polling()



if __name__ == "__main__":
    main()
