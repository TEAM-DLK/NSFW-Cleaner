# DLK NSFW Cleaner - FULL CLEAN FINAL FILE (LOGS PER-GROUP ADMINS ONLY)
# - /free whitelists the WHOLE STICKER PACK for that chat
# - Per-chat PACK WHITELIST (overrides blacklist & scanning)
# - When a user is muted, bot sends PRIVATE logs to group owner & key admins
# - No mute logs go to LOG_CHAT_ID channel
# - Start / Help UI simplified and more step-by-step
# - All previous flows (/free, /unfree, /blockpack, detector, etc.) kept working

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
from pyrogram.enums import ChatMemberStatus, ChatMembersFilter

try:
    from nudenet import NudeDetector
except Exception:
    NudeDetector = None

from PIL import Image, UnidentifiedImageError

# -------------------------------------------------
# Load environment
# -------------------------------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.30"))

NSFW_STICKER_LIMIT = int(os.getenv("NSFW_STICKER_LIMIT", "2"))
PACK_STICKER_LIMIT = int(os.getenv("PACK_STICKER_LIMIT", str(NSFW_STICKER_LIMIT)))
MUTE_DURATION_SECONDS = int(os.getenv("MUTE_DURATION_SECONDS", "86400"))
CONFIRM_MSG_DELETE_SECONDS = int(os.getenv("CONFIRM_MSG_DELETE_SECONDS", "20"))
DELETE_LOG_MESSAGE_SECONDS = int(os.getenv("DELETE_LOG_MESSAGE_SECONDS", "20"))

MONGO_URI = os.getenv("MONGO_URI", "").strip()
if not MONGO_URI:
    raise SystemExit("MONGO_URI is not set in environment. Set it in your .env file.")

LOG_CHAT_ID_ENV = os.getenv("LOG_CHAT_ID", "").strip()
# Reserved for future use; current file does NOT send mute logs to this channel
LOG_CHAT_ID = LOG_CHAT_ID_ENV if LOG_CHAT_ID_ENV else None

OWNER_IDS = set()
owner_env = os.getenv("OWNER_IDS", "").strip()
if owner_env:
    try:
        OWNER_IDS = set(int(s) for s in re.split(r"[,\s]+", owner_env) if s)
    except Exception:
        OWNER_IDS = set()

START_TIME = time.time()

OFFICIAL_CHANNEL = "https://t.me/DLKDevelopers"
LOG_PUBLIC_URL = "https://t.me/DOOZY_OFF"
START_PHOTO_URL = "https://i.ibb.co/WNzKw5qk/DLKNSFWCleaner.png"

