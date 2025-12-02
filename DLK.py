# Full updated NSFW guard bot implementation
# - Merges /free (no-reply flow), /blockpack bulk blocking and fixes from earlier patch
# - Safe media handling (Pillow normalize) to avoid NudeDetector NoneType/shape errors
# - Lazy-loads NudeDetector, resilient to detector exceptions
# - Supports forum/topic threads by preserving message_thread_id when replying/logging
# - Safe copy/send wrappers, robust pending actions and private flows
# - Inline moderation (Unmute/Ban) with permission checks and Close button
# - Per-chat sticker whitelist, pack blacklist, per-user violation counts and auto-mute
# Requirements: pyrogram, pymongo, python-dotenv, pillow, nudenet (optional but recommended),
# ffmpeg installed system-wide, lottie (optional for .tgs)
#
# Configure via .env: API_ID, API_HASH, BOT_TOKEN, MONGO_URI, LOG_CHAT_ID (optional), OWNER_IDS (optional)
# Optional timings: NSFW_THRESHOLD, NSFW_STICKER_LIMIT, PACK_STICKER_LIMIT, MUTE_DURATION_SECONDS,
# CONFIRM_MSG_DELETE_SECONDS, DELETE_LOG_MESSAGE_SECONDS

import os
import time
import logging
import tempfile
import shutil
import subprocess
import asyncio
import re
from datetime import datetime, timedelta
from typing import List, Optional

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from bson.objectid import ObjectId

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
    CallbackQuery,
)
from pyrogram.enums import ChatMemberStatus

# Lazy import for NudeDetector and lottie (tgs conversion)
try:
    from nudenet import NudeDetector
except Exception:
    NudeDetector = None

from PIL import Image

# -------------------------------------------------
# Load environment
# -------------------------------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.75"))

# how many NSFW stickers allowed before mute
NSFW_STICKER_LIMIT = int(os.getenv("NSFW_STICKER_LIMIT", "3"))
# also support separate pack-based sticker limit if desired
PACK_STICKER_LIMIT = int(os.getenv("PACK_STICKER_LIMIT", str(NSFW_STICKER_LIMIT)))

# mute duration in seconds (default: 1 day)
MUTE_DURATION_SECONDS = int(os.getenv("MUTE_DURATION_SECONDS", "86400"))

# helper message lifetime seconds
CONFIRM_MSG_DELETE_SECONDS = int(os.getenv("CONFIRM_MSG_DELETE_SECONDS", "10"))

# how long to keep in-chat log messages before auto-delete
DELETE_LOG_MESSAGE_SECONDS = int(os.getenv("DELETE_LOG_MESSAGE_SECONDS", "10"))

MONGO_URI = os.getenv("MONGO_URI", "").strip()
if not MONGO_URI:
    raise SystemExit("MONGO_URI is not set in environment. Set it in your .env file.")

# Optional external log chat (still supported)
LOG_CHAT_ID_ENV = os.getenv("LOG_CHAT_ID", "").strip()
LOG_CHAT_ID = LOG_CHAT_ID_ENV if LOG_CHAT_ID_ENV else None

# Optional owner ids who can always perform mod actions from logs
OWNER_IDS = set()
owner_env = os.getenv("OWNER_IDS", "").strip()
if owner_env:
    try:
        OWNER_IDS = set(int(s) for s in re.split(r"[,\s]+", owner_env) if s)
    except Exception:
        OWNER_IDS = set()

START_TIME = time.time()

# Branding & helper URLs
OFFICIAL_CHANNEL = "https://t.me/DLKDevelopers"
LOG_PUBLIC_URL = "https://t.me/DOOZY_OFF"
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
# MongoDB collections & helpers
# -------------------------------------------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["nsfw_guard"]

# sticker whitelist (per chat)
whitelist_col = db["sticker_whitelist"]
whitelist_col.create_index([("chat_id", ASCENDING), ("file_unique_id", ASCENDING)], unique=True)


def is_sticker_whitelisted(chat_id: int, file_unique_id: str) -> bool:
    return whitelist_col.find_one({"chat_id": chat_id, "file_unique_id": file_unique_id}) is not None


def add_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.update_one(
        {"chat_id": chat_id, "file_unique_id": file_unique_id},
        {"$set": {"chat_id": chat_id, "file_unique_id": file_unique_id, "ts": int(time.time())}},
        upsert=True,
    )


def remove_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.delete_one({"chat_id": chat_id, "file_unique_id": file_unique_id})


# sticker pack blacklist (per chat)
pack_blacklist_col = db["sticker_pack_blacklist"]
pack_blacklist_col.create_index([("chat_id", ASCENDING), ("set_name", ASCENDING)], unique=True)


def is_pack_blacklisted(chat_id: int, set_name: str) -> bool:
    if not set_name:
        return False
    return pack_blacklist_col.find_one({"chat_id": chat_id, "set_name": set_name}) is not None


def add_pack_blacklist(chat_id: int, set_name: str) -> None:
    if not set_name:
        return
    pack_blacklist_col.update_one(
        {"chat_id": chat_id, "set_name": set_name},
        {"$set": {"chat_id": chat_id, "set_name": set_name, "ts": int(time.time())}},
        upsert=True,
    )


def remove_pack_blacklist(chat_id: int, set_name: str) -> None:
    if not set_name:
        return
    pack_blacklist_col.delete_one({"chat_id": chat_id, "set_name": set_name})


