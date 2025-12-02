import os
import time
import logging
import tempfile
import shutil
import subprocess
from datetime import datetime, timedelta

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from bson.objectid import ObjectId

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
)
from pyrogram.enums import ChatMemberStatus
from nudenet import NudeDetector

# -------------------------------------------------
# Load environment
# -------------------------------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", ""))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.75"))

# how many NSFW stickers allowed before mute
NSFW_STICKER_LIMIT = int(os.getenv("NSFW_STICKER_LIMIT", "3"))
# mute duration in seconds (default: 1 day)
MUTE_DURATION_SECONDS = int(os.getenv("MUTE_DURATION_SECONDS", "86400"))

MONGO_URI = os.getenv("MONGO_URI", "").strip()
if not MONGO_URI:
    raise SystemExit("MONGO_URI is not set in environment. Set it in your .env file.")

LOG_CHAT_ID_ENV = os.getenv("LOG_CHAT_ID", "").strip()
# Can be numeric (-100...) or @username
LOG_CHAT_ID = LOG_CHAT_ID_ENV if LOG_CHAT_ID_ENV else None

START_TIME = time.time()

# Your branding
OFFICIAL_CHANNEL = "https://t.me/DLKDevelopers"
LOG_PUBLIC_URL = "https://t.me/DOOZY_OFF"  # 🔹 Button link for logs/updates
START_PHOTO_URL = "https://i.ibb.co/WNzKw5qk/DLKNSFWCleaner.png"

DEV_ABOUT_TEXT = (
    "DLK DEVELOPER\n"
    "SEE THE FUTURE THROUGH MY VISION"
)

# -------------------------------------------------
# Logging
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
)
log = logging.getLogger("NSFW-GUARD")

# -------------------------------------------------
# MongoDB (sticker whitelist + PACK BLACKLIST + VIOLATIONS + PENDING WHITELISTS)
# -------------------------------------------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["nsfw_guard"]

# --- sticker whitelist (per-chat, per-sticker) ---
whitelist_col = db["sticker_whitelist"]
whitelist_col.create_index(
    [("chat_id", ASCENDING), ("file_unique_id", ASCENDING)],
    unique=True,
)


def is_sticker_whitelisted(chat_id: int, file_unique_id: str) -> bool:
    doc = whitelist_col.find_one({"chat_id": chat_id, "file_unique_id": file_unique_id})
    return doc is not None


def add_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.update_one(
        {"chat_id": chat_id, "file_unique_id": file_unique_id},
        {"$set": {"chat_id": chat_id, "file_unique_id": file_unique_id, "ts": int(time.time())}},
        upsert=True,
    )


# --- sticker PACK blacklist ---
pack_blacklist_col = db["sticker_pack_blacklist"]
pack_blacklist_col.create_index(
    [("chat_id", ASCENDING), ("set_name", ASCENDING)],
    unique=True,
)


def is_pack_blacklisted(chat_id: int, set_name: str) -> bool:
    """
    Check if a whole sticker pack (set_name) is blacklisted in this chat.
    """
    if not set_name:
        return False
    doc = pack_blacklist_col.find_one({"chat_id": chat_id, "set_name": set_name})
    return doc is not None


def add_pack_blacklist(chat_id: int, set_name: str) -> None:
    """
    Add a sticker pack (set_name) to blacklist in this chat.
    After this, all stickers from this pack will be deleted
    without scanning.
    """
    if not set_name:
        return
    pack_blacklist_col.update_one(
        {"chat_id": chat_id, "set_name": set_name},
        {"$set": {"chat_id": chat_id, "set_name": set_name, "ts": int(time.time())}},
        upsert=True,
    )


def remove_pack_blacklist(chat_id: int, set_name: str) -> None:
    """
    Remove a sticker pack blacklist entry for a specific chat.
    This is used when an admin explicitly whitelists a sticker from that pack
    for the chat: pack-level blacklist should no longer apply in that chat.
    """
    if not set_name:
        return
    pack_blacklist_col.delete_one({"chat_id": chat_id, "set_name": set_name})


# --- NSFW sticker violations per-user ---
violations_col = db["nsfw_violations"]
violations_col.create_index(
    [("chat_id", ASCENDING), ("user_id", ASCENDING)],
    unique=True,
)