DEV_ABOUT_TEXT = (
    "\nDLK DEVELOPER\n"
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
# MongoDB
# -------------------------------------------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["nsfw_guard"]

whitelist_col = db["sticker_whitelist"]
whitelist_col.create_index(
    [("chat_id", ASCENDING), ("file_unique_id", ASCENDING)],
    unique=True,
)

# NEW: sticker PACK whitelist
pack_whitelist_col = db["sticker_pack_whitelist"]
pack_whitelist_col.create_index(
    [("chat_id", ASCENDING), ("set_name", ASCENDING)],
    unique=True,
)

pack_blacklist_col = db["sticker_pack_blacklist"]
pack_blacklist_col.create_index(
    [("chat_id", ASCENDING), ("set_name", ASCENDING)],
    unique=True,
)

violations_col = db["nsfw_violations"]
violations_col.create_index(
    [("chat_id", ASCENDING), ("user_id", ASCENDING)],
    unique=True,
)

pending_col = db["pending_actions"]
try:
    pending_col.create_index("ts", expireAfterSeconds=3600)
except Exception:
    pass

# --- DB helper functions ---
def is_sticker_whitelisted(chat_id: int, file_unique_id: str) -> bool:
    doc = whitelist_col.find_one({"chat_id": chat_id, "file_unique_id": file_unique_id})
    return doc is not None


def add_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.update_one(
        {"chat_id": chat_id, "file_unique_id": file_unique_id},
        {"$set": {"chat_id": chat_id, "file_unique_id": file_unique_id, "ts": int(time.time())}},
        upsert=True,
    )


def remove_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.delete_one({"chat_id": chat_id, "file_unique_id": file_unique_id})


def is_pack_blacklisted(chat_id: int, set_name: str) -> bool:
    if not set_name:
        return False
    doc = pack_blacklist_col.find_one({"chat_id": chat_id, "set_name": set_name})
    return doc is not None


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


# NEW: pack whitelist helpers
def is_pack_whitelisted(chat_id: int, set_name: str) -> bool:
    if not set_name:
        return False
    doc = pack_whitelist_col.find_one({"chat_id": chat_id, "set_name": set_name})
    return doc is not None


def add_pack_whitelist(chat_id: int, set_name: str) -> None:
    if not set_name:
        return
    pack_whitelist_col.update_one(
        {"chat_id": chat_id, "set_name": set_name},
        {"$set": {"chat_id": chat_id, "set_name": set_name, "ts": int(time.time())}},
        upsert=True,
    )


def increment_violation(chat_id: int, user_id: int) -> int:
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
        return pending_col.find_one(
            {"admin_user_id": admin_user_id, "action": action_type, "state": "open"},
            sort=[("ts", -1)]
        )
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
app = Client(
    "nsfw_guard_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# -------------------------------------------------
# Detector & labels
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
# Helpers (conversion, download, etc.)
# -------------------------------------------------
async def is_bot_admin(client: Client, chat_id: int) -> bool:
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
        log.info(f"[ADMIN CHECK] status={member.status}, member={member}")
        if member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            log.info("[ADMIN CHECK] Bot is not owner/admin.")
            return False
        privileges = getattr(member, "privileges", None)
        if privileges is not None:
            can_delete = bool(getattr(privileges, "can_delete_messages", False))
            log.info(f"[ADMIN CHECK] privileges.can_delete_messages = {can_delete}")
            return can_delete
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


def extract_video_frames(src_path: str, temp_dir: str, max_frames: int = 3) -> List[str]:
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


def ffmpeg_convert_to_jpeg(in_path: str, out_path: str) -> Optional[str]:
    try:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            in_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            out_path,
        ]
        subprocess.run(cmd, check=True)
        if os.path.exists(out_path):
            return out_path
    except Exception as e:
        log.warning(f"ffmpeg conversion failed for {in_path}: {e}")
    return None


def convert_webp_to_jpeg_try_pil(webp_path: str, out_path: str) -> Optional[str]:
    try:
        with Image.open(webp_path) as img:
            rgb = img.convert("RGB")
            rgb.save(out_path, format="JPEG", quality=85)
        return out_path
    except UnidentifiedImageError as e:
        log.debug(f"PIL could not identify image {webp_path}: {e}")
        return None
    except Exception as e:
        log.warning(f"PIL webp->jpeg conversion failed: {e}")
        return None


def prepare_image_for_detector(src_path: str, out_dir: str) -> Optional[str]:
    if not src_path or not os.path.exists(src_path):
        return None
    try:
        out_path = os.path.join(out_dir, "scan_image.jpg")
        try:
            img = Image.open(src_path).convert("RGB")
            img.save(out_path, format="JPEG", quality=85)
            return out_path
        except UnidentifiedImageError:
            log.debug(f"PIL cannot identify {src_path}; attempting specialized conversions.")
            conv = convert_webp_to_jpeg_try_pil(src_path, out_path)
            if conv:
                return conv
            if src_path.lower().endswith(".tgs"):
                tmp_png = os.path.join(out_dir, "tgs_conv.png")
                conv_tgs = convert_tgs_to_png(src_path, tmp_png)
                if conv_tgs:
                    try:
                        img = Image.open(conv_tgs).convert("RGB")
                        img.save(out_path, format="JPEG", quality=85)
                        return out_path
                    except Exception as e:
                        log.warning(f"Failed to save tgs->jpeg after convert: {e}")
            ff = ffmpeg_convert_to_jpeg(src_path, out_path)
            if ff:
                return ff
            return None
        except Exception as e:
            log.warning(f"prepare_image_for_detector primary attempt failed for {src_path}: {e}")
            out_path = os.path.join(out_dir, "scan_image.jpg")
            ff = ffmpeg_convert_to_jpeg(src_path, out_path)
            if ff:
                return ff
            return None
    except Exception as e:
        log.warning(f"prepare_image_for_detector failed for {src_path}: {e}")
        return None


def scan_images_for_nsfw(image_paths: List[str]) -> float:
    if not image_paths:
        return 0.0
    max_score = 0.0
    detector = None
    try:
        detector = get_detector()
    except Exception as e:
        log.warning(f"Detector not available: {e}")
        return 0.0

    for path in image_paths:
        if not path or not os.path.exists(path):
            log.debug(f"[SCAN] Skipping missing path: {path}")
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
                    log.info(f"[DETECT] EXPLICIT HIT label={label}, score={score:.2f}")
                    if score > max_score:
                        max_score = score
                else:
                    log.debug(f"[DETECT] Ignoring non-explicit label={label}, score={score:.2f}")
        except Exception as e:
            log.warning(f"[DETECT] Scanning failed for {path}: {e}")
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
            log.warning(f"Failed to delete NSFW message with message.delete(): {e}; trying client.delete_messages fallback.")
            try:
                await client.delete_messages(chat.id, getattr(message, "message_id", getattr(message, "id", None)))
            except Exception as e2:
                log.warning(f"Fallback client.delete_messages also failed: {e2}")
        log.info(
            f"[DELETE] NSFW content deleted in chat={chat.id}, "
            f"user={user.id if user else 'N/A'}, score={score:.2f}"
        )
    except Exception as e:
        log.warning(f"Failed in delete_nsfw_message: {e}")

    # In-chat log only (NO LOG CHANNEL)
    try:
        reason = f"NSFW detection score {score:.2f} >= threshold {NSFW_THRESHOLD}"
        mention = user.mention if user else "<b>Anonymous / Unknown</b>"

        kb_rows = []
        if user and getattr(user, "id", None):
            kb_rows.append(
                [
                    InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{chat.id}:{user.id}"),
                    InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{chat.id}:{user.id}")
                ]
            )
        kb_rows.append([InlineKeyboardButton("✖️ Close", callback_data="close_log")])
        kb = InlineKeyboardMarkup(kb_rows)

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
    add_url = f"https://t.me/{bot_username}?startgroup=nsfw_guard"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add me to your group", url=add_url)],
            [
                InlineKeyboardButton("📖 How to use", callback_data="page_how"),
                InlineKeyboardButton("🛡 Admin setup", callback_data="page_perms"),
            ],
            [
                InlineKeyboardButton("🧰 Commands", callback_data="page_features"),
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
        "🛡 <b>DLK NSFW Cleaner</b>\n\n"
        "Auto-delete nude / explicit NSFW content from your Telegram groups.\n\n"
        "• Scans stickers, photos, GIFs & videos\n"
        "• Deletes explicit NSFW instantly\n"
        "• Can block or allow entire sticker packs per chat\n"
        f"• Mutes users after <b>{NSFW_STICKER_LIMIT}</b> NSFW violations (non-admins)\n\n"
        "<b>Quick start:</b>\n"
        "① Add me to your group\n"
        "② Make me admin (delete + ban/restrict)\n"
        "③ I start auto-cleaning NSFW content\n\n"
        f"⏱ Uptime: <code>{uptime}</code>\n\n"
        "Use the buttons below to see how to use, setup and commands."
    )


def get_how_text() -> str:
    return (
        "📖 <b>How to use</b>\n\n"
        "① Add the bot to your group\n"
        "② Promote as admin with:\n"
        "   • Delete messages\n"
        "   • Ban/Restrict users\n\n"
        "③ The bot will now:\n"
        "   • Scan new stickers/photos/GIFs/videos\n"
        "   • Delete explicit NSFW content\n"
        "   • Auto-mute repeat NSFW senders\n\n"
        "🧩 Pack control:\n"
        "• /free – whitelist a sticker & its pack for this chat\n"
        "• /unfree – remove a sticker from whitelist\n"
        "• /blockpack – bulk-block packs via private chat\n\n"
        f"Helper messages auto-delete after <code>{CONFIRM_MSG_DELETE_SECONDS}</code> seconds to keep chats clean."
    )


def get_features_text() -> str:
    return (
        "🧰 <b>Commands & Features</b>\n\n"
        "• /about – about the bot\n"
        "• /status – show my admin permissions in this group\n"
        "• /ping – bot uptime\n\n"
        "NSFW control:\n"
        "• /free – whitelist sticker & its pack for this chat\n"
        "• /unfree – un-whitelist a sticker (reply to sticker)\n"
        "• /blockpack – start pack-block flow in PM\n\n"
        f"Auto-mute after <code>{NSFW_STICKER_LIMIT}</code> NSFW violations (non-admin users)."
    )


def get_perms_text() -> str:
    return (
        "🛡 <b>Admin setup (permissions)</b>\n\n"
        "To work correctly in a group, I need:\n\n"
        "• Be <b>Admin</b>\n"
        "• <b>Delete messages</b>\n"
        "• <b>Ban/Restrict users</b>\n\n"
        "In Topic / Forum groups:\n"
        "• Make sure I'm admin at MAIN group level\n"
        "• Not only inside one topic\n"
    )


def get_about_text() -> str:
    return (
        "ℹ️ <b>About DLK NSFW Cleaner</b>\n\n"
        "This bot automatically detects and removes nude / explicit NSFW content.\n"
        "If a sticker in a pack is explicit, that pack can be auto-blacklisted per chat.\n\n"
        f"If a user keeps sending NSFW content beyond <b>{NSFW_STICKER_LIMIT}</b> times, "
        "the bot will try to mute them (non-admins only).\n\n"
        f"<b>Developer:</b>\n<code>{DEV_ABOUT_TEXT}</code>\n"
        f"<b>Logs & Updates:</b> {LOG_PUBLIC_URL}"
    )


def build_subpage_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    add_url = f"https://t.me/{bot_username}?startgroup=nsfw_guard"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ Back", callback_data="page_main"),
                InlineKeyboardButton("➕ Add to group", url=add_url),
            ],
            [
                InlineKeyboardButton("📖 How to use", callback_data="page_how"),
                InlineKeyboardButton("🛡 Admin setup", callback_data="page_perms"),
            ],
            [
                InlineKeyboardButton("🧰 Commands", callback_data="page_features"),
                InlineKeyboardButton("ℹ️ About", callback_data="page_about"),
            ],
            [
                InlineKeyboardButton("📢 Updates & Logs", url=LOG_PUBLIC_URL),
            ],
        ]
    )


