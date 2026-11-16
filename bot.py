import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_IDS = {
    int(x.strip())
    for x in os.environ.get("OWNER_ID", "").split(",")
    if x.strip().isdigit()
}
DEFAULT_ACCEPT_MSG = os.environ.get(
    "ACCEPT_MSG",
    "✅ Your join request has been accepted! Welcome aboard.",
).strip()
ENV_PROMO_TEXT = os.environ.get("PROMO_POST_TEXT", "").strip()
ENV_PROMO_BUTTONS = os.environ.get("PROMO_BUTTONS", "").strip()

CONFIG_FILE = "config.json"


def load_config() -> dict:
    default = {
        "accept_msg": DEFAULT_ACCEPT_MSG,
        "promo_chat_id": None,
        "promo_message_id": None,
        "accepted_count": 0,
        "dm_sent_count": 0,
        "dm_failed_count": 0,
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default.update(data)
        except Exception as e:
            logger.warning("Failed to load config.json: %s", e)
    return default


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Failed to save config.json: %s", e)


config = load_config()


def is_owner(user_id: int | None) -> bool:
    if not OWNER_IDS:
        return False
    return user_id in OWNER_IDS


def parse_env_buttons(raw: str) -> InlineKeyboardMarkup | None:
    if not raw:
        return None
    rows = []
    for item in raw.split(";;"):
        item = item.strip()
        if "|" in item:
            label, url = item.split("|", 1)
            label = label.strip()
            url = url.strip()
            if label and url.startswith(("http://", "https://", "tg://")):
                rows.append([InlineKeyboardButton(label, url=url)])
    return InlineKeyboardMarkup(rows) if rows else None


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server() -> None:
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server listening on port %s", port)


async def send_welcome_and_promo(
    bot, user_id: int
) -> tuple[bool, str | None]:
    accept_msg = config.get("accept_msg") or DEFAULT_ACCEPT_MSG
    try:
        await bot.send_message(chat_id=user_id, text=accept_msg)
    except Forbidden:
        return False, "User blocked the bot or DMs are closed."
    except TelegramError as e:
        return False, str(e)

    promo_chat_id = config.get("promo_chat_id")
    promo_message_id = config.get("promo_message_id")

    if promo_chat_id and promo_message_id:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=promo_chat_id,
                message_id=promo_message_id,
            )
            return True, None
        except TelegramError as e:
            logger.warning("copy_message promo failed: %s", e)

    if ENV_PROMO_TEXT or ENV_PROMO_BUTTONS:
        markup = parse_env_buttons(ENV_PROMO_BUTTONS)
        text = ENV_PROMO_TEXT or "📢 Join our other channels:"
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            logger.warning("env promo send failed: %s", e)

    return True, None


async def handle_join_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    req = update.chat_join_request
    if not req:
        return

    user = req.from_user
    chat = req.chat

    try:
        await req.approve()
        config["accepted_count"] = config.get("accepted_count", 0) + 1
        logger.info("Approved join request from %s in %s", user.id, chat.id)
    except TelegramError as e:
        logger.error("Failed to approve join request for %s: %s", user.id, e)
        return

    ok, err = await send_welcome_and_promo(context.bot, user.id)
    if ok:
        config["dm_sent_count"] = config.get("dm_sent_count", 0) + 1
    else:
        config["dm_failed_count"] = config.get("dm_failed_count", 0) + 1
        logger.info("Could not DM %s: %s", user.id, err)

    save_config(config)


async def cmd_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    bot_username = context.bot.username

    if user and is_owner(user.id):
        promo_status = (
            "Active (/setpost)"
            if config.get("promo_message_id")
            else ("Active (ENV)" if (ENV_PROMO_TEXT or ENV_PROMO_BUTTONS) else "Not set yet")
        )
        text = (
            "👋 *Auto-Accept + Promo Bot Ready!*\n\n"
            f"• *1st Message (Accept):* `{config.get('accept_msg')}`\n"
            f"• *2nd Message (Channels Post):* {promo_status}\n\n"
            "*Admin Commands:*\n"
            "• `/setmsg <text>` — Acceptance message change karein\n"
            "• `/setpost` — Kisi bhi post/photo/buttons ko reply karke 2nd message set karein\n"
            "• `/clearpost` — Saved 2nd post hatayein\n"
            "• `/testpost` — Khud ke DM mein preview dekhein\n"
            "• `/stats` — Total accepted requests dekhein"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Add to Your Channel",
                url=f"https://t.me/{bot_username}?startchannel=true&admin=invite_users",
            )
        ],
        [
            InlineKeyboardButton(
                "➕ Add to Your Group",
                url=f"https://t.me/{bot_username}?startgroup=true&admin=invite_users",
            )
        ],
    ]
    await update.message.reply_text(
        "👋 Hello! Add me as an Admin with *Invite Users via Link* permission to automatically accept join requests.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def cmd_setmsg(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_user or not is_owner(update.effective_user.id):
        return
    new_msg = " ".join(context.args).strip()
    if not new_msg:
        await update.message.reply_text(
            "Usage: `/setmsg ✅ Aapki request accept ho gayi hai!`",
            parse_mode="Markdown",
        )
        return
    config["accept_msg"] = new_msg
    save_config(config)
    await update.message.reply_text(f"✅ 1st message updated:\n\n{new_msg}")


async def cmd_setpost(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_user or not is_owner(update.effective_user.id):
        return
    replied = update.message.reply_to_message
    if not replied:
        await update.message.reply_text(
            "📌 Pehle woh post (text, photo, links, ya buttons ke sath) is chat mein bhejein, "
            "phir us message par *Reply* karke `/setpost` likhein.",
            parse_mode="Markdown",
        )
        return

    config["promo_chat_id"] = replied.chat_id
    config["promo_message_id"] = replied.message_id
    save_config(config)
    await update.message.reply_text(
        "✅ 2nd message (Channels list post) save ho gaya! Check karne ke liye `/testpost` bhejein.",
        parse_mode="Markdown",
    )


async def cmd_clearpost(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_user or not is_owner(update.effective_user.id):
        return
    config["promo_chat_id"] = None
    config["promo_message_id"] = None
    save_config(config)
    await update.message.reply_text("🗑️ Saved 2nd post clear kar diya gaya hai.")


async def cmd_testpost(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_user or not is_owner(update.effective_user.id):
        return
    await update.message.reply_text("🔍 Preview bhej raha hoon:")
    await send_welcome_and_promo(context.bot, update.effective_user.id)


async def cmd_stats(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_user or not is_owner(update.effective_user.id):
        return
    text = (
        "📊 *Bot Stats*\n\n"
        f"• Total Accepted: *{config.get('accepted_count', 0)}*\n"
        f"• DMs Sent: *{config.get('dm_sent_count', 0)}*\n"
        f"• DMs Failed: *{config.get('dm_failed_count', 0)}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    start_health_server()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setmsg", cmd_setmsg))
    app.add_handler(CommandHandler("setpost", cmd_setpost))
    app.add_handler(CommandHandler("clearpost", cmd_clearpost))
    app.add_handler(CommandHandler("testpost", cmd_testpost))
    app.add_handler(CommandHandler("stats", cmd_stats))

    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