# NSFW violations per-user
violations_col = db["nsfw_violations"]
violations_col.create_index([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)


def increment_violation(chat_id: int, user_id: int) -> int:
    doc = violations_col.find_one({"chat_id": chat_id, "user_id": user_id})
    if doc:
        new_count = int(doc.get("count", 0)) + 1
        violations_col.update_one({"chat_id": chat_id, "user_id": user_id}, {"$set": {"count": new_count}})
    else:
        new_count = 1
        violations_col.insert_one({"chat_id": chat_id, "user_id": user_id, "count": new_count})
    return new_count


def get_violation_count(chat_id: int, user_id: int) -> int:
    doc = violations_col.find_one({"chat_id": chat_id, "user_id": user_id})
    return int(doc.get("count", 0)) if doc else 0


# pending actions for confirm flows
pending_col = db["pending_actions"]
try:
    pending_col.create_index("ts", expireAfterSeconds=3600)
except Exception:
    pass


def create_pending_action(action_type: str, chat_id: int, admin_user_id: int, file_unique_id: str = "", set_name: str = "") -> str:
    doc = {
        "action": action_type,
        "chat_id": chat_id,
        "admin_user_id": admin_user_id,
        "file_unique_id": file_unique_id or "",
        "set_name": set_name or "",
        "stickers": [],
        "set_names": [],
        "state": "open",
        "ts": int(time.time()),
    }
    res = pending_col.insert_one(doc)
    return str(res.inserted_id)


def get_pending_action(pending_id: str):
    try:
        return pending_col.find_one({"_id": ObjectId(pending_id)})
    except Exception:
        return None


def get_latest_pending_for_admin(admin_user_id: int, action_type: str):
    try:
        return pending_col.find_one({"admin_user_id": admin_user_id, "action": action_type, "state": "open"}, sort=[("ts", -1)])
    except Exception:
        return None


def update_pending_action(pending_id: str, update_fields: dict):
    try:
        pending_col.update_one({"_id": ObjectId(pending_id)}, {"$set": update_fields})
    except Exception:
        pass


def push_sticker_to_pending(pending_id: str, file_unique_id: str):
    try:
        pending_col.update_one({"_id": ObjectId(pending_id)}, {"$addToSet": {"stickers": file_unique_id}})
    except Exception:
        pass


def push_setname_to_pending(pending_id: str, set_name: str):
    try:
        if set_name:
            pending_col.update_one({"_id": ObjectId(pending_id)}, {"$addToSet": {"set_names": set_name}})
    except Exception:
        pass


def finalize_pending_action(pending_id: str):
    try:
        pending_col.update_one({"_id": ObjectId(pending_id)}, {"$set": {"state": "done", "done_ts": int(time.time())}})
    except Exception:
        pass


def cancel_pending_action(pending_id: str):
    try:
        pending_col.update_one({"_id": ObjectId(pending_id)}, {"$set": {"state": "cancelled", "cancel_ts": int(time.time())}})
    except Exception:
        pass


# -------------------------------------------------
# Pyrogram client
# -------------------------------------------------
app = Client("nsfw_guard_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# -------------------------------------------------
# NSFW detector (lazy)
# -------------------------------------------------
_detector_instance = None


def get_detector():
    global _detector_instance
    if _detector_instance is None:
        if NudeDetector is None:
            raise RuntimeError("NudeDetector not available. Install nudenet package.")
        log.info("Loading NudeNet detector (first run may download the model)...")
        _detector_instance = NudeDetector()
    return _detector_instance


EXPLICIT_LABELS = {
    "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "GENITALIA_EXPOSED", "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED", "FEMALE_NIPPLE_EXPOSED", "MALE_BREAST_EXPOSED", "BREAST_EXPOSED",
    "NUDE_FEMALE_CHEST", "NUDE_MALE_CHEST", "BUTTOCKS_EXPOSED", "FEMALE_BUTTOCKS_EXPOSED",
    "MALE_BUTTOCKS_EXPOSED", "SEXUAL_ACTIVITY", "SEX_ACT", "SEXUAL_INTERCOURSE", "MASTURBATION",
    "ORAL_SEX", "ANAL_SEX", "PORNOGRAPHIC", "SEXUALIZED_NUDITY", "EXPLICIT_NUDITY", "ADULT_CONTENT",
    "HARDCORE", "SOFTCORE", "LEWD_CONTENT", "OBSCENE_CONTENT", "INAPPROPRIATE_CONTENT",
    "MINOR_NUDITY", "CHILD_NUDITY", "CSAM_SUSPECT", "ADULT_TOY", "SEX_TOY", "FETISH_CONTENT",
}

# -------------------------------------------------
# Helpers
# -------------------------------------------------
async def is_bot_admin(client: Client, chat_id: int) -> bool:
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
        if member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            return False
        privileges = getattr(member, "privileges", None)
        if privileges is not None:
            return bool(getattr(privileges, "can_delete_messages", False))
        can_delete_attr = getattr(member, "can_delete_messages", None)
        if can_delete_attr is not None:
            return bool(can_delete_attr)
        return False
    except Exception as e:
        log.warning(f"Admin check failed: {e}")
        return False


async def is_user_admin(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


async def bot_can_restrict_members(client: Client, chat_id: int) -> bool:
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
        privileges = getattr(member, "privileges", None)
        if privileges is not None:
            return bool(getattr(privileges, "can_restrict_members", False) or getattr(privileges, "can_restrict_users", False))
        return False
    except Exception as e:
        log.warning(f"Restrict permission check failed: {e}")
        return False


def extract_video_frames(src_path: str, temp_dir: str, max_frames: int = 3) -> List[str]:
    frames = []
    try:
        out_pattern = os.path.join(temp_dir, "frame_%03d.jpg")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src_path, "-vf", "fps=1", "-vframes", str(max_frames), out_pattern]
        subprocess.run(cmd, check=True)
        for fname in sorted(os.listdir(temp_dir)):
            if fname.lower().endswith(".jpg") or fname.lower().endswith(".jpeg"):
                frames.append(os.path.join(temp_dir, fname))
    except Exception as e:
        log.warning(f"Frame extract failed: {e}")
    return frames


def convert_tgs_to_png(tgs_path: str, out_path: str) -> Optional[str]:
    try:
        from lottie import importers, exporters
        with open(tgs_path, "rb") as f:
            animation = importers.tgs.import_tgs(f)
        exporters.export_png(animation, out_path)
        return out_path
    except Exception as e:
        log.warning(f"TGS convert failed (treating as safe): {e}")
        return None


def prepare_image_for_detector(src_path: str, out_dir: str) -> Optional[str]:
    """
    Normalize an image to JPEG for the detector. Returns path to normalized JPEG or None.
    """
    if not src_path or not os.path.exists(src_path):
        return None
    try:
        img = Image.open(src_path).convert("RGB")
        out_path = os.path.join(out_dir, f"scan_{os.path.basename(src_path)}.jpg")
        img.save(out_path, format="JPEG", quality=85)
        return out_path
    except Exception as e:
        log.warning(f"prepare_image_for_detector failed for {src_path}: {e}")
        return None


def scan_images_for_nsfw(image_paths: List[str]) -> float:
    """
    Returns max explicit score across provided images. Resilient to detector errors.
    """
    if not image_paths:
        return 0.0
    max_score = 0.0
    try:
        detector = get_detector()
    except Exception as e:
        log.warning(f"Detector not available: {e}")
        return 0.0

    for path in image_paths:
        if not path or not os.path.exists(path):
            continue
        try:
            detections = detector.detect(path)
            log.info(f"[DETECT] {path} -> {detections}")
            if not detections:
                continue
            for det in detections:
                label = str(det.get("class", "")).upper()
                score = float(det.get("score", 0.0))
                if label in EXPLICIT_LABELS:
                    if score > max_score:
                        max_score = score
                else:
                    log.debug(f"[DETECT] Ignoring non-explicit label={label}, score={score:.2f}")
        except Exception as e:
            log.warning(f"Scanning failed for {path}: {e}")
            continue
    return max_score


async def safe_send_message(client: Client, chat_id: int, text: str, reply_markup=None, thread_id: Optional[int] = None):
    try:
        if thread_id:
            return await client.send_message(chat_id, text, reply_markup=reply_markup, message_thread_id=thread_id)
        else:
            return await client.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        log.warning(f"Failed to send message to {chat_id} (thread {thread_id}): {e}")
        return None


async def safe_copy_message(client: Client, to_chat_id: int, from_chat_id: int, message_id: int, thread_id: Optional[int] = None):
    try:
        if thread_id:
            return await client.copy_message(to_chat_id, from_chat_id, message_id, message_thread_id=thread_id)
        else:
            return await client.copy_message(to_chat_id, from_chat_id, message_id)
    except Exception as e:
        log.warning(f"Failed to copy message {message_id} from {from_chat_id} to {to_chat_id} (thread {thread_id}): {e}")
        return None


async def delete_nsfw_message(client: Client, message: Message, score: float):
    chat = message.chat
    user = message.from_user
    thread_id = getattr(message, "message_thread_id", None)
    try:
        try:
            await message.delete()
        except Exception as e:
            log.warning(f"Failed to delete NSFW message in chat {chat.id}: {e}")
        log.info(f"[DELETE] NSFW content deleted in chat={chat.id}, user={user.id if user else 'N/A'}, score={score:.2f}")
    except Exception as e:
        log.warning(f"Failed in delete_nsfw_message: {e}")

    # create inline keyboard for logs (unmute/ban/close)
    try:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{chat.id}:{user.id if user else 0}"),
             InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{chat.id}:{user.id if user else 0}")],
            [InlineKeyboardButton("✖️ Close", callback_data="close_log")]
        ])
        reason = f"NSFW detection score {score:.2f} >= threshold {NSFW_THRESHOLD}"
        mention = user.mention if user else "<b>Unknown</b>"
        text = (
            "🔍 <b>NSFW content deleted</b>\n\n"
            f"👤 User: {mention}\n"
            f"💬 Chat: <code>{chat.title or chat.id}</code>\n"
            f"📊 Score: <code>{score:.2f}</code>\n"
            f"🆔 Chat ID: <code>{chat.id}</code>\n"
            f"🆔 User ID: <code>{user.id if user else 'N/A'}</code>\n"
            f"📝 Reason: {reason}"
        )
        sent = await safe_send_message(client, chat.id, text, reply_markup=kb, thread_id=thread_id)
        if sent:
            asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
        # also optionally send to external log channel
        if LOG_CHAT_ID:
            try:
                await client.send_message(LOG_CHAT_ID, text, reply_markup=kb)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"Failed to send in-chat log message: {e}")