async def edit_main_message(msg: Message, text: str, keyboard: InlineKeyboardMarkup):
    if msg is None:
        return
    try:
        if getattr(msg, "photo", None):
            await msg.edit_caption(text, reply_markup=keyboard)
        else:
            await msg.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        log.warning(f"Failed to edit main message: {e}")

# -------------------------------------------------
# Commands: start/help/about/status/ping
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

    # deep-link flows
    if payload.startswith("free_") or payload.startswith("unblack_") or payload.startswith("block_"):
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
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")
                ]]
            )
            await message.reply_text(
                f"You're about to whitelist a sticker and its pack in chat <code>{pending['chat_id']}</code>.\n"
                "Press Confirm to allow this sticker pack in that group.",
                reply_markup=kb,
            )
            return
        elif action == "unblacklist":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Remove pack blacklist for chat", callback_data=f"unblack_confirm:{pending_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"unblack_cancel:{pending_id}")
                ]]
            )
            await message.reply_text(
                f"You're about to remove the pack-level blacklist for pack <code>{pending.get('set_name')}</code> in chat <code>{pending['chat_id']}</code>.\n"
                "Press Confirm to remove the blacklist for that chat.",
                reply_markup=kb,
            )
            return
        elif action == "bulk_block":
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data=f"block_cancel:{pending_id}")]]
            )
            await message.reply_text(
                "📦 Bulk pack block\n\n"
                "Send stickers (up to 50) from one or more packs here, then send <b>DONE</b>.\n"
                "Sticker packs from those stickers will be blocked in the target chat.",
                reply_markup=kb,
            )
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
    await message.reply_text(f"🏓 <b>Pong!</b>\nUptime: <code>{uptime}</code>")

