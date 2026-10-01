import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.error import Forbidden, TelegramError, RetryAfter
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatJoinRequestHandler,
    ContextTypes, filters
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite3")
POST_FILE_ID_KEY = "welcome_post_file_id"
POST_TYPE_KEY = "welcome_post_type"
POST_CAPTION_KEY = "welcome_post_caption"
POST_TEXT_KEY = "welcome_post_text"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("auto_dm_bot")


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with closing(db()) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                blocked INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS join_requests (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                dm_status TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.commit()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def save_user(user_id: int):
    with closing(db()) as con:
        con.execute(
            """INSERT INTO users(user_id) VALUES(?)
               ON CONFLICT(user_id) DO UPDATE SET
               last_seen=CURRENT_TIMESTAMP, blocked=0""",
            (user_id,),
        )
        con.commit()


def bump(key: str, amount: int = 1):
    with closing(db()) as con:
        con.execute(
            """INSERT INTO stats(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value=value+excluded.value""",
            (key, amount),
        )
        con.commit()


def get_stat(key: str) -> int:
    with closing(db()) as con:
        row = con.execute("SELECT value FROM stats WHERE key=?", (key,)).fetchone()
        return row[0] if row else 0


def get_counts():
    with closing(db()) as con:
        started = con.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]
        blocked = con.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
        requests = con.execute("SELECT COUNT(*) FROM join_requests").fetchone()[0]
        return started, blocked, requests