async def schedule_delete(msg: Message, delay: int):
    try:
        await asyncio.sleep(delay)
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception as e:
        log.debug(f"schedule_delete error: {e}")


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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add me to your group", url=f"https://t.me/{bot_username}?startgroup=nsfw_guard")],
        [InlineKeyboardButton("📖 How to use", callback_data="page_how"),
         InlineKeyboardButton("🛠 Features", callback_data="page_features")],
        [InlineKeyboardButton("🔐 Permissions", callback_data="page_perms"),
         InlineKeyboardButton("ℹ️ About", callback_data="page_about")],
        [InlineKeyboardButton("📢 Updates & Logs", url=LOG_PUBLIC_URL)],
    ])


def get_main_text() -> str:
    uptime = format_uptime(int(time.time() - START_TIME))
    return (
        "🛡 <b>DLK NSFW Cleaner</b>\n\n"
        "Protect your Telegram groups from nude / explicit content automatically.\n\n"
        "• Auto-scan stickers, photos, GIFs & videos\n"
        "• Silently deletes explicit NSFW content\n"
        "• Blacklists whole NSFW sticker packs per chat\n"
        f"• Mutes users after <b>{NSFW_STICKER_LIMIT}</b> NSFW stickers (non-admins only)\n\n"
        f"Uptime: <code>{uptime}</code>\n\n"
        "Use the buttons below for more help and options."
    )