# -------------------------------------------------
# /free, /unfree, /blockpack
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

    # reply-mode: /free on sticker
    if reply and reply.sticker:
        sticker_msg = reply
        file_unique_id = sticker_msg.sticker.file_unique_id
        set_name = getattr(sticker_msg.sticker, "set_name", None) or ""
        pending_id = create_pending_action("whitelist", chat_id, user.id, file_unique_id, set_name)
        deep_link = f"https://t.me/{bot_username}?start=free_{pending_id}"
        confirm_kb_group = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm in private", url=deep_link),
                InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel_group:{pending_id}")
            ]]
        )
        try:
            grp_msg = await message.reply_text(
                "✅ Whitelist request created.\nAdmin: open my private chat and confirm.\n\n"
                "This will whitelist the sticker and its pack for this chat.",
                reply_markup=confirm_kb_group,
                quote=True,
            )
            asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
        except Exception as e:
            log.warning(f"Failed to send group confirm button: {e}")

        # PM helper
        try:
            copied = await safe_copy_message(
                client,
                user.id,
                sticker_msg.chat.id,
                getattr(sticker_msg, "message_id", getattr(sticker_msg, "id", None))
            )
            confirm_kb_pm = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")
                ]]
            )
            pm_text = await client.send_message(
                user.id,
                f"You're about to whitelist a sticker and its pack in chat <code>{chat_id}</code> "
                f"({message.chat.title or 'group'}).\n\n"
                "Press Confirm to allow this pack in that group.",
                reply_markup=confirm_kb_pm,
            )
            if copied:
                asyncio.create_task(schedule_delete(copied, CONFIRM_MSG_DELETE_SECONDS))
            asyncio.create_task(schedule_delete(pm_text, CONFIRM_MSG_DELETE_SECONDS))
        except Exception as e:
            log.info(f"Could not send PM to admin {user.id}: {e}")
    else:
        # no-reply mode: /free then go to PM and send sticker
        pending_id = create_pending_action("whitelist", chat_id, user.id, "", "")
        deep_link = f"https://t.me/{bot_username}?start=free_{pending_id}"
        confirm_kb_group = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("➡️ Open bot private (send sticker there)", url=deep_link),
                InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel_group:{pending_id}")
            ]]
        )
        try:
            grp_msg = await message.reply_text(
                "✅ Whitelist request created.\n\n"
                "① Click the button to open my private chat\n"
                "② Send the sticker from the pack you want to allow\n"
                "③ Press Confirm in PM\n\n"
                "The sticker's pack will be allowed only in this group.",
                reply_markup=confirm_kb_group,
                quote=True,
            )
            asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
        except Exception as e:
            log.warning(f"Failed to send group helper for no-reply /free: {e}")

    try:
        await safe_send_message(
            client,
            chat_id,
            f"🛡 Whitelist requested by admin {user.mention} in chat <code>{chat_id}</code>.\n"
            "After confirming in PM, the sticker pack will be allowed here.",
            thread_id=thread_id
        )
    except Exception:
        pass


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

    # If pack is blacklisted, we offer unblacklist flow (same as before)
    if set_name and is_pack_blacklisted(chat_id, set_name):
        me = await client.get_me()
        bot_username = me.username or "NSFWGuardBot"
        pending_id = create_pending_action("unblacklist", chat_id, user.id, file_unique_id, set_name)
        deep_link = f"https://t.me/{bot_username}?start=unblack_{pending_id}"
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Remove pack-blacklist (confirm in PM)", url=deep_link),
                InlineKeyboardButton("❌ Cancel", callback_data=f"unblack_cancel_group:{pending_id}")
            ]]
        )
        try:
            grp = await message.reply_text(
                f"⚠️ Pack <code>{set_name}</code> is currently blacklisted in this chat.\n\n"
                "If you want to remove the pack-level blacklist so stickers from this pack stop being auto-deleted, "
                "confirm in my private chat.",
                reply_markup=kb,
                quote=True,
            )
            asyncio.create_task(schedule_delete(grp, CONFIRM_MSG_DELETE_SECONDS))
        except Exception:
            pass
    try:
        await safe_send_message(
            client,
            chat_id,
            f"❌ Unwhitelist requested by admin {user.mention} in chat <code>{chat_id}</code>.",
            thread_id=thread_id
        )
    except Exception:
        pass


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
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("➡️ Open private and send stickers", url=deep_link),
            InlineKeyboardButton("❌ Cancel", callback_data=f"block_cancel_group:{pending_id}")
        ]]
    )
    try:
        grp_msg = await message.reply_text(
            "📦 <b>Bulk block sticker packs</b>\n\n"
            "① Click the button to open my private chat\n"
            "② Send stickers from the pack(s) you want to block\n"
            "③ When finished, send <b>DONE</b>\n\n"
            "I will block those packs only for this chat.",
            reply_markup=kb,
            quote=True,
        )
        asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
    except Exception as e:
        log.warning(f"Failed to send group helper for /blockpack: {e}")

    try:
        await safe_send_message(
            client,
            chat_id,
            f"📦 Bulk block requested by admin {user.mention} in chat <code>{chat_id}</code>. "
            f"Pending ID: <code>{pending_id}</code>",
            thread_id=thread_id
        )
    except Exception:
        pass

