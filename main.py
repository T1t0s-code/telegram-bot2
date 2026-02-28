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
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "").strip()

if not ADMIN_IDS_RAW:
    raise RuntimeError("ADMIN_IDS missing in Railway Variables")

ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}

DB_PATH = "bot.db"

# Simple post counter (increments on each broadcast). Reset with /resetposts.
current_post_id: int = 0
pending_text: Optional[str] = None


# -------------------- DB --------------------
def db_init():
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()

        cur.execute("CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Store user labels (name/username) so /list can show them
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT
            )
        """)

        # Ensure counter exists
        cur.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('current_post_id', '0')")

        con.commit()
    finally:
        con.close()


def meta_get_int(key: str, default: int = 0) -> int:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("SELECT value FROM meta WHERE key = ? LIMIT 1", (key,))
        row = cur.fetchone()
    finally:
        con.close()

    if not row:
        return default
    try:
        return int(row[0])
    except Exception:
        return default


def meta_set_int(key: str, value: int) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        con.commit()
    finally:
        con.close()


def start_new_post() -> int:
    post_id = meta_get_int("current_post_id", 0) + 1
    meta_set_int("current_post_id", post_id)
    return post_id


def whitelist_add(user_id: int):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (user_id,))
        con.commit()
    finally:
        con.close()


def whitelist_remove(user_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
        changed = cur.rowcount > 0
        con.commit()
        return changed
    finally:
        con.close()


def whitelist_all() -> Set[int]:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("SELECT user_id FROM whitelist ORDER BY user_id ASC")
        rows = cur.fetchall()
        return {int(r[0]) for r in rows}
    finally:
        con.close()


def whitelist_has(user_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("SELECT 1 FROM whitelist WHERE user_id = ? LIMIT 1", (user_id,))
        row = cur.fetchone()
        return row is not None
    finally:
        con.close()


def upsert_user(user_id: int, full_name: Optional[str], username: Optional[str]) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO users(user_id, full_name, username)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name=excluded.full_name,
                username=excluded.username
            """,
            (user_id, (full_name or "").strip(), (username or "").strip()),
        )
        con.commit()
    finally:
        con.close()


def get_user_label(user_id: int) -> str:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.cursor()
        cur.execute("SELECT full_name, username FROM users WHERE user_id = ? LIMIT 1", (user_id,))
        row = cur.fetchone()
    finally:
        con.close()

    if not row:
        return f"{user_id}"

    full_name, username = row
    full_name = (full_name or "").strip()
    username = (username or "").strip()

    if username and not username.startswith("@"):
        username = "@" + username

    label = f"{user_id}"
    if full_name:
        label += f" - {full_name}"
    if username:
        label += f" {username}"
    return label


# -------------------- ADMIN COMMANDS --------------------
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /approve USER_ID")
        return

    whitelist_add(int(context.args[0]))
    await update.message.reply_text("User approved.")


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /remove USER_ID")
        return

    user_id = int(context.args[0])
    removed = whitelist_remove(user_id)

    if removed:
        await update.message.reply_text(f"✅ Removed {get_user_label(user_id)} from whitelist.")
    else:
        await update.message.reply_text(f"⚠️ {get_user_label(user_id)} was not in the whitelist.")


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    users = sorted(whitelist_all())
    if not users:
        await update.message.reply_text("Empty")
        return

    lines = [get_user_label(uid) for uid in users]
    await update.message.reply_text("\n".join(lines))


async def reset_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    meta_set_int("current_post_id", 0)
    global current_post_id
    current_post_id = 0
    await update.message.reply_text("✅ Post ID counter reset. Next broadcast will be Post #1.")


async def post_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(f"Current post id: {current_post_id}")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast your message here")
        return

    users = sorted(whitelist_all())
    if not users:
        await update.message.reply_text("Whitelist is empty.")
        return

    sent_to = []
    failed_to = []

    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent_to.append(uid)
        except Exception:
            failed_to.append(uid)

    sent_labels = [get_user_label(u) for u in sent_to]
    failed_labels = [get_user_label(u) for u in failed_to]

    msg = f"📢 Broadcast message sent.\n✅ Sent to: {sent_labels if sent_labels else 'none'}"
    if failed_labels:
        msg += f"\n⚠️ Failed: {failed_labels}"
    await update.message.reply_text(msg)


# -------------------- UI --------------------
def inline_send_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📩 Send", callback_data="GET_TEXT")]])