def get_how_text() -> str:
    return (
        "📖 <b>How to use</b>\n\n"
        "1) Add the bot to your group and grant admin permissions:\n"
        "   • Delete messages\n"
        "   • Restrict/ban users\n\n"
        "2) The bot will scan newly posted stickers, photos, GIFs & videos.\n"
        f"   • After <b>{NSFW_STICKER_LIMIT}</b> violations, a non-admin will be muted.\n\n"
        "Whitelist & block pack flows:\n"
        "• /free (reply or no-reply) — whitelist a sticker per-chat\n"
        "• /unfree (reply) — remove a sticker from whitelist\n"
        "• /blockpack — bulk-block packs via PM (send stickers then DONE)\n"
    )


def get_features_text() -> str:
    return (
        "🛠 <b>Features</b>\n\n"
        "• Works on stickers, photos, GIFs, videos\n"
        "• Silently deletes explicit content\n"
        "• Per-chat sticker whitelist with /free\n"
        "• Bulk sticker-pack blocking via /blockpack\n"
    )


def get_perms_text() -> str:
    return (
        "🔐 <b>Required Permissions</b>\n\n"
        "• Be <b>Admin</b>\n"
        "• <b>Delete messages</b>\n"
        "• <b>Ban/Restrict users</b>\n\n"
        "In topic groups (forums) ensure the bot is admin at the main group level."
    )


def get_about_text() -> str:
    return (
        "ℹ️ <b>About DLK NSFW Cleaner</b>\n\n"
        "Automatically detects and removes nude/explicit NSFW content. Pack-level blacklisting and per-chat whitelists supported.\n\n"
        f"Developer: <code>{DEV_ABOUT_TEXT}</code>\nUpdates & Logs: {LOG_PUBLIC_URL}"
    )


async def edit_main_message(msg: Message, text: str, keyboard: InlineKeyboardMarkup):
    if not msg:
        return
    try:
        if getattr(msg, "photo", None):
            await msg.edit_caption(text, reply_markup=keyboard)
        else:
            await msg.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        log.warning(f"Failed to edit main message: {e}")


# -------------------------------------------------
# Violation handling (mute after limit)
# -------------------------------------------------
async def notify_mute_to_log(client: Client, chat_id: int, user, violations: int, score: float, reason: str, thread_id: Optional[int] = None):
    if not chat_id:
        return
    try:
        mention = user.mention if user else "<b>Unknown</b>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{chat_id}:{user.id if user else 0}"),
             InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{chat_id}:{user.id if user else 0}")],
            [InlineKeyboardButton("✖️ Close", callback_data="close_log")]
        ])
        text = (
            "🚫 <b>User muted for NSFW stickers</b>\n\n"
            f"👥 Chat: <code>{chat_id}</code>\n"
            f"👤 User: {mention}\n"
            f"📊 Last Score: <code>{score:.2f}</code>\n"
            f"🔢 Violations: <code>{violations}</code>\n"
            f"⏱ Duration: <code>{MUTE_DURATION_SECONDS}s</code>\n"
            f"📝 Reason: {reason}"
        )
        sent = await safe_send_message(client, chat_id, text, reply_markup=kb, thread_id=thread_id)
        if sent:
            asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
    except Exception as e:
        log.warning(f"Failed to send mute log message: {e}")


async def handle_nsfw_sticker_violation(client: Client, message: Message, score: float, reason: str = "NSFW content"):
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    user = message.from_user
    if not user:
        return
    if await is_user_admin(client, chat_id, user.id):
        log.info(f"[VIOLATION] User {user.id} is admin; skipping mute. (chat={chat_id})")
        return
    new_count = increment_violation(chat_id, user.id)
    log.info(f"[VIOLATION] NSFW sticker violation for user={user.id} in chat={chat_id}. count={new_count}, limit={NSFW_STICKER_LIMIT}")
    if new_count <= NSFW_STICKER_LIMIT:
        return
    if not await bot_can_restrict_members(client, chat_id):
        log.warning(f"[VIOLATION] Bot lacks restrict permission in chat={chat_id}, cannot mute user={user.id}.")
        return
    until_date = datetime.utcnow() + timedelta(seconds=MUTE_DURATION_SECONDS)
    permissions = ChatPermissions(
        can_send_messages=False, can_send_media_messages=False, can_send_polls=False, can_send_other_messages=False,
        can_add_web_page_previews=False, can_change_info=False, can_invite_users=False, can_pin_messages=False,
    )
    try:
        await client.restrict_chat_member(chat_id, user.id, permissions=permissions, until_date=until_date)
        log.info(f"[VIOLATION] Muted user={user.id} in chat={chat_id} for {MUTE_DURATION_SECONDS}s")
        try:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Bot Logs / Updates", url=LOG_PUBLIC_URL)]])
            reply_msg = None
            try:
                reply_msg = await message.reply_text(f"🚫 {user.mention} has been muted for repeated NSFW content (>{NSFW_STICKER_LIMIT}).", reply_markup=kb)
            except Exception:
                reply_msg = await safe_send_message(client, chat_id, f"🚫 {user.mention} has been muted for repeated NSFW content (>{NSFW_STICKER_LIMIT}).", reply_markup=kb, thread_id=thread_id)
            if reply_msg:
                asyncio.create_task(schedule_delete(reply_msg, DELETE_LOG_MESSAGE_SECONDS))
        except Exception:
            pass
        await notify_mute_to_log(client, chat_id, user, new_count, score, reason, thread_id=thread_id)
    except Exception as e:
        log.warning(f"[VIOLATION] Failed to mute user={user.id} in chat={chat_id}: {e}")