# -------------------------------------------------
# Private handlers for collecting stickers and confirming actions
# -------------------------------------------------
@app.on_message(filters.private & filters.sticker)
async def private_sticker_collector(client: Client, message: Message):
    user = message.from_user
    pending = get_latest_pending_for_admin(user.id, "bulk_block")
    if not pending:
        # maybe it's /free no-reply mode
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
            pending_id = str(pending_free["_id"])
            confirm_kb_pm = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")
                ]]
            )
            try:
                copied = await safe_copy_message(
                    client,
                    user.id,
                    message.chat.id,
                    getattr(message, "message_id", getattr(message, "id", None))
                )
                pm_text = await client.send_message(
                    user.id,
                    f"Sticker received for chat <code>{pending_free['chat_id']}</code>.\n"
                    "Press Confirm to whitelist the sticker and its pack in that chat.",
                    reply_markup=confirm_kb_pm
                )
                if copied:
                    asyncio.create_task(schedule_delete(copied, CONFIRM_MSG_DELETE_SECONDS))
                asyncio.create_task(schedule_delete(pm_text, CONFIRM_MSG_DELETE_SECONDS))
            except Exception:
                await message.reply_text("Sticker received. Press Confirm to whitelist.", reply_markup=confirm_kb_pm)
            return
        return

    # bulk_block mode
    pending_id = str(pending["_id"])
    if pending["state"] != "open":
        await message.reply_text("This bulk-block request is no longer open.")
        return

    st = message.sticker
    set_name = getattr(st, "set_name", None) or ""
    if not set_name:
        await message.reply_text("This sticker doesn't belong to a sticker pack; please send stickers from the pack(s) you want to block.")
        return

    push_sticker_to_pending(pending_id, st.file_unique_id)
    push_setname_to_pending(pending_id, set_name)
    await message.reply_text(f"Collected sticker from pack <code>{set_name}</code>. Send more or send DONE to finalize.", quote=True)


@app.on_message(filters.private & filters.regex(r"^\s*DONE\s*$", flags=re.IGNORECASE))
async def private_done_handler(client: Client, message: Message):
    user = message.from_user
    pending = get_latest_pending_for_admin(user.id, "bulk_block")
    if not pending:
        await message.reply_text("No open bulk-block request was found.")
        return
    pending_id = str(pending["_id"])
    set_names = pending.get("set_names", []) or []
    if not set_names:
        await message.reply_text("No sticker packs collected. Send stickers from the pack(s) you want to block first.", quote=True)
        return
    chat_id = pending.get("chat_id")
    for set_name in set_names:
        add_pack_blacklist(chat_id, set_name)
    finalize_pending_action(pending_id)
    await message.reply_text(f"✅ Blocked {len(set_names)} sticker pack(s) for chat <code>{chat_id}</code>.", quote=True)
    try:
        await safe_send_message(
            client,
            chat_id,
            f"📦 Admin {user.mention} blocked sticker packs {', '.join(set_names)} for chat <code>{chat_id}</code>."
        )
    except Exception:
        pass

# -------------------------------------------------
# Reliable download helper
# -------------------------------------------------
async def download_media_with_retries(client: Client, message: Message, file_reference, dest_path: str, retries: int = 2, delay: float = 0.6) -> Optional[str]:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            path = await client.download_media(file_reference, file_name=dest_path)
            if path and os.path.exists(path):
                return path
        except Exception as e:
            last_exc = e
            log.debug(f"download attempt {attempt} failed for {dest_path}: {e}")
            await asyncio.sleep(delay)
    log.warning(f"download_media_with_retries failed after {retries+1} attempts: {last_exc}")
    return None