def increment_violation(chat_id: int, user_id: int) -> int:
    """
    Increase NSFW sticker violation count for a user in a chat.
    Returns the new count.
    """
    doc = violations_col.find_one({"chat_id": chat_id, "user_id": user_id})
    if doc:
        new_count = int(doc.get("count", 0)) + 1
        violations_col.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$set": {"count": new_count}},
        )
    else:
        new_count = 1
        violations_col.insert_one(
            {"chat_id": chat_id, "user_id": user_id, "count": new_count}
        )
    return new_count


def get_violation_count(chat_id: int, user_id: int) -> int:
    doc = violations_col.find_one({"chat_id": chat_id, "user_id": user_id})
    return int(doc.get("count", 0)) if doc else 0


# --- pending whitelists (temporary, used for confirm buttons in bot private chat) ---
pending_col = db["pending_whitelists"]
# Optional: automatically expire pending requests after 1 hour
try:
    pending_col.create_index("ts", expireAfterSeconds=3600)
except Exception:
    pass


def create_pending_whitelist(chat_id: int, admin_user_id: int, file_unique_id: str, set_name: str | None) -> str:
    doc = {
        "chat_id": chat_id,
        "admin_user_id": admin_user_id,
        "file_unique_id": file_unique_id,
        "set_name": set_name or "",
        "ts": int(time.time()),
    }
    res = pending_col.insert_one(doc)
    return str(res.inserted_id)


def get_pending_whitelist(pending_id: str):
    try:
        return pending_col.find_one({"_id": ObjectId(pending_id)})
    except Exception:
        return None


def delete_pending_whitelist(pending_id: str):
    try:
        pending_col.delete_one({"_id": ObjectId(pending_id)})
    except Exception:
        pass


# -------------------------------------------------
# Pyrogram client
# -------------------------------------------------
app = Client(
    "nsfw_guard_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# -------------------------------------------------
# NSFW detector (NudeNet 3.x)
# -------------------------------------------------
log.info("Loading NudeNet detector (first run may download the model)...")
detector = NudeDetector()

# Only these labels are considered explicit NSFW
EXPLICIT_LABELS = {
    # Genitalia Exposure
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "GENITALIA_EXPOSED",
    "ANUS_EXPOSED",

    "FEMALE_BREAST_EXPOSED",
    "FEMALE_NIPPLE_EXPOSED",
    "MALE_BREAST_EXPOSED",
    "BREAST_EXPOSED",
    "NUDE_FEMALE_CHEST",
    "NUDE_MALE_CHEST",

    "BUTTOCKS_EXPOSED",
    "FEMALE_BUTTOCKS_EXPOSED",
    "MALE_BUTTOCKS_EXPOSED",

    "SEXUAL_ACTIVITY",
    "SEX_ACT",
    "SEXUAL_INTERCOURSE",
    "MASTURBATION",
    "ORAL_SEX",
    "ANAL_SEX",

    "PORNOGRAPHIC",
    "SEXUALIZED_NUDITY",
    "EXPLICIT_NUDITY",
    "ADULT_CONTENT",
    "HARDCORE",
    "SOFTCORE",
    "LEWD_CONTENT",
    "OBSCENE_CONTENT",
    "INAPPROPRIATE_CONTENT",

    "MINOR_NUDITY",
    "CHILD_NUDITY",
    "CSAM_SUSPECT",

    "ADULT_TOY",
    "SEX_TOY",
    "FETISH_CONTENT",
}



# -------------------------------------------------
# Helpers
# -------------------------------------------------
async def is_bot_admin(client: Client, chat_id: int) -> bool:
    """
    Check if the bot is admin and has delete permission.
    Uses ChatMemberStatus enum and ChatPrivileges.
    """
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)

        log.info(f"[ADMIN CHECK] status={member.status}, member={member}")

        # Must be OWNER or ADMINISTRATOR
        if member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            log.info("[ADMIN CHECK] Bot is not owner/admin.")
            return False

        privileges = getattr(member, "privileges", None)
        if privileges is not None:
            can_delete = bool(getattr(privileges, "can_delete_messages", False))
            log.info(f"[ADMIN CHECK] privileges.can_delete_messages = {can_delete}")
            return can_delete

        # Fallback (rare)
        can_delete_attr = getattr(member, "can_delete_messages", None)
        if can_delete_attr is not None:
            log.info(f"[ADMIN CHECK] legacy can_delete_messages = {can_delete_attr}")
            return bool(can_delete_attr)

        log.info("[ADMIN CHECK] No privileges or delete flag found.")
        return False
    except Exception as e:
        log.warning(f"Admin check failed: {e}")
        return False