# -------------------------------------------------
# Commands & pages
# -------------------------------------------------
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    me = await client.get_me()
    bot_username = me.username or "NSFWGuardBot"
    keyboard = build_main_keyboard(bot_username)
    main_text = get_main_text()

    payload = ""
    if message.text:
        parts = message.text.split()
        if len(parts) > 1:
            payload = parts[1].strip()

    if payload.startswith(("free_", "unblack_", "block_")):
        pending_id = payload.split("_", 1)[1]
        pending = get_pending_action(pending_id)
        if not pending:
            await message.reply_text("This request is invalid or has expired.")
            return
        if message.from_user.id != int(pending["admin_user_id"]):
            await message.reply_text("You are not allowed to confirm this request.")
            return
        action = pending.get("action", "whitelist")
        if action == "whitelist":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"), InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")]])
            await message.reply_text(f"You're about to whitelist a sticker in chat <code>{pending['chat_id']}</code>.\nPress Confirm to whitelist it in that group (per-chat).", reply_markup=kb)
            return
        if action == "unblacklist":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Remove pack blacklist for chat", callback_data=f"unblack_confirm:{pending_id}"), InlineKeyboardButton("❌ Cancel", callback_data=f"unblack_cancel:{pending_id}")]])
            await message.reply_text(f"You're about to remove the pack-level blacklist for pack <code>{pending.get('set_name')}</code> in chat <code>{pending['chat_id']}</code>.\nPress Confirm to remove the blacklist for that chat.", reply_markup=kb)
            return
        if action == "bulk_block":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"block_cancel:{pending_id}")]])
            await message.reply_text("Bulk pack block — send stickers here, then send DONE to finalize.", reply_markup=kb)
            return

    await message.reply_photo(START_PHOTO_URL, caption=main_text, reply_markup=keyboard)


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
        can_restrict = bool(getattr(privileges, "can_restrict_members", False) or getattr(privileges, "can_restrict_users", False)) if privileges else False
        text = ("🛡 <b>NSFW Guard Status</b>\n\n"
                f"👥 Chat: <code>{message.chat.title or chat_id}</code>\n"
                f"🤖 Status: <code>{status}</code>\n"
                f"🗑 Delete messages: <b>{'✅ enabled' if can_delete else '❌ missing'}</b>\n"
                f"🚫 Restrict/Mute users: <b>{'✅ enabled' if can_restrict else '❌ missing'}</b>\n\n"
                f"⏱ Uptime: <code>{uptime}</code>\n")
    except Exception as e:
        text = f"⚠️ Failed to read permissions: <code>{e}</code>"
    await message.reply_text(text)


@app.on_message(filters.command("ping"))
async def ping_cmd(client: Client, message: Message):
    uptime = format_uptime(int(time.time() - START_TIME))
    await message.reply_text(f"🏓 <b>Pong!</b>\nUptime: <code>{uptime}</code>")


# -------------------------------------------------
# /free command (group) - supports reply and no-reply flows
# -------------------------------------------------
@app.on_message(filters.command("free") & filters.group)
async def free_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    user = message.from_user
    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text("❌ Only group admins can use this command.", quote=True)
        return

    reply = message.reply_to_message
    me = await client.get_me()
    bot_username = me.username or "NSFWGuardBot"

    if reply and reply.sticker:
        sticker_msg = reply
        file_unique_id = sticker_msg.sticker.file_unique_id
        set_name = getattr(sticker_msg.sticker, "set_name", None) or ""
        pending_id = create_pending_action("whitelist", chat_id, user.id, file_unique_id, set_name)
        deep_link = f"https://t.me/{bot_username}?start=free_{pending_id}"
        confirm_kb_group = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm in private", url=deep_link), InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel_group:{pending_id}")]])
        try:
            grp_msg = await message.reply_text("✅ Whitelist request created. Open my private chat and confirm.", reply_markup=confirm_kb_group, quote=True)
            asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
        except Exception:
            pass
        try:
            # copy sticker to admin PM for preview (preserve thread if sending into a forum PM thread doesn't apply)
            copied = await safe_copy_message(client, user.id, getattr(sticker_msg.chat, "id", sticker_msg.chat.id), getattr(sticker_msg, "message_id", getattr(sticker_msg, "id", None)))
            confirm_kb_pm = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"), InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")]])
            pm_text = await client.send_message(user.id, f"You're about to whitelist a sticker in chat <code>{chat_id}</code>. Press Confirm to whitelist.", reply_markup=confirm_kb_pm)
            if copied:
                asyncio.create_task(schedule_delete(copied, CONFIRM_MSG_DELETE_SECONDS))
            asyncio.create_task(schedule_delete(pm_text, CONFIRM_MSG_DELETE_SECONDS))
        except Exception:
            log.info(f"Could not send PM to admin {user.id}")
    else:
        pending_id = create_pending_action("whitelist", chat_id, user.id, "", "")
        deep_link = f"https://t.me/{bot_username}?start=free_{pending_id}"
        confirm_kb_group = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Open bot private (send sticker there)", url=deep_link), InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel_group:{pending_id}")]])
        try:
            grp_msg = await message.reply_text("✅ Whitelist request created. Open my private chat and send the sticker you want to whitelist, then press Confirm.", reply_markup=confirm_kb_group, quote=True)
            asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
        except Exception:
            pass

    # optional in-chat log
    try:
        await safe_send_message(client, chat_id, f"🛡 Whitelist requested by admin {user.mention} in chat <code>{chat_id}</code>. Pending ID: <code>{pending_id}</code>", thread_id=thread_id)
    except Exception:
        pass