# -------------------------------------------------
# Group media handler (with pack whitelist logic)
# -------------------------------------------------
@app.on_message(filters.group & (filters.sticker | filters.photo | filters.video | filters.animation | filters.document))
async def group_media_handler(client: Client, message: Message):
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    user = message.from_user
    if not user:
        return

    if not await is_bot_admin(client, chat_id):
        log.info(f"[MEDIA HANDLER] Bot is not admin in chat {chat_id}; skipping moderation.")
        return

    try:
        # STICKER-specific handling: pack whitelist / blacklist
        if message.sticker:
            set_name = getattr(message.sticker, "set_name", None) or ""
            file_unique_id = message.sticker.file_unique_id

            # sticker whitelist
            if is_sticker_whitelisted(chat_id, file_unique_id):
                log.debug(f"Sticker {file_unique_id} whitelisted in chat {chat_id}, skipping scan.")
                return

            # PACK whitelist (NEW) – skip everything for this pack
            if set_name and is_pack_whitelisted(chat_id, set_name):
                log.debug(f"Pack {set_name} is whitelisted in chat {chat_id}, skipping scan.")
                return

            # PACK blacklist
            if set_name and is_pack_blacklisted(chat_id, set_name):
                try:
                    await message.delete()
                except Exception as e:
                    log.warning(f"Initial delete of blacklisted sticker failed: {e}; trying client.delete_messages fallback.")
                    try:
                        await client.delete_messages(chat_id, getattr(message, "message_id", getattr(message, "id", None)))
                    except Exception as e2:
                        log.warning(f"Fallback delete also failed for blacklisted sticker: {e2}")
                new_count = increment_violation(chat_id, user.id)
                log.info(f"[PACK DELETE] Deleted sticker from blacklisted pack {set_name} in chat {chat_id}. user={user.id} count={new_count}")
                if new_count > PACK_STICKER_LIMIT and not await is_user_admin(client, chat_id, user.id) and await bot_can_restrict_members(client, chat_id):
                    reason = f"Sent stickers from blacklisted pack {set_name}"
                    await handle_nsfw_sticker_violation(client, message, score=0.0, reason=reason)
                try:
                    sent = await safe_send_message(
                        client,
                        chat_id,
                        f"🗑 Deleted sticker from blacklisted pack <code>{set_name}</code> by {user.mention}.",
                        thread_id=thread_id
                    )
                    if sent:
                        asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
                except Exception:
                    pass
                return

        # general NSFW scan
        tmpdir = tempfile.mkdtemp(prefix="nsfwscan_")
        paths = []
        try:
            if message.sticker:
                base_dest = os.path.join(tmpdir, "sticker")
                file_ref = getattr(message.sticker, "file_id", message)
                file_path = await download_media_with_retries(client, message, file_ref, base_dest)
                if not file_path:
                    log.warning(f"Sticker download failed for message {getattr(message,'message_id',None)}; aborting scan.")
                else:
                    prepared = prepare_image_for_detector(file_path, tmpdir)
                    if prepared:
                        paths.append(prepared)
            elif message.photo:
                file_ref = message.photo.file_id
                dest = os.path.join(tmpdir, "photo.jpg")
                file_path = await download_media_with_retries(client, message.photo, file_ref, dest)
                if file_path:
                    prepared = prepare_image_for_detector(file_path, tmpdir)
                    if prepared:
                        paths.append(prepared)
            elif message.animation:
                dest = os.path.join(tmpdir, "anim")
                file_path = await download_media_with_retries(client, message, message, dest)
                if file_path:
                    frames = extract_video_frames(file_path, tmpdir, max_frames=3)
                    for f in frames:
                        p = prepare_image_for_detector(f, tmpdir)
                        if p:
                            paths.append(p)
            elif message.video or (message.document and (getattr(message.document, "mime_type", "") or "").startswith("video/")):
                dest = os.path.join(tmpdir, "video")
                file_path = await download_media_with_retries(client, message, message, dest)
                if file_path:
                    frames = extract_video_frames(file_path, tmpdir, max_frames=3)
                    for f in frames:
                        p = prepare_image_for_detector(f, tmpdir)
                        if p:
                            paths.append(p)
            elif message.document:
                dest = os.path.join(tmpdir, "doc")
                file_path = await download_media_with_retries(client, message, message, dest)
                if file_path:
                    prepared = prepare_image_for_detector(file_path, tmpdir)
                    if prepared:
                        paths.append(prepared)

            score = scan_images_for_nsfw(paths)
            if score >= NSFW_THRESHOLD:
                # NSFW sticker -> auto blacklists pack (unless whitelisted)
                if message.sticker:
                    set_name = getattr(message.sticker, "set_name", None) or ""
                    if set_name and not is_pack_whitelisted(chat_id, set_name):
                        add_pack_blacklist(chat_id, set_name)
                        try:
                            sent = await safe_send_message(
                                client,
                                chat_id,
                                f"🚫 Automatically blacklisted sticker pack <code>{set_name}</code> in chat <code>{chat_id}</code> (explicit sticker detected).",
                                thread_id=thread_id
                            )
                            if sent:
                                asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
                        except Exception:
                            pass
                await delete_nsfw_message(client, message, score)
                await handle_nsfw_sticker_violation(client, message, score, reason=f"NSFW detection score {score:.2f}")
            else:
                log.debug(f"[SCAN] No explicit NSFW found (score {score:.2f}) for message {getattr(message, 'message_id', getattr(message, 'id', None))} in chat {chat_id}")
        finally:
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"Error in group_media_handler: {e}")

# -------------------------------------------------
# Violation handling (mute after limit)
# -------------------------------------------------
async def get_log_recipients(client: Client, chat_id: int):
    """Return list of user IDs: group owner + admins with change_info or restrict perms."""
    recipients = []
    try:
        async for member in client.get_chat_members(chat_id, filter=ChatMembersFilter.ADMINISTRATORS):
            if member.status == ChatMemberStatus.OWNER:
                recipients.append(member.user.id)
            else:
                priv = getattr(member, "privileges", None)
                if not priv:
                    continue
                if getattr(priv, "can_change_info", False) or getattr(priv, "can_restrict_members", False):
                    recipients.append(member.user.id)
    except Exception as e:
        log.warning(f"Failed to fetch admin list for private logs: {e}")
    # remove duplicates
    return list(set(recipients))


async def send_private_mute_log(client: Client, chat_id: int, user, violations: int, score: float, reason: str, kb: InlineKeyboardMarkup):
    """Send mute log to bot's inbox for group owner + important admins."""
    recipients = await get_log_recipients(client, chat_id)
    if not recipients:
        return

    # try to get chat title
    chat_title = str(chat_id)
    try:
        chat = await client.get_chat(chat_id)
        if chat and getattr(chat, "title", None):
            chat_title = chat.title
    except Exception:
        pass

    text = (
        "🚫 <b>User muted for NSFW</b>\n\n"
        f"👥 Chat: <code>{chat_title}</code> (<code>{chat_id}</code>)\n"
        f"👤 User: {user.mention}\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"📊 Last Score: <code>{score:.2f}</code>\n"
        f"🔢 Violations: <code>{violations}</code>\n"
        f"⏱ Duration: <code>{MUTE_DURATION_SECONDS}s</code>\n"
        f"📝 Reason: {reason}\n\n"
        "You can unmute or ban this user directly from this message."
    )

    for uid in recipients:
        try:
            await client.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            log.debug(f"Failed to send private mute log to {uid}: {e}")