async def is_user_admin(client: Client, chat_id: int, user_id: int) -> bool:
    """
    Check if a user is admin or owner.
    Used for commands like /free.
    """
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


async def bot_can_restrict_members(client: Client, chat_id: int) -> bool:
    """
    Check if the bot has permission to restrict/mute/ban users.
    """
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
        privileges = getattr(member, "privileges", None)
        if privileges is not None:
            can_restrict = bool(
                getattr(privileges, "can_restrict_members", False)
                or getattr(privileges, "can_restrict_users", False)
            )
            log.info(f"[ADMIN CHECK] privileges.can_restrict_members = {can_restrict}")
            return can_restrict
        return False
    except Exception as e:
        log.warning(f"Restrict permission check failed: {e}")
        return False


def extract_video_frames(src_path: str, temp_dir: str, max_frames: int = 3) -> list:
    """
    Extract a few frames from videos/GIFs using ffmpeg.
    """
    frames = []
    try:
        out_pattern = os.path.join(temp_dir, "frame_%03d.jpg")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            src_path,
            "-vf",
            "fps=1",
            "-vframes",
            str(max_frames),
            out_pattern,
        ]
        subprocess.run(cmd, check=True)

        for fname in sorted(os.listdir(temp_dir)):
            if fname.lower().endswith(".jpg"):
                frames.append(os.path.join(temp_dir, fname))
    except Exception as e:
        log.warning(f"Frame extract failed: {e}")

    return frames


def convert_tgs_to_png(tgs_path: str, out_path: str) -> str | None:
    """
    Convert an animated .tgs sticker to a PNG (first frame).
    Requires: pip install lottie
    """
    try:
        from lottie import importers, exporters

        with open(tgs_path, "rb") as f:
            animation = importers.tgs.import_tgs(f)

        exporters.export_png(animation, out_path)
        return out_path
    except Exception as e:
        log.warning(f"TGS convert failed (treating as safe): {e}")
        return None


def scan_images_for_nsfw(image_paths: list[str]) -> float:
    """
    Scan a list of image paths with NudeNet NudeDetector.
    Only EXPLICIT_LABELS are counted as NSFW.
    """
    if not image_paths:
        return 0.0

    max_score = 0.0

    for path in image_paths:
        try:
            detections = detector.detect(path)
            log.info(f"[DETECT] {path} -> {detections}")

            for det in detections:
                label = str(det.get("class", "")).upper()
                score = float(det.get("score", 0.0))

                if label in EXPLICIT_LABELS:
                    log.info(f"[DETECT] EXPLICIT HIT label={label}, score={score:.2f}")
                    if score > max_score:
                        max_score = score
                else:
                    log.debug(f"[DETECT] Ignoring non-explicit label={label}, score={score:.2f}")
        except Exception as e:
            log.warning(f"Scanning failed for {path}: {e}")
            continue

    return max_score


async def delete_nsfw_message(client: Client, message: Message, score: float):
    """
    Delete NSFW message silently in the group.
    Optionally send log to LOG_CHAT_ID.
    """
    chat = message.chat
    user = message.from_user

    try:
        await message.delete()
        log.info(
            f"[DELETE] NSFW content deleted in chat={chat.id}, "
            f"user={user.id if user else 'N/A'}, score={score:.2f}"
        )
    except Exception as e:
        log.warning(f"Failed to delete NSFW message: {e}")

    if LOG_CHAT_ID:
        try:
            text = (
                "🔍 NSFW content deleted\n\n"
                f"👤 User: {user.mention if user else 'Unknown'}\n"
                f"💬 Chat: {chat.title or chat.id}\n"
                f"📊 Score: <code>{score:.2f}</code>\n"
                f"🆔 Chat ID: <code>{chat.id}</code>\n"
                f"🆔 User ID: <code>{user.id if user else 'N/A'}</code>"
            )
            await client.send_message(
                LOG_CHAT_ID,
                text,
            )
        except Exception as e:
            log.warning(f"Failed to send log message: {e}")