# -------------------------------------------------
# /unfree command
# -------------------------------------------------
@app.on_message(filters.command("unfree") & filters.group)
async def unfree_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    user = message.from_user
    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text("❌ Only group admins can use this command.", quote=True)
        return
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await message.reply_text("Please reply to the sticker you want to un-whitelist with /unfree.", quote=True)
        return
    sticker = message.reply_to_message.sticker
    file_unique_id = sticker.file_unique_id
    set_name = getattr(sticker, "set_name", None) or ""
    remove_sticker_whitelist(chat_id, file_unique_id)
    await message.reply_text("✅ This sticker has been removed from the chat whitelist (if it was whitelisted).", quote=True)
    if set_name and is_pack_blacklisted(chat_id, set_name):
        me = await client.get_me()
        bot_username = me.username or "NSFWGuardBot"
        pending_id = create_pending_action("unblacklist", chat_id, user.id, file_unique_id, set_name)
        deep_link = f"https://t.me/{bot_username}?start=unblack_{pending_id}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Remove pack-blacklist (confirm in PM)", url=deep_link), InlineKeyboardButton("❌ Cancel", callback_data=f"unblack_cancel_group:{pending_id}")]])
        try:
            grp = await message.reply_text(f"⚠️ Pack <code>{set_name}</code> is blacklisted in this chat. Click to confirm removal in my private chat.", reply_markup=kb, quote=True)
            asyncio.create_task(schedule_delete(grp, CONFIRM_MSG_DELETE_SECONDS))
        except Exception:
            pass
    # optional external log
    if LOG_CHAT_ID:
        try:
            await client.send_message(LOG_CHAT_ID, f"❌ Unwhitelist requested by admin {user.mention} in chat <code>{chat_id}</code>.")
        except Exception:
            pass


# -------------------------------------------------
# /blockpack (group) - bulk block via PM
# -------------------------------------------------
@app.on_message(filters.command("blockpack") & filters.group)
async def blockpack_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    user = message.from_user
    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text("❌ Only group admins can use this command.", quote=True)
        return

    me = await client.get_me()
    bot_username = me.username or "NSFWGuardBot"
    pending_id = create_pending_action("bulk_block", chat_id, user.id, "", "")
    deep_link = f"https://t.me/{bot_username}?start=block_{pending_id}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Open private and send stickers", url=deep_link), InlineKeyboardButton("❌ Cancel", callback_data=f"block_cancel_group:{pending_id}")]])
    try:
        grp_msg = await message.reply_text("📦 Bulk block: Click to open my private chat and send up to 200 stickers (multiple packs allowed). When finished, send DONE.", reply_markup=kb, quote=True)
        asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
    except Exception:
        pass
    if LOG_CHAT_ID:
        try:
            await client.send_message(LOG_CHAT_ID, f"Bulk block requested by admin {user.mention} in chat <code>{chat_id}</code>. Pending ID: <code>{pending_id}</code>")
        except Exception:
            pass


# -------------------------------------------------
# Private: collecting stickers in PM and DONE handler
# -------------------------------------------------
@app.on_message(filters.private & filters.sticker)
async def private_sticker_collector(client: Client, message: Message):
    user = message.from_user
    # bulk_block pending for this admin?
    pending = get_latest_pending_for_admin(user.id, "bulk_block")
    if not pending:
        # maybe it's a /free no-reply pending
        pending_free = get_latest_pending_for_admin(user.id, "whitelist")
        if pending_free and pending_free.get("file_unique_id", "") == "":
            st = message.sticker
            if not st:
                await message.reply_text("Please send a sticker to whitelist.")
                return
            file_unique_id = st.file_unique_id
            set_name = getattr(st, "set_name", None) or ""
            update_pending_action(str(pending_free["_id"]), {"file_unique_id": file_unique_id, "set_name": set_name})
            pending = get_pending_action(str(pending_free["_id"]))
            pending_id = str(pending["_id"])
            confirm_kb_pm = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"), InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")]])
            try:
                copied = await safe_copy_message(client, user.id, message.chat.id, getattr(message, "message_id", getattr(message, "id", None)))
                pm_text = await client.send_message(user.id, f"Sticker received for chat <code>{pending['chat_id']}</code>. Press Confirm to whitelist.", reply_markup=confirm_kb_pm)
                if copied:
                    asyncio.create_task(schedule_delete(copied, CONFIRM_MSG_DELETE_SECONDS))
                asyncio.create_task(schedule_delete(pm_text, CONFIRM_MSG_DELETE_SECONDS))
            except Exception:
                await message.reply_text("Sticker received. Press Confirm to whitelist.", reply_markup=confirm_kb_pm)
            return
        return

    # handle bulk_block pending
    pending_id = str(pending["_id"])
    if pending.get("state") != "open":
        await message.reply_text("This bulk-block request is no longer open.")
        return

    st = message.sticker
    set_name = getattr(st, "set_name", None) or ""
    if not set_name:
        await message.reply_text("This sticker doesn't belong to a sticker pack; please send stickers from the packs you want to block.")
        return

    current_stickers = pending.get("stickers", []) or []
    if len(current_stickers) >= 200:
        await message.reply_text("You've reached the maximum sticker limit for this bulk block. Send DONE to finalize.")
        return

    file_unique_id = st.file_unique_id
    push_sticker_to_pending(pending_id, file_unique_id)
    push_setname_to_pending(pending_id, set_name)
    updated = get_pending_action(pending_id)
    set_names = updated.get("set_names", []) or []
    stickers = updated.get("stickers", []) or []
    await message.reply_text(f"Sticker added. Collected packs: {len(set_names)}. Total stickers collected: {len(stickers)}. Send more or DONE to finalize.", quote=True)