async def notify_mute_to_log(client: Client, chat_id: int, user, violations: int, score: float, reason: str, thread_id: Optional[int] = None):
    if not chat_id:
        return
    try:
        mention = user.mention if user else "<b>Unknown</b>"
        kb_rows = []
        if user and getattr(user, "id", None):
            kb_rows.append(
                [
                    InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{chat_id}:{user.id}"),
                    InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{chat_id}:{user.id}")
                ]
            )
        kb_rows.append([InlineKeyboardButton("✖️ Close", callback_data="close_log")])
        kb = InlineKeyboardMarkup(kb_rows)

        text = (
            "🚫 <b>User muted for NSFW stickers</b>\n\n"
            f"👥 Chat: <code>{chat_id}</code>\n"
            f"👤 User: {mention}\n"
            f"🆔 User ID: <code>{user.id if user else 'N/A'}</code>\n"
            f"📊 Last Score: <code>{score:.2f}</code>\n"
            f"🔢 Violations: <code>{violations}</code>\n"
            f"⏱ Duration: <code>{MUTE_DURATION_SECONDS}s</code>\n"
            f"📝 Reason: {reason}"
        )
        # send in group
        sent = await safe_send_message(client, chat_id, text, reply_markup=kb, thread_id=thread_id)
        if sent:
            asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))

        # also send private logs to owner + key admins (bot inbox -> admins)
        if user:
            await send_private_mute_log(client, chat_id, user, violations, score, reason, kb)
    except Exception as e:
        log.warning(f"Failed to send mute log message: {e}")


async def handle_nsfw_sticker_violation(client: Client, message: Message, score: float, reason: str = "NSFW content"):
    chat_id = message.chat.id
    user = message.from_user
    thread_id = getattr(message, "message_thread_id", None)
    if not user:
        return
    if await is_user_admin(client, chat_id, user.id):
        log.info(f"[VIOLATION] User {user.id} is admin/owner, not muting. (chat={chat_id})")
        return
    new_count = increment_violation(chat_id, user.id)
    log.info(f"[VIOLATION] NSFW sticker violation for user={user.id} in chat={chat_id}. count={new_count}, limit={NSFW_STICKER_LIMIT}")
    if new_count <= NSFW_STICKER_LIMIT:
        return
    if not await bot_can_restrict_members(client, chat_id):
        log.warning(f"[VIOLATION] Bot has no restrict/ban permission in chat={chat_id}, cannot mute user={user.id}. Only deleting messages.")
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
        await client.restrict_chat_member(chat_id, user.id, permissions=permissions, until_date=until_date)
        log.info(f"[VIOLATION] User={user.id} muted in chat={chat_id} for NSFW stickers. Duration={MUTE_DURATION_SECONDS}s")
        try:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Bot Logs / Updates", url=LOG_PUBLIC_URL)]])
            reply_msg = None
            try:
                reply_msg = await message.reply_text(
                    f"🚫 {user.mention} has been muted for repeated NSFW content (>{NSFW_STICKER_LIMIT}).",
                    reply_markup=kb
                )
            except Exception:
                reply_msg = await safe_send_message(
                    client,
                    chat_id,
                    f"🚫 {user.mention} has been muted for repeated NSFW content (>{NSFW_STICKER_LIMIT}).",
                    reply_markup=kb,
                    thread_id=thread_id
                )
            if reply_msg:
                asyncio.create_task(schedule_delete(reply_msg, DELETE_LOG_MESSAGE_SECONDS))
        except Exception:
            pass
        await notify_mute_to_log(client, chat_id, user, new_count, score, reason, thread_id=thread_id)
    except Exception as e:
        log.warning(f"[VIOLATION] Failed to mute user={user.id} in chat={chat_id}: {e}")