def format_uptime(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def build_main_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add me to your group",
                    url=f"https://t.me/{bot_username}?startgroup=nsfw_guard",
                )
            ],
            [
                InlineKeyboardButton("📖 How to use", callback_data="page_how"),
                InlineKeyboardButton("🛠 Features", callback_data="page_features"),
            ],
            [
                InlineKeyboardButton("🔐 Permissions", callback_data="page_perms"),
                InlineKeyboardButton("ℹ️ About", callback_data="page_about"),
            ],
            [
                InlineKeyboardButton("📢 Updates & Logs", url=LOG_PUBLIC_URL),
            ],
        ]
    )


def get_main_text() -> str:
    uptime = format_uptime(int(time.time() - START_TIME))
    return (
        "🛡 <b>DLK NSFW Cleaner</b>\n"
        "Keep your Telegram groups clean from NSFW content.\n\n"
        "• Deletes explicit NSFW content silently\n"
        "• Blacklists whole NSFW sticker packs per chat\n"
        f"• Mutes users after <code>{NSFW_STICKER_LIMIT}</code> NSFW stickers\n\n"
        "Use the buttons below to see how to use, features & permissions.\n\n"
        f"<b>Uptime:</b> <code>{uptime}</code>"
    )


def get_how_text() -> str:
    return (
        "📖 <b>How to use</b>\n\n"
        "1️⃣ Add me to your group\n"
        "2️⃣ Make me <b>Admin</b>\n"
        "   • Delete messages\n"
        "   • Ban/Restrict users\n\n"
        "3️⃣ That's it!\n"
        "   • I will silently delete 🔞 stickers/photos/gifs/videos\n"
        "   • If a sticker from a pack is NSFW, I blacklist that entire pack in that chat\n"
        f"   • If a user sends more than <code>{NSFW_STICKER_LIMIT}</code> NSFW stickers, "
        "I will try to mute them.\n\n"
        "Admins can whitelist specific stickers per-chat using /free (reply to a sticker). "
        "When you use /free, the bot will send you a private confirmation button — "
        "use that to confirm the whitelist. This whitelist is per-chat only and will prevent "
        "that sticker from being deleted in this chat."
    )


def get_features_text() -> str:
    return (
        "🛠 <b>Features</b>\n\n"
        "• Works on stickers, photos, GIFs, videos\n"
        "• Silently deletes 🔞 content\n"
        "• Per-chat sticker whitelist with /free\n"
        "• Sticker pack blacklist per chat\n"
        f"• Auto-mute after <code>{NSFW_STICKER_LIMIT}</code> NSFW stickers\n"
    )


def get_perms_text() -> str:
    return (
        "🔐 <b>Required Permissions</b>\n\n"
        "To work correctly in a group, I need:\n\n"
        "• Be <b>Admin</b>\n"
        "• <b>Delete messages</b>\n"
        "• <b>Ban/Restrict users</b> (to mute spammers)\n\n"
        "In topic groups (forums) also make sure:\n"
        "• I am admin at main group level (not only in one topic)\n"
    )


def get_about_text() -> str:
    return (
        "ℹ️ <b>About DLK NSFW Cleaner</b>\n\n"
        "This bot automatically detects and removes 🔞 NSFW content "
        "If a sticker in a pack is NSFW, the whole pack is blacklisted for that chat.\n"
        f"If a user keeps sending NSFW stickers more than <code>{NSFW_STICKER_LIMIT}</code> times, "
        "the bot will try to mute them (if it has permission).\n\n"
        f"<b>Developer:</b>\n<code>{DEV_ABOUT_TEXT}</code>\n"
        f"<b>Logs & Updates:</b> {LOG_PUBLIC_URL}"
    )


def build_subpage_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    # Same buttons, but first row includes Back
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ Back", callback_data="page_main"),
                InlineKeyboardButton(
                    "➕ Add to group",
                    url=f"https://t.me/{bot_username}?startgroup=nsfw_guard",
                ),
            ],
            [
                InlineKeyboardButton("📖 How to use", callback_data="page_how"),
                InlineKeyboardButton("🛠 Features", callback_data="page_features"),
            ],
            [
                InlineKeyboardButton("🔐 Permissions", callback_data="page_perms"),
                InlineKeyboardButton("ℹ️ About", callback_data="page_about"),
            ],
            [
                InlineKeyboardButton("📢 Updates & Logs", url=LOG_PUBLIC_URL),
            ],
        ]
    )