@app.on_message(filters.private & filters.text)
async def private_text_handler(client: Client, message: Message):
    user = message.from_user
    txt = (message.text or "").strip()
    if txt.lower() == "done":
        pending = get_latest_pending_for_admin(user.id, "bulk_block")
        if not pending:
            await message.reply_text("No open bulk-block request found.")
            return
        if pending.get("state") != "open":
            await message.reply_text("This bulk-block request is no longer open.")
            return
        set_names = pending.get("set_names", []) or []
        chat_id = pending.get("chat_id")
        if not set_names:
            await message.reply_text("No sticker packs were collected. Please send stickers from the packs you want to block.")
            return
        blocked = []
        for set_name in set_names:
            try:
                add_pack_blacklist(chat_id, set_name)
                blocked.append(set_name)
            except Exception as e:
                log.warning(f"Failed to blacklist pack {set_name} for chat {chat_id}: {e}")
        finalize_pending_action(str(pending["_id"]))
        try:
            packs_list = "\n".join([f"• {s}" for s in blocked])
            await message.reply_text(f"✅ Blacklisted {len(blocked)} pack(s) for chat <code>{chat_id}</code>:\n{packs_list}")
        except Exception:
            pass
        try:
            await safe_send_message(client, chat_id, f"⚠️ Admin {user.mention} blacklisted {len(blocked)} sticker pack(s) for this chat.", thread_id=None)
        except Exception:
            pass
        if LOG_CHAT_ID:
            try:
                await client.send_message(LOG_CHAT_ID, f"📦 Bulk-block finalized by {user.mention} for chat <code>{chat_id}</code>. Packs:\n{', '.join(blocked)}")
            except Exception:
                pass
        return

    if txt.lower() in ("cancel", "abort"):
        pending = get_latest_pending_for_admin(user.id, "bulk_block")
        if pending:
            cancel_pending_action(str(pending["_id"]))
            await message.reply_text("Bulk-block request cancelled.")
        else:
            await message.reply_text("No open request found.")
        return

    # other text ignored
    return