# -------------------------------------------------
# Callback query handlers (confirm flows + mod actions)
# -------------------------------------------------
@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    data = query.data or ""
    user = query.from_user

    # page navigation
    if data == "page_main":
        me = await client.get_me()
        bot_username = me.username or "NSFWGuardBot"
        await edit_main_message(query.message, get_main_text(), build_main_keyboard(bot_username))
        await query.answer()
        return
    if data == "page_how":
        me = await client.get_me()
        bot_username = me.username or "NSFWGuardBot"
        await edit_main_message(query.message, get_how_text(), build_subpage_keyboard(bot_username))
        await query.answer()
        return
    if data == "page_features":
        me = await client.get_me()
        bot_username = me.username or "NSFWGuardBot"
        await edit_main_message(query.message, get_features_text(), build_subpage_keyboard(bot_username))
        await query.answer()
        return
    if data == "page_perms":
        me = await client.get_me()
        bot_username = me.username or "NSFWGuardBot"
        await edit_main_message(query.message, get_perms_text(), build_subpage_keyboard(bot_username))
        await query.answer()
        return
    if data == "page_about":
        me = await client.get_me()
        bot_username = me.username or "NSFWGuardBot"
        await edit_main_message(query.message, get_about_text(), build_subpage_keyboard(bot_username))
        await query.answer()
        return

    if data == "close_log":
        try:
            await query.message.delete()
            await query.answer()
        except Exception:
            await query.answer("Unable to remove message.", show_alert=True)
        return

    # Confirm whitelist
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
        set_name = pending.get("set_name") or ""
        if not file_unique_id:
            await query.answer("No sticker was provided to whitelist.", show_alert=True)
            return

        # whitelist the sticker
        add_sticker_whitelist(chat_id, file_unique_id)
        # NEW: whitelist the whole pack and remove blacklist if exists
        if set_name:
            remove_pack_blacklist(chat_id, set_name)
            add_pack_whitelist(chat_id, set_name)

        finalize_pending_action(pending_id)
        try:
            await query.edit_message_text("✅ Sticker and its pack have been whitelisted for that chat.")
        except Exception:
            pass
        await query.answer("Sticker pack whitelisted.")
        try:
            await safe_send_message(
                client,
                chat_id,
                f"✅ Sticker pack <code>{set_name or 'unknown'}</code> whitelisted in chat <code>{chat_id}</code> by admin {user.mention}."
            )
        except Exception:
            pass
        return

    m = re.match(r"^free_cancel:([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(1)
        pending = get_pending_action(pending_id)
        if not pending:
            await query.answer("Request expired or invalid.", show_alert=True)
            return
        if user.id != int(pending["admin_user_id"]):
            await query.answer("You are not allowed to cancel this request.", show_alert=True)
            return
        cancel_pending_action(pending_id)
        try:
            await query.edit_message_text("❌ Whitelist request cancelled.")
        except Exception:
            pass
        await query.answer("Cancelled.")
        return

    m = re.match(r"^unblack_confirm:([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(1)
        pending = get_pending_action(pending_id)
        if not pending:
            await query.answer("Request expired or invalid.", show_alert=True)
            return
        if user.id != int(pending["admin_user_id"]):
            await query.answer("You are not allowed to confirm this request.", show_alert=True)
            return
        chat_id = pending.get("chat_id")
        set_name = pending.get("set_name")
        if set_name:
            remove_pack_blacklist(chat_id, set_name)
        finalize_pending_action(pending_id)
        try:
            await query.edit_message_text("✅ Pack blacklist removed for the chat.")
        except Exception:
            pass
        await query.answer("Done.")
        try:
            await safe_send_message(
                client,
                chat_id,
                f"✅ Pack <code>{set_name}</code> un-blacklisted for chat <code>{chat_id}</code> by {user.mention}."
            )
        except Exception:
            pass
        return

    m = re.match(r"^unblack_cancel:([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(1)
        pending = get_pending_action(pending_id)
        if not pending:
            await query.answer("Request expired or invalid.", show_alert=True)
            return
        if user.id != int(pending["admin_user_id"]):
            await query.answer("You are not allowed to cancel this request.", show_alert=True)
            return
        cancel_pending_action(pending_id)
        try:
            await query.edit_message_text("❌ Unblacklist request cancelled.")
        except Exception:
            pass
        await query.answer("Cancelled.")
        return

    m = re.match(r"^block_cancel:([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(1)
        pending = get_pending_action(pending_id)
        if not pending:
            await query.answer("Request expired or invalid.", show_alert=True)
            return
        if user.id != int(pending["admin_user_id"]):
            await query.answer("You are not allowed to cancel this request.", show_alert=True)
            return
        cancel_pending_action(pending_id)
        try:
            await query.edit_message_text("❌ Bulk block request cancelled.")
        except Exception:
            pass
        await query.answer("Cancelled.")
        return

    m = re.match(r"^free_cancel_group:([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(1)
        cancel_pending_action(pending_id)
        try:
            await query.edit_message_text("Cancelled (group helper).")
        except Exception:
            pass
        await query.answer("Cancelled.")
        return

    m = re.match(r"^unblack_cancel_group:([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(1)
        cancel_pending_action(pending_id)
        try:
            await query.edit_message_text("Cancelled (group helper).")
        except Exception:
            pass
        await query.answer("Cancelled.")
        return

    m = re.match(r"^block_cancel_group:([0-9a-fA-F]+)$", data)
    if m:
        pending_id = m.group(1)
        cancel_pending_action(pending_id)
        try:
            await query.edit_message_text("Cancelled (group helper).")
        except Exception:
            pass
        await query.answer("Cancelled.")
        return

    # moderator actions from inline buttons (unmute / ban)
    m = re.match(r"^mod_action:(unmute|ban):(-?\d+):(\d+)$", data)
    if m:
        action = m.group(1)
        target_chat = int(m.group(2))
        target_user = int(m.group(3))

        if not target_chat or not target_user:
            await query.answer("Invalid target chat or user.", show_alert=True)
            return

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

        if not await bot_can_restrict_members(client, target_chat):
            await query.answer("Bot lacks restrict/ban permissions in target chat.", show_alert=True)
            return

        if action == "unmute":
            permissions = ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False,
            )
            try:
                await client.restrict_chat_member(target_chat, target_user, permissions=permissions)
                try:
                    await query.edit_message_text(
                        f"🔈 User <a href='tg://user?id={target_user}'>user</a> has been unmuted in chat <code>{target_chat}</code>.",
                        parse_mode="html"
                    )
                except Exception:
                    pass
                await query.answer("User unmuted.")
            except Exception as e:
                log.warning(f"Initial unmute failed for {target_user} in {target_chat}: {e}")
                try:
                    await client.restrict_chat_member(target_chat, target_user, permissions=permissions, until_date=0)
                    try:
                        await query.edit_message_text(
                            f"🔈 User <a href='tg://user?id={target_user}'>user</a> has been unmuted in chat <code>{target_chat}</code>.",
                            parse_mode="html"
                        )
                    except Exception:
                        pass
                    await query.answer("User unmuted.")
                except Exception as e2:
                    log.warning(f"Fallback unmute also failed: {e2}")
                    await query.answer(f"Failed to unmute: {e2}", show_alert=True)
            return
        elif action == "ban":
            try:
                await client.ban_chat_member(target_chat, target_user)
                try:
                    await query.edit_message_text(
                        f"⛔ User <a href='tg://user?id={target_user}'>user</a> has been banned from chat <code>{target_chat}</code>.",
                        parse_mode="html"
                    )
                except Exception:
                    pass
            except Exception as e:
                await query.answer(f"Failed to ban: {e}", show_alert=True)
                return
            await query.answer("User banned.")
            return

    await query.answer()

# -------------------------------------------------
# Start the bot
# -------------------------------------------------
if __name__ == "__main__":
    log.info("Starting DLK NSFW Cleaner (FULL CLEAN FINAL FILE).")
    app.run()