async def edit_main_message(msg: Message, text: str, keyboard: InlineKeyboardMarkup):
    """
    Edit caption if photo, else text.
    (Private /start → photo + caption, groups → normal text)
    """
    if msg.photo:
        await msg.edit_caption(text, reply_markup=keyboard)
    else:
        await msg.edit_text(text, reply_markup=keyboard)


# -------------------------------------------------
# Violation handling (mute after limit)
# -------------------------------------------------
async def handle_nsfw_sticker_violation(client: Client, message: Message, score: float):
    """
    Called when a NSFW sticker is deleted.
    - Increments user's NSFW sticker violation count.
    - If count > NSFW_STICKER_LIMIT -> try to mute user (if bot has permission).
    """
    chat_id = message.chat.id
    user = message.from_user
    if not user:
        return

    # Don't mute admins/owners
    if await is_user_admin(client, chat_id, user.id):
        log.info(
            f"[VIOLATION] User {user.id} is admin/owner, not muting. (chat={chat_id})"
        )
        return

    new_count = increment_violation(chat_id, user.id)
    log.info(
        f"[VIOLATION] NSFW sticker violation for user={user.id} in chat={chat_id}. "
        f"count={new_count}, limit={NSFW_STICKER_LIMIT}"
    )

    # තුණට වඩා ( > ) mute
    if new_count <= NSFW_STICKER_LIMIT:
        return

    if not await bot_can_restrict_members(client, chat_id):
        log.warning(
            f"[VIOLATION] Bot has no restrict/ban permission in chat={chat_id}, "
            f"cannot mute user={user.id}. Only deleting messages."
        )
        return

    until_date = datetime.utcnow() + timedelta(seconds=MUTE_DURATION_SECONDS)

    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
    )

    try:
        await client.restrict_chat_member(
            chat_id,
            user.id,
            permissions=permissions,
            until_date=until_date,
        )
        log.info(
            f"[VIOLATION] User={user.id} muted in chat={chat_id} for NSFW stickers. "
            f"Duration={MUTE_DURATION_SECONDS}s"
        )

        # Notice in group + button to your log channel
        try:
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📢 Bot Logs / Updates",
                            url=LOG_PUBLIC_URL,
                        )
                    ]
                ]
            )
            await message.reply_text(
                f"🚫 {user.mention} has been muted for repeated NSFW stickers "
                f"(>{NSFW_STICKER_LIMIT}).",
                reply_markup=kb,
            )
        except Exception:
            pass

        # Optional log chat
        if LOG_CHAT_ID:
            try:
                await client.send_message(
                    LOG_CHAT_ID,
                    "🚫 <b>User muted for NSFW stickers</b>\n\n"
                    f"👥 Chat ID: <code>{chat_id}</code>\n"
                    f"👤 User: {user.mention}\n"
                    f"🆔 User ID: <code>{user.id}</code>\n"
                    f"📊 Score(last): <code>{score:.2f}</code>\n"
                    f"🔢 Violations: <code>{new_count}</code>\n"
                    f"⏱ Duration: <code>{MUTE_DURATION_SECONDS}s</code>",
                )
            except Exception as e:
                log.warning(f"Failed to send violation log: {e}")

    except Exception as e:
        log.warning(
            f"[VIOLATION] Failed to mute user={user.id} in chat={chat_id}: {e}"
        )


# -------------------------------------------------
# Commands
# -------------------------------------------------
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    me = await client.get_me()
    bot_username = me.username or "NSFWGuardBot"
    keyboard = build_main_keyboard(bot_username)
    main_text = get_main_text()

    # private / group / supergroup / topic හැම තැනම එකම UI
    await message.reply_photo(
        START_PHOTO_URL,   # https://i.ibb.co/WNzKw5qk/DLKNSFWCleaner.png
        caption=main_text,
        reply_markup=keyboard,
    )


# /help → same menu (no separate help text)
@app.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    await start_cmd(client, message)


@app.on_message(filters.command("about"))
async def about_cmd(client: Client, message: Message):
    await message.reply_text(get_about_text())