# -------------------------------------------------
# Callbacks: confirmations, cancels, navigation, mod actions
# -------------------------------------------------
@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    data = query.data or ""
    user = query.from_user

    # navigation
    if data == "page_main":
        me = await client.get_me()
        await edit_main_message(query.message, get_main_text(), build_main_keyboard(me.username or "NSFWGuardBot"))
        await query.answer()
        return
    if data == "page_how":
        me = await client.get_me()
        await edit_main_message(query.message, get_how_text(), build_subpage_keyboard(me.username or "NSFWGuardBot"))
        await query.answer()
        return
    if data == "page_features":
        me = await client.get_me()
        await edit_main_message(query.message, get_features_text(), build_subpage_keyboard(me.username or "NSFWGuardBot"))
        await query.answer()
        return
    if data == "page_perms":
        me = await client.get_me()
        await edit_main_message(query.message, get_perms_text(), build_subpage_keyboard(me.username or "NSFWGuardBot"))
        await query.answer()
        return
    if data == "page_about":
        me = await client.get_me()
        await edit_main_message(query.message, get_about_text(), build_subpage_keyboard(me.username or "NSFWGuardBot"))
        await query.answer()
        return

    # close log
    if data == "close_log":
        try:
            await query.message.delete()
            await query.answer()
        except Exception:
            await query.answer("Unable to remove message.", show_alert=True)
        return

    # free_confirm
    m = re.match(r"^free_confirm:([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(1)
        pending = get_pending_action(pending_id)
        if not pending:
            await query.answer("Request expired or invalid.", show_alert=True)
            return
        if user.id != int(pending["admin_user_id"]):
            await query.answer("You are not allowed to confirm this request.", show_alert=True)
            return
        file_unique_id = pending.get("file_unique_id", "")
        chat_id = pending.get("chat_id")
        if not file_unique_id:
            await query.answer("No sticker provided.", show_alert=True)
            return
        add_sticker_whitelist(chat_id, file_unique_id)
        finalize_pending_action(pending_id)
        try:
            await query.edit_message_text("✅ Sticker has been whitelisted for that chat.")
        except Exception:
            pass
        await query.answer("Sticker whitelisted.")
        if LOG_CHAT_ID:
            try:
                await client.send_message(LOG_CHAT_ID, f"✅ Sticker whitelisted in chat <code>{chat_id}</code> by admin {user.mention}.")
            except Exception:
                pass
        return

    # cancels
    m = re.match(r"^(free_cancel|free_cancel_group|unblack_cancel|unblack_cancel_group|block_cancel|block_cancel_group):([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(2)
        pending = get_pending_action(pending_id)
        if pending:
            cancel_pending_action(pending_id)
        try:
            await query.edit_message_text("❌ Request cancelled.")
        except Exception:
            pass
        await query.answer("Cancelled.")
        return

    # unblack_confirm
    m = re.match(r"^unblack_confirm:([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(1)
        pending = get_pending_action(pending_id)
        if not pending:
            await query.answer("Request not found or expired.", show_alert=True)
            return
        if user.id != int(pending["admin_user_id"]):
            await query.answer("You are not authorized to confirm this request.", show_alert=True)
            return
        set_name = pending.get("set_name", "")
        chat_id = pending.get("chat_id")
        if set_name:
            remove_pack_blacklist(chat_id, set_name)
        finalize_pending_action(pending_id)
        try:
            await query.edit_message_text(f"✅ Pack <code>{set_name}</code> removed from blacklist for chat <code>{chat_id}</code>.")
        except Exception:
            pass
        await query.answer("Done.")
        if LOG_CHAT_ID:
            try:
                await client.send_message(LOG_CHAT_ID, f"✅ Pack {set_name} un-blacklisted by {user.mention} for chat <code>{chat_id}</code>.")
            except Exception:
                pass
        return

    # mod actions from inline logs: mod_action:<action>:<chat_id>:<user_id>
    m = re.match(r"^mod_action:(unmute|ban):(-?\d+):(\d+)$", data)
    if m:
        action = m.group(1)
        target_chat = int(m.group(2))
        target_user = int(m.group(3))

        caller_id = user.id
        allowed = (caller_id in OWNER_IDS)
        if not allowed:
            try:
                allowed = await is_user_admin(client, target_chat, caller_id)
            except Exception:
                allowed = False
        if not allowed:
            await query.answer("You do not have permission to perform this action in the target chat.", show_alert=True)
            return

        if action == "unmute":
            permissions = ChatPermissions(
                can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True,
                can_add_web_page_previews=True, can_change_info=False, can_invite_users=True, can_pin_messages=False,
            )
            try:
                await client.restrict_chat_member(target_chat, target_user, permissions=permissions, until_date=0)
                await query.edit_message_text(f"🔈 User <a href='tg://user?id={target_user}'>user</a> has been unmuted in chat <code>{target_chat}</code>.")
                await query.answer("User unmuted.")
            except Exception as e:
                await query.answer(f"Failed to unmute: {e}", show_alert=True)
        elif action == "ban":
            try:
                await client.ban_chat_member(target_chat, target_user)
                await query.edit_message_text(f"⛔ User <a href='tg://user?id={target_user}'>user</a> has been banned from chat <code>{target_chat}</code>.")
                await query.answer("User banned.")
            except Exception as e:
                await query.answer(f"Failed to ban: {e}", show_alert=True)
        return

    await query.answer()


# -------------------------------------------------
# Main media scanning handler for groups (stickers/photos/videos/docs/animation)
# - deletes NSFW content, blacklists packs, increments violations, mutes if threshold exceeded
# - respects per-chat whitelist and pack blacklist
# - supports topics by preserving message_thread_id when sending logs
# -------------------------------------------------
@app.on_message(filters.group & (filters.sticker | filters.photo | filters.video | filters.animation | filters.document))
async def group_media_handler(client: Client, message: Message):
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    user = message.from_user
    if not user:
        return

    try:
        # Sticker early path: pack blacklist & whitelist checks
        if message.sticker:
            set_name = getattr(message.sticker, "set_name", None) or ""
            file_unique_id = message.sticker.file_unique_id
            if is_sticker_whitelisted(chat_id, file_unique_id):
                log.debug(f"Sticker {file_unique_id} whitelisted in chat {chat_id}, skipping.")
                return
            if set_name and is_pack_blacklisted(chat_id, set_name):
                try:
                    await message.delete()
                except Exception:
                    pass
                new_count = increment_violation(chat_id, user.id)
                log.info(f"[PACK_DELETE] Deleted sticker from blacklisted pack {set_name} in chat {chat_id}. user={user.id} count={new_count}")
                if new_count > PACK_STICKER_LIMIT and not await is_user_admin(client, chat_id, user.id) and await bot_can_restrict_members(client, chat_id):
                    await handle_nsfw_sticker_violation(client, message, score=0.0, reason=f"Sent stickers from blacklisted pack {set_name}")
                try:
                    sent = await safe_send_message(client, chat_id, f"🗑 Deleted sticker from blacklisted pack <code>{set_name}</code> by {user.mention}.", thread_id=thread_id)
                    if sent:
                        asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
                except Exception:
                    pass
                return

        # Download / prepare files for scanning
        tmpdir = tempfile.mkdtemp(prefix="nsfwscan_")
        paths: List[str] = []
        try:
            if message.sticker:
                file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "sticker"))
                if file_path and file_path.endswith(".tgs"):
                    out_png = os.path.join(tmpdir, "sticker.png")
                    converted = convert_tgs_to_png(file_path, out_png)
                    if converted:
                        p = prepare_image_for_detector(converted, tmpdir)
                        if p:
                            paths.append(p)
                else:
                    p = prepare_image_for_detector(file_path, tmpdir)
                    if p:
                        paths.append(p)
            elif message.photo:
                file_path = await client.download_media(message.photo.file_id, file_name=os.path.join(tmpdir, "photo"))
                p = prepare_image_for_detector(file_path, tmpdir)
                if p:
                    paths.append(p)
            elif message.animation:
                file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "anim"))
                frames = extract_video_frames(file_path, tmpdir, max_frames=3)
                for f in frames:
                    p = prepare_image_for_detector(f, tmpdir)
                    if p:
                        paths.append(p)
            elif message.video or (message.document and (getattr(message.document, "mime_type", "") or "").startswith("video/")):
                file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "video"))
                frames = extract_video_frames(file_path, tmpdir, max_frames=3)
                for f in frames:
                    p = prepare_image_for_detector(f, tmpdir)
                    if p:
                        paths.append(p)
            elif message.document:
                file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "doc"))
                p = prepare_image_for_detector(file_path, tmpdir)
                if p:
                    paths.append(p)

            score = scan_images_for_nsfw(paths)
            if score >= NSFW_THRESHOLD:
                # if sticker -> blacklist pack
                if message.sticker:
                    set_name = getattr(message.sticker, "set_name", None) or ""
                    if set_name:
                        add_pack_blacklist(chat_id, set_name)
                        try:
                            sent = await safe_send_message(client, chat_id, f"🚫 Automatically blacklisted sticker pack <code>{set_name}</code> in chat <code>{chat_id}</code> (explicit sticker detected).", thread_id=thread_id)
                            if sent:
                                asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
                        except Exception:
                            pass
                # delete and handle violation/mute
                await delete_nsfw_message(client, message, score)
                await handle_nsfw_sticker_violation(client, message, score, reason=f"NSFW detection score {score:.2f}")
            else:
                log.debug(f"[SCAN] No explicit NSFW (score {score:.2f}) for message {getattr(message, 'message_id', getattr(message, 'id', None))} in chat {chat_id}")
        finally:
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"Error in group_media_handler: {e}")


# -------------------------------------------------
# Start the bot
# -------------------------------------------------
if __name__ == "__main__":
    log.info("Starting NSFW Guard bot...")
    app.run()