def reply_send_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("/send")]], resize_keyboard=True)


# -------------------- USER COMMANDS --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.full_name, user.username)

    await update.message.reply_text(
        "Press 📩 Send (or /send) to receive the latest text (approved users only).",
        reply_markup=inline_send_keyboard(),
    )


async def send_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    upsert_user(uid, user.full_name, user.username)

    if not whitelist_has(uid):
        await update.message.reply_text("❌ You are not approved.")
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 Non-whitelisted user tried /send: {get_user_label(uid)}")
        return

    if not pending_text:
        await update.message.reply_text("No text saved yet.")
        return

    await update.message.reply_text(pending_text)
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Sent text to approved user {get_user_label(uid)} via /send")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    uid = user.id
    upsert_user(uid, user.full_name, user.username)

    if not whitelist_has(uid):
        await query.answer("❌ You are not approved.", show_alert=True)
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 Non-whitelisted user tried button: {get_user_label(uid)}")
        return

    if not pending_text:
        await query.answer("No text saved yet.", show_alert=True)
        return

    await query.answer()
    await query.message.reply_text(pending_text)
    await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ Sent text to approved user {get_user_label(uid)}")


# -------------------- BROADCAST --------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    global pending_text, current_post_id

    caption = (update.message.caption or "").strip()
    if caption:
        pending_text = caption

    # Support both normal photo and "send as file"
    photo_file_id = None
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
    elif (
        update.message.document
        and update.message.document.mime_type
        and update.message.document.mime_type.startswith("image/")
    ):
        photo_file_id = update.message.document.file_id

    if not photo_file_id:
        await update.message.reply_text("Send a photo or an image file.")
        return

    users = sorted(whitelist_all())
    if not users:
        await update.message.reply_text("Whitelist is empty. Add users with /approve first.")
        return

    current_post_id = start_new_post()

    sent_to = []
    failed_to = []

    caption_text = f"Post #{current_post_id}\nTap 📩 Send or use /send to get the text."

    for uid in users:
        try:
            await context.bot.send_photo(
                chat_id=uid,
                photo=photo_file_id,
                caption=caption_text,
                reply_markup=inline_send_keyboard(),
            )
            sent_to.append(uid)
        except Exception:
            failed_to.append(uid)

    sent_labels = [get_user_label(u) for u in sent_to]
    failed_labels = [get_user_label(u) for u in failed_to]

    msg = f"📸 Broadcast complete (Post #{current_post_id}).\n✅ Sent to: {sent_labels if sent_labels else 'none'}"
    if failed_labels:
        msg += f"\n⚠️ Failed: {failed_labels} (they may not have started the bot or blocked it)"
    if caption:
        msg += "\n📝 Text updated from photo caption."
    else:
        msg += "\n📝 No caption. Send a normal text message now to set the text."

    await update.message.reply_text(msg)


# -------------------- APPROVAL HELPERS --------------------
async def addme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    upsert_user(uid, user.full_name, user.username)

    username = f"@{user.username}" if user.username else "(no username)"

    await update.message.reply_text("Request sent to admin. Wait for approval.")

    await context.bot.send_message(
       for admin_id in ADMIN_IDS:
    await context.bot.send_message(chat_id=admin_id, text="..."),
        text=(
            "📥 Approval request\n"
            f"ID: {uid}\n"
            f"Name: {user.full_name}\n"
            f"Username: {username}\n\n"
            "Approve with:\n"
            f"/approve {uid}\n"
            "OR reply to one of their messages with /approve_reply"
        ),
    )


async def approve_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("Use this by replying to a user's message: reply then type /approve_reply")
        return

    target = update.message.reply_to_message.from_user
    upsert_user(target.id, target.full_name, target.username)
    whitelist_add(target.id)

    await update.message.reply_text(f"✅ Approved {get_user_label(target.id)}")

    try:
        await context.bot.send_message(chat_id=target.id, text="✅ You have been approved.")
    except Exception:
        pass


# -------------------- MAIN --------------------
def main():
    global current_post_id
    db_init()
    current_post_id = meta_get_int("current_post_id", 0)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("send", send_text))

    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("resetposts", reset_posts))
    app.add_handler(CommandHandler("postid", post_id))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    app.add_handler(CommandHandler("addme", addme))
    app.add_handler(CommandHandler("approve_reply", approve_reply))

    # Photo OR image document
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))

    app.add_handler(CallbackQueryHandler(button))

    app.run_polling()


if __name__ == "__main__":
    main()