@app.on_message(filters.command("status") & filters.group)
async def status_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    uptime = format_uptime(int(time.time() - START_TIME))

    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
        status = member.status
        privileges = getattr(member, "privileges", None)

        can_delete = bool(getattr(privileges, "can_delete_messages", False)) if privileges else False
        can_restrict = bool(
            getattr(privileges, "can_restrict_members", False)
            or getattr(privileges, "can_restrict_users", False)
        ) if privileges else False

        text = (
            "🛡 <b>NSFW Guard Status</b>\n\n"
            f"👥 Chat: <code>{message.chat.title or chat_id}</code>\n"
            f"🤖 Status: <code>{status}</code>\n"
            f"🗑 Delete messages: <b>{'✅ enabled' if can_delete else '❌ missing'}</b>\n"
            f"🚫 Restrict/Mute users: <b>{'✅ enabled' if can_restrict else '❌ missing'}</b>\n\n"
            f"⏱ Uptime: <code>{uptime}</code>\n"
        )
    except Exception as e:
        text = f"⚠️ Failed to read permissions: <code>{e}</code>"

    await message.reply_text(text)


@app.on_message(filters.command("ping"))
async def ping_cmd(client: Client, message: Message):
    uptime = format_uptime(int(time.time() - START_TIME))
    await message.reply_text(
        f"🏓 <b>Pong!</b>\nUptime: <code>{uptime}</code>"
    )


@app.on_message(filters.command("free") & filters.group)
async def free_cmd(client: Client, message: Message):
    """
    /free
    Admin-only.
    Must be used as reply to a sticker message.
    Whitelists that sticker in this chat — but to avoid accidental whitelists,
    we send a private confirmation button to the admin. When the admin clicks
    confirm in the bot's inbox, the sticker is whitelisted for the chat and any
    pack-blacklist entry for that pack in that chat is removed.
    """
    chat_id = message.chat.id
    user = message.from_user

    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text(
            "❌ Only group admins can use this command.",
            quote=True,
        )
        return

    if not message.reply_to_message or not message.reply_to_message.sticker:
        if message.reply_to_message:
            await message.reply_to_message.reply_text(
                "Please reply to a <b>sticker</b> with /free to whitelist it.",
                quote=True,
            )
        else:
            await message.reply_text(
                "Please reply to a <b>sticker</b> with /free to whitelist it.",
                quote=True,
            )
        return

    sticker = message.reply_to_message.sticker
    file_unique_id = sticker.file_unique_id
    set_name = getattr(sticker, "set_name", None) or ""

    # Create a pending request and send admin a private confirmation button
    pending_id = create_pending_whitelist(chat_id, user.id, file_unique_id, set_name)

    confirm_kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm whitelist in this group",
                    callback_data=f"free_confirm:{pending_id}",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"free_cancel:{pending_id}",
                ),
            ]
        ]
    )

    sent_to_group_text = (
        "✅ Request created. I have sent you a private message with a confirmation button. "
        "Open your private chat with me and tap ✅ to confirm the whitelist."
    )

    try:
        # Try to send private message to the admin
        await client.send_message(
            user.id,
            f"You're about to whitelist a sticker for chat: <code>{chat_id}</code> ({message.chat.title or 'group'}).\n\n"
            "Press Confirm to whitelist the sticker in that group. This whitelist is per-chat only — "
            "it will prevent this sticker from being deleted in that chat. If a pack was blacklisted in the chat, "
            "confirming this will remove the pack-blacklist for that chat so stickers from this pack won't be auto-deleted.\n\n"
            f"Sticker file id: <code>{file_unique_id}</code>",
            reply_markup=confirm_kb,
        )
        # Inform in group to check PM
        await message.reply_text(sent_to_group_text, quote=True)
    except Exception as e:
        log.warning(f"Failed to send private message to admin {user.id}: {e}")
        # If we cannot message the admin (blocked / hasn't started bot), instruct to start bot
        await message.reply_text(
            "✅ I couldn't send you a private message. Please start a private chat with me (@"
            f"{(await client.get_me()).username}) and then press /free again.",
            quote=True,
        )

    if LOG_CHAT_ID:
        try:
            await client.send_message(
                LOG_CHAT_ID,
                f"🛡 Whitelist requested by admin {user.mention} in chat <code>{chat_id}</code>.\n"
                f"Sticker: <code>{file_unique_id}</code>\nPack: <code>{set_name}</code>"
            )
        except Exception as e:
            log.warning(f"Failed to send whitelist log message: {e}")


