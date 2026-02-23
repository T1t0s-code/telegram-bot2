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

# Each post (broadcast) has an id; each user can request text up to 2 times per post.
MAX_SENDS_PER_POST = 2
current_post_id: int = 0
pending_text: Optional[str] = None


# -------------------- DB --------------------
def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            sent_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (post_id, user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS post_recipients (
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (post_id, user_id)
        )
    """)

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


def meta_get_int(key: str, default: int = 0) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT value FROM meta WHERE key = ? LIMIT 1", (key,))
    row = cur.fetchone()
    con.close()
    if not row:
        return default
    try:
        return int(row[0])
    except Exception:
        return default


def meta_set_int(key: str, value: int) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    con.commit()
    con.close()


def start_new_post() -> int:
    post_id = meta_get_int("current_post_id", 0) + 1
    meta_set_int("current_post_id", post_id)
    return post_id


def get_sent_count(post_id: int, user_id: int) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT sent_count FROM deliveries WHERE post_id = ? AND user_id = ?",
        (post_id, user_id),
    )
    row = cur.fetchone()
    con.close()
    return int(row[0]) if row else 0


def increment_sent_count(post_id: int, user_id: int) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO deliveries(post_id, user_id, sent_count) VALUES(?, ?, 1) "
        "ON CONFLICT(post_id, user_id) DO UPDATE SET sent_count = sent_count + 1",
        (post_id, user_id),
    )
    cur.execute(
        "SELECT sent_count FROM deliveries WHERE post_id = ? AND user_id = ?",
        (post_id, user_id),
    )
    row = cur.fetchone()
    con.commit()
    con.close()
    return int(row[0]) if row else 1


def recipients_set(post_id: int, user_ids: Set[int]) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM post_recipients WHERE post_id = ?", (post_id,))
    cur.executemany(
        "INSERT OR IGNORE INTO post_recipients(post_id, user_id) VALUES(?, ?)",
        [(post_id, uid) for uid in user_ids],
    )
    con.commit()
    con.close()


def recipients_get(post_id: int) -> Set[int]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT user_id FROM post_recipients WHERE post_id = ? ORDER BY user_id ASC", (post_id,))
    rows = cur.fetchall()
    con.close()
    return {int(r[0]) for r in rows}


def deliveries_count(post_id: int) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM deliveries WHERE post_id = ? AND sent_count > 0", (post_id,))
    row = cur.fetchone()
    con.close()
    return int(row[0]) if row else 0


# -------------------- UI --------------------
def inline_send_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📩 Send", callback_data="GET_TEXT")]])


def reply_send_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("/send")]], resize_keyboard=True)


# -------------------- ADMIN COMMANDS --------------------
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("Usage: /approve USER_ID")
        return

    if not context.args[0].isdigit():
        await update.message.reply_text("Usage: /approve USER_ID (numeric)")
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
        await update.message.reply_text(f"✅ Removed {user_id} from whitelist.")
    else:
        await update.message.reply_text(f"⚠️ {user_id} was not in the whitelist.")


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    users = whitelist_all()
    await update.message.reply_text("\n".join(str(u) for u in sorted(users)) or "Empty")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    global current_post_id

    wl = whitelist_all()
    if current_post_id <= 0:
        await update.message.reply_text(
            f"Status:\n- Current post: none\n- Whitelist users: {len(wl)}"
        )
        return

    recips = recipients_get(current_post_id)
    requested = deliveries_count(current_post_id)
    not_requested = sorted(list(recips - {
        uid for uid in recips if get_sent_count(current_post_id, uid) > 0
    }))

    msg = (
        f"Status:\n"
        f"- Current post: #{current_post_id}\n"
        f"- Whitelist users: {len(wl)}\n"
        f"- Photo recipients (this post): {len(recips)}\n"
        f"- Users who requested line: {requested}\n"
        f"- Not requested yet: {not_requested if not_requested else 'none'}"
    )
    await update.message.reply_text(msg)


# -------------------- USER COMMANDS --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Press 📩 Send (or /send) to receive the latest line (approved users only).",
        reply_markup=inline_send_keyboard(),
    )


async def send_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_post_id, pending_text

    uid = update.effective_user.id

    if not whitelist_has(uid):
        await update.message.reply_text("❌ You are not approved.")
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 Non-whitelisted user tried /send: {uid}")
        return

    if current_post_id <= 0:
        await update.message.reply_text("No active post yet.")
        return

    if not pending_text:
        await update.message.reply_text("No line saved yet.")
        return

    count = get_sent_count(current_post_id, uid)
    if count >= MAX_SENDS_PER_POST:
        await update.message.reply_text(f"Already sent ({MAX_SENDS_PER_POST}/{MAX_SENDS_PER_POST}) for Post #{current_post_id}.")
        return

    new_count = increment_sent_count(current_post_id, uid)
    await update.message.reply_text(pending_text)
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"✅ Sent line to {uid} for Post #{current_post_id} ({new_count}/{MAX_SENDS_PER_POST}) via /send",
    )


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_post_id, pending_text

    query = update.callback_query
    uid = query.from_user.id

    if not whitelist_has(uid):
        await query.answer("❌ You are not approved.", show_alert=True)
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 Non-whitelisted user pressed button: {uid}")
        return

    if current_post_id <= 0:
        await query.answer("No active post yet.", show_alert=True)
        return

    if not pending_text:
        await query.answer("No line saved yet.", show_alert=True)
        return

    count = get_sent_count(current_post_id, uid)
    if count >= MAX_SENDS_PER_POST:
        await query.answer(f"Already sent ({MAX_SENDS_PER_POST}/{MAX_SENDS_PER_POST}) for this post.", show_alert=True)
        return

    await query.answer()
    new_count = increment_sent_count(current_post_id, uid)
    await query.message.reply_text(pending_text)
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"✅ Sent line to {uid} for Post #{current_post_id} ({new_count}/{MAX_SENDS_PER_POST}) via button",
    )


# -------------------- BROADCAST --------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    global pending_text, current_post_id

    caption = (update.message.caption or "").strip()
    if caption:
        pending_text = caption

    photo_file_id = update.message.photo[-1].file_id

    users = whitelist_all()
    if not users:
        await update.message.reply_text("Whitelist is empty. Add users with /approve first.")
        return
async def reset_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    meta_set_int("current_post_id", 0)

    global current_post_id
    current_post_id = 0

    await update.message.reply_text("✅ Post ID counter reset. Next broadcast will be Post #1.")

    
    # New post each broadcast
    current_post_id = start_new_post()
    recipients_set(current_post_id, users)

    sent_to = []
    failed_to = []

    caption_text = f"Post #{current_post_id}\nTap 📩 Send or use /send to get the line."

    for uid in sorted(users):
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

    msg = f"📸 Broadcast complete (Post #{current_post_id}).\n✅ Sent to: {sent_to if sent_to else 'none'}"
    if failed_to:
        msg += f"\n⚠️ Failed: {failed_to} (they may not have started the bot or blocked it)"
    if caption:
        msg += "\n📝 Line updated from photo caption."
    else:
        msg += "\n📝 No caption. Send a normal text message now to set the line."

    await update.message.reply_text(msg)


async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    global pending_text
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    pending_text = text
    await update.message.reply_text("✅ Line saved. Users can press 📩 Send (or /send).")


# -------------------- APPROVAL HELPERS --------------------
async def addme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    name = user.full_name
    username = f"@{user.username}" if user.username else "(no username)"

    await update.message.reply_text("Request sent to admin. Wait for approval.")

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "📥 Approval request\n"
            f"ID: {uid}\n"
            f"Name: {name}\n"
            f"Username: {username}\n\n"
            "Approve with:\n"
            f"/approve {uid}"
        ),
    )


async def approve_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("Use this by replying to a user's message: reply then type /approve_reply")
        return

    target = update.message.reply_to_message.from_user
    whitelist_add(target.id)

    username = f"@{target.username}" if target.username else "(no username)"
    await update.message.reply_text(f"✅ Approved {target.full_name} {username} ({target.id})")

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
    app.add_handler(CommandHandler("status", status_cmd))

    app.add_handler(CommandHandler("addme", addme))
    app.add_handler(CommandHandler("approve_reply", approve_reply))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))

    app.add_handler(CallbackQueryHandler(button))

    app.run_polling()


if __name__ == "__main__":
    main()