def set_blocked(user_id: int):
    with closing(db()) as con:
        con.execute("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))
        con.commit()


def save_join_request(user_id: int, chat_id: int, status: str):
    with closing(db()) as con:
        con.execute(
            "INSERT INTO join_requests(user_id, chat_id, dm_status) VALUES(?, ?, ?)",
            (user_id, chat_id, status),
        )
        con.commit()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.effective_message:
        return
    save_user(user.id)
    await update.effective_message.reply_text(
        "Hi! You're connected to the bot. You'll receive channel updates here. "
        "Use /help to see available commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Commands:\n"
        "/start — register with the bot\n"
        "/help — show this help\n"
        "/stats — bot statistics (admin only)\n"
        "/setpost — reply with the message to use as the welcome post\n"
        "/previewpost — send a preview of the saved welcome post\n"
        "/clearpost — remove the saved welcome post\n"
        "/broadcast — reply with the message/media to broadcast (admin only)\n\n"
        "To configure a welcome post: send /setpost, then send the text/photo/video/GIF "
        "as your next message. You can include a caption and supported Telegram formatting."
    )
    await update.effective_message.reply_text(msg)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    started, blocked, requests = get_counts()
    await update.effective_message.reply_text(
        "📊 Bot stats\n"
        f"Users who started the bot: {started}\n"
        f"Blocked/unreachable users: {blocked}\n"
        f"Join requests seen: {requests}\n"
        f"Welcome DMs attempted: {get_stat('welcome_attempted')}\n"
        f"Welcome DMs sent: {get_stat('welcome_sent')}\n"
        f"Welcome DMs failed: {get_stat('welcome_failed')}\n"
        f"Broadcast messages sent: {get_stat('broadcast_sent')}\n"
        f"Broadcast deliveries: {get_stat('broadcast_delivered')}\n"
        f"Broadcast failures: {get_stat('broadcast_failed')}"
    )


async def setpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    context.user_data["awaiting_welcome_post"] = True
    await update.effective_message.reply_text(
        "Now send the welcome post as your next message: text, photo, video, animation/GIF, "
        "or another Telegram message. I'll save it as the auto-DM post."
    )


async def clearpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    for key in (POST_FILE_ID_KEY, POST_TYPE_KEY, POST_CAPTION_KEY, POST_TEXT_KEY):
        context.application.bot_data.pop(key, None)
    persist_post(context)
    await update.effective_message.reply_text("Welcome post cleared.")


async def previewpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    if not await send_saved_post(context, update.effective_chat.id):
        await update.effective_message.reply_text("No welcome post saved. Use /setpost first.")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    context.user_data["awaiting_broadcast"] = True
    await update.effective_message.reply_text(
        "Send the next message to broadcast to users who have started the bot. "
        "Supported: text, photo, video, GIF/animation, document, or audio. "
        "Send /cancel to cancel."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting_broadcast", None)
    context.user_data.pop("awaiting_welcome_post", None)
    await update.effective_message.reply_text("Cancelled.")


async def send_saved_post(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    data = context.application.bot_data
    post_type = data.get(POST_TYPE_KEY)
    file_id = data.get(POST_FILE_ID_KEY)
    caption = data.get(POST_CAPTION_KEY, "")
    text = data.get(POST_TEXT_KEY, "")
    try:
        if post_type == "text":
            await context.bot.send_message(chat_id=chat_id, text=text)
        elif post_type == "photo":
            await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
        elif post_type == "video":
            await context.bot.send_video(chat_id=chat_id, video=file_id, caption=caption)
        elif post_type == "animation":
            await context.bot.send_animation(chat_id=chat_id, animation=file_id, caption=caption)
        elif post_type == "document":
            await context.bot.send_document(chat_id=chat_id, document=file_id, caption=caption)
        elif post_type == "audio":
            await context.bot.send_audio(chat_id=chat_id, audio=file_id, caption=caption)
        elif post_type == "voice":
            await context.bot.send_voice(chat_id=chat_id, voice=file_id, caption=caption)
        elif post_type == "video_note":
            await context.bot.send_video_note(chat_id=chat_id, video_note=file_id)
        elif post_type == "sticker":
            await context.bot.send_sticker(chat_id=chat_id, sticker=file_id)
        else:
            return False
        return True
    except TelegramError:
        log.exception("Could not send saved post to chat_id=%s", chat_id)
        return False


async def handle_admin_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not is_admin(user.id):
        return

    if context.user_data.get("awaiting_welcome_post"):
        if message.text and message.text.startswith("/"):
            return
        data = context.application.bot_data
        data.pop(POST_FILE_ID_KEY, None)
        data.pop(POST_TYPE_KEY, None)
        data.pop(POST_CAPTION_KEY, None)
        data.pop(POST_TEXT_KEY, None)

        if message.photo:
            data[POST_TYPE_KEY] = "photo"
            data[POST_FILE_ID_KEY] = message.photo[-1].file_id
            data[POST_CAPTION_KEY] = message.caption or ""
        elif message.video:
            data[POST_TYPE_KEY] = "video"
            data[POST_FILE_ID_KEY] = message.video.file_id
            data[POST_CAPTION_KEY] = message.caption or ""
        elif message.animation:
            data[POST_TYPE_KEY] = "animation"
            data[POST_FILE_ID_KEY] = message.animation.file_id
            data[POST_CAPTION_KEY] = message.caption or ""
        elif message.document:
            data[POST_TYPE_KEY] = "document"
            data[POST_FILE_ID_KEY] = message.document.file_id
            data[POST_CAPTION_KEY] = message.caption or ""
        elif message.audio:
            data[POST_TYPE_KEY] = "audio"
            data[POST_FILE_ID_KEY] = message.audio.file_id
            data[POST_CAPTION_KEY] = message.caption or ""
        elif message.voice:
            data[POST_TYPE_KEY] = "voice"
            data[POST_FILE_ID_KEY] = message.voice.file_id
            data[POST_CAPTION_KEY] = message.caption or ""
        elif message.video_note:
            data[POST_TYPE_KEY] = "video_note"
            data[POST_FILE_ID_KEY] = message.video_note.file_id
        elif message.sticker:
            data[POST_TYPE_KEY] = "sticker"
            data[POST_FILE_ID_KEY] = message.sticker.file_id
        elif message.text:
            data[POST_TYPE_KEY] = "text"
            data[POST_TEXT_KEY] = message.text
        else:
            await message.reply_text("That message type isn't supported. Try text, photo, video, or GIF.")
            return

        context.user_data.pop("awaiting_welcome_post", None)
        await message.reply_text("✅ Welcome post saved. Use /previewpost to test it.")
        return

    if context.user_data.get("awaiting_broadcast"):
        if message.text and message.text.startswith("/"):
            return
        context.user_data.pop("awaiting_broadcast", None)
        with closing(db()) as con:
            rows = con.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()
        targets = [row[0] for row in rows]
        if not targets:
            await message.reply_text("No registered users to broadcast to.")
            return

        await message.reply_text(f"Broadcast started for {len(targets)} registered users.")
        sent = failed = 0
        for target_id in targets:
            try:
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                sent += 1
                bump("broadcast_delivered")
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
                try:
                    await context.bot.copy_message(
                        chat_id=target_id,
                        from_chat_id=message.chat_id,
                        message_id=message.message_id,
                    )
                    sent += 1
                    bump("broadcast_delivered")
                except TelegramError:
                    failed += 1
                    bump("broadcast_failed")
            except Forbidden:
                set_blocked(target_id)
                failed += 1
                bump("broadcast_failed")
            except TelegramError:
                failed += 1
                bump("broadcast_failed")
            await asyncio.sleep(0.05)
        bump("broadcast_sent")
        await message.reply_text(f"Broadcast complete.\nDelivered: {sent}\nFailed: {failed}")
        return


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    if not request:
        return
    user_id = request.from_user.id
    chat_id = request.chat.id
    bump("join_requests")
    bump("welcome_attempted")

    # Store the join request for counting even if the user has never /started the bot.
    try:
        ok = await send_saved_post(context, user_id)
    except Exception:
        log.exception("Unexpected error sending join-request DM")
        ok = False

    status = "sent" if ok else "failed"
    save_join_request(user_id, chat_id, status)
    bump("welcome_sent" if ok else "welcome_failed")


async def post_init(application: Application):
    # Persist the welcome template in SQLite so it survives VPS restarts.
    with closing(db()) as con:
        rows = dict(con.execute("SELECT key, value FROM stats WHERE key LIKE 'post_%'").fetchall())
        con.commit()
    # Saved template is held in a separate table (created below).
    with closing(db()) as con:
        row = con.execute("SELECT post_type, file_id, caption, text FROM welcome_post WHERE id=1").fetchone()
    if row:
        post_type, file_id, caption, text = row
        application.bot_data[POST_TYPE_KEY] = post_type
        if file_id:
            application.bot_data[POST_FILE_ID_KEY] = file_id
        application.bot_data[POST_CAPTION_KEY] = caption or ""
        application.bot_data[POST_TEXT_KEY] = text or ""


def init_post_table():
    with closing(db()) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS welcome_post (
                id INTEGER PRIMARY KEY CHECK (id=1),
                post_type TEXT NOT NULL,
                file_id TEXT,
                caption TEXT,
                text TEXT
            )
        """)
        con.commit()


def persist_post(context: ContextTypes.DEFAULT_TYPE):
    data = context.application.bot_data
    post_type = data.get(POST_TYPE_KEY)
    if not post_type:
        with closing(db()) as con:
            con.execute("DELETE FROM welcome_post WHERE id=1")
            con.commit()
        return
    with closing(db()) as con:
        con.execute(
            """INSERT INTO welcome_post(id, post_type, file_id, caption, text)
               VALUES(1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET post_type=excluded.post_type,
               file_id=excluded.file_id, caption=excluded.caption, text=excluded.text""",
            (post_type, data.get(POST_FILE_ID_KEY), data.get(POST_CAPTION_KEY, ""),
             data.get(POST_TEXT_KEY, "")),
        )
        con.commit()


async def handle_admin_content_persistent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    before = context.application.bot_data.get(POST_TYPE_KEY)
    await handle_admin_content(update, context)
    after = context.application.bot_data.get(POST_TYPE_KEY)
    if before != after or (after and update.effective_user and is_admin(update.effective_user.id)):
        # Only write the saved template when a welcome post was just configured/cleared.
        if update.effective_user and is_admin(update.effective_user.id):
            if not context.user_data.get("awaiting_welcome_post"):
                persist_post(context)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and add your token.")
    if not ADMIN_IDS:
        raise SystemExit("ADMIN_IDS is missing. Add your numeric Telegram user ID to .env.")

    init_db()
    init_post_table()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("setpost", setpost))
    app.add_handler(CommandHandler("previewpost", previewpost))
    app.add_handler(CommandHandler("clearpost", clearpost))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_admin_content_persistent))

    log.info("Bot is starting...")
    app.run_polling(allowed_updates=["message", "chat_join_request"])


if __name__ == "__main__":
    main()