# -------------------------------------------------
# Callback queries (inline buttons)
# -------------------------------------------------
@app.on_callback_query()
async def callback_handler(client: Client, callback_query):
    try:
        data = callback_query.data
        msg = callback_query.message
        me = await client.get_me()
        bot_username = me.username or "NSFWGuardBot"

        # Whitelist confirm flow
        if data and data.startswith("free_confirm:"):
            pending_id = data.split(":", 1)[1]
            pending = get_pending_whitelist(pending_id)
            if not pending:
                await callback_query.answer("This request has expired or is invalid.", show_alert=True)
                try:
                    await callback_query.message.edit_reply_markup(None)
                except Exception:
                    pass
                return

            # Only the admin who created the request can confirm
            if callback_query.from_user.id != int(pending["admin_user_id"]):
                await callback_query.answer("You are not allowed to confirm this request.", show_alert=True)
                return

            chat_id = int(pending["chat_id"])
            file_unique_id = pending["file_unique_id"]
            set_name = pending.get("set_name", "") or ""

            # Add whitelist entry
            add_sticker_whitelist(chat_id, file_unique_id)
            # Remove pack blacklist for this chat (so pack won't auto-delete)
            if set_name:
                remove_pack_blacklist(chat_id, set_name)

            delete_pending_whitelist(pending_id)

            # Notify admin in private
            try:
                await callback_query.answer("Sticker whitelisted for the chat.", show_alert=False)
                await callback_query.message.edit_text(
                    "✅ Sticker has been whitelisted in the chat.\n"
                    "Pack-level blacklist (if present) has been removed for that chat so stickers from this pack won't be auto-deleted.",
                )
            except Exception:
                pass

            # Optionally notify the group that admin whitelisted (non-spammy)
            try:
                await client.send_message(
                    chat_id,
                    f"✅ Sticker has been whitelisted by admin {callback_query.from_user.mention}. "
                    "This sticker will no longer be deleted by the NSFW filter in this group.",
                )
            except Exception:
                # Could fail if bot was removed etc.
                pass

            if LOG_CHAT_ID:
                try:
                    await client.send_message(
                        LOG_CHAT_ID,
                        f"✅ Sticker whitelisted by admin {callback_query.from_user.mention} in chat <code>{chat_id}</code>.\n"
                        f"<code>{file_unique_id}</code>\nPack: <code>{set_name}</code>"
                    )
                except Exception:
                    pass

            return

        if data and data.startswith("free_cancel:"):
            pending_id = data.split(":", 1)[1]
            pending = get_pending_whitelist(pending_id)
            if not pending:
                await callback_query.answer("This request has expired or is invalid.", show_alert=True)
                try:
                    await callback_query.message.edit_reply_markup(None)
                except Exception:
                    pass
                return

            # Only the admin who created the request can cancel
            if callback_query.from_user.id != int(pending["admin_user_id"]):
                await callback_query.answer("You are not allowed to cancel this request.", show_alert=True)
                return

            delete_pending_whitelist(pending_id)
            await callback_query.answer("Whitelist request cancelled.", show_alert=False)
            try:
                await callback_query.message.edit_text("❌ Whitelist request cancelled.")
            except Exception:
                pass
            return

        # Existing UI pages
        if data == "page_main":
            await callback_query.answer()
            await edit_main_message(
                msg,
                get_main_text(),
                build_main_keyboard(bot_username),
            )

        elif data == "page_how":
            await callback_query.answer()
            await edit_main_message(
                msg,
                get_how_text(),
                build_subpage_keyboard(bot_username),
            )

        elif data == "page_features":
            await callback_query.answer()
            await edit_main_message(
                msg,
                get_features_text(),
                build_subpage_keyboard(bot_username),
            )

        elif data == "page_perms":
            await callback_query.answer()
            await edit_main_message(
                msg,
                get_perms_text(),
                build_subpage_keyboard(bot_username),
            )

        elif data == "page_about":
            await callback_query.answer()
            await edit_main_message(
                msg,
                get_about_text(),
                build_subpage_keyboard(bot_username),
            )

        else:
            await callback_query.answer("Unknown action.", show_alert=False)
    except Exception as e:
        log.error(f"Error in callback_handler: {e}")


# -------------------------------------------------
# Main filter handler (stickers, photos, videos, GIFs)
# -------------------------------------------------
@app.on_message(
    filters.group
    & (
        filters.sticker
        | filters.photo
        | filters.video
        | filters.animation
    )
)
async def media_guard(client: Client, message: Message):
    chat_id = message.chat.id

    log.info(
        f"[MEDIA] New media in chat={chat_id}, user={getattr(message.from_user, 'id', 'N/A')}, "
        f"types: sticker={bool(message.sticker)}, photo={bool(message.photo)}, "
        f"video={bool(message.video)}, animation={bool(message.animation)}"
    )

    if not await is_bot_admin(client, chat_id):
        log.info(f"[MEDIA] Bot is not admin or cannot delete in chat={chat_id}, skipping.")
        return

    if not message.from_user or message.from_user.is_bot:
        return

    user = message.from_user

    temp_root = tempfile.mkdtemp(prefix="nsfw_guard_")
    image_paths: list[str] = []

    try:
        # Stickers
        if message.sticker:
            st = message.sticker
            set_name = getattr(st, "set_name", None)

            # 1) Sticker whitelist first — whitelisted stickers should NEVER be deleted in this chat
            if is_sticker_whitelisted(chat_id, st.file_unique_id):
                log.info(
                    f"[MEDIA] Sticker is whitelisted (chat={chat_id}, file_unique_id={st.file_unique_id})"
                )
                return

            # 2) PACK BLACKLIST next — if pack is blacklisted for this chat, delete without scan
            if set_name and is_pack_blacklisted(chat_id, set_name):
                log.info(
                    f"[MEDIA] Sticker pack blacklisted (chat={chat_id}, set_name={set_name}). "
                    "Deleting without scan."
                )
                await delete_nsfw_message(client, message, score=1.0)
                await handle_nsfw_sticker_violation(client, message, score=1.0)
                return

            file_path = await message.download(
                file_name=os.path.join(temp_root, "sticker")
            )

            if st.is_animated:
                png_path = os.path.join(temp_root, "sticker.png")
                converted = convert_tgs_to_png(file_path, png_path)
                if converted:
                    image_paths.append(converted)

            elif st.is_video:
                frame_dir = os.path.join(temp_root, "frames")
                os.makedirs(frame_dir, exist_ok=True)
                frames = extract_video_frames(file_path, frame_dir, max_frames=3)
                image_paths.extend(frames)

            else:
                image_paths.append(file_path)

        # Photos
        elif message.photo:
            file_path = await message.download(
                file_name=os.path.join(temp_root, "photo")
            )
            image_paths.append(file_path)

        # Videos / GIFs
        elif message.video or message.animation:
            file_path = await message.download(
                file_name=os.path.join(temp_root, "video")
            )
            frame_dir = os.path.join(temp_root, "frames")
            os.makedirs(frame_dir, exist_ok=True)
            frames = extract_video_frames(file_path, frame_dir, max_frames=3)
            image_paths.extend(frames)

        if not image_paths:
            log.info(f"[MEDIA] No image paths extracted, skipping.")
            return

        score = scan_images_for_nsfw(image_paths)
        log.info(
            f"[MEDIA] Scan result chat={chat_id}, user={user.id}, score={score:.2f}, threshold={NSFW_THRESHOLD}"
        )

        if score >= NSFW_THRESHOLD:
            await delete_nsfw_message(client, message, score)

            if message.sticker:
                st = message.sticker
                set_name = getattr(st, "set_name", None)
                if set_name:
                    add_pack_blacklist(chat_id, set_name)
                    log.info(
                        f"[PACK-BL] Blacklisted sticker pack in chat={chat_id}, set_name={set_name}"
                    )

                    if LOG_CHAT_ID:
                        try:
                            await client.send_message(
                                LOG_CHAT_ID,
                                "🚫 <b>Sticker pack blacklisted</b>\n\n"
                                f"👥 Chat ID: <code>{chat_id}</code>\n"
                                f"📦 Pack: <code>{set_name}</code>\n"
                                f"👤 Triggered by: {user.mention if user else 'Unknown'}\n"
                                f"📊 Score: <code>{score:.2f}</code>",
                            )
                        except Exception as e:
                            log.warning(f"Failed to send pack blacklist log: {e}")

                await handle_nsfw_sticker_violation(client, message, score)
        else:
            log.info(
                f"[MEDIA] Content seems safe (chat={chat_id}, user={user.id}, score={score:.2f})"
            )

    except Exception as e:
        log.error(f"Error in media_guard: {e}")
    finally:
        try:
            shutil.rmtree(temp_root)
        except Exception:
            pass


# -------------------------------------------------
# Main
# -------------------------------------------------
if __name__ == "__main__":
    log.info("NSFW Guard Bot is starting...")
    app.run()
