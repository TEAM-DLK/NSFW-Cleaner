# NSFW Cleaner - group/topic-aware, whitelist/pack-blacklist, mute after limit, mod inline actions
# Requirements: Python 3.8+, pyrogram, tgcrypto, pymongo, python-dotenv, nudenet, pillow, ffmpeg, (lottie optional)
# Place this file in your bot project and run after setting .env (API_ID, API_HASH, BOT_TOKEN, MONGO_URI, optional OWNER_IDS, LOG_CHAT_ID)

import os
import time
import logging
import tempfile
import shutil
import subprocess
import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional, List

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

# Lazy import of NudeNet (it may download models on first use)
try:
    from nudenet import NudeDetector
except Exception:
    NudeDetector = None

from PIL import Image

# -------------------------
# Environment / config
# -------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.75"))
NSFW_STICKER_LIMIT = int(os.getenv("NSFW_STICKER_LIMIT", "3"))
PACK_STICKER_LIMIT = int(os.getenv("PACK_STICKER_LIMIT", str(NSFW_STICKER_LIMIT)))
MUTE_DURATION_SECONDS = int(os.getenv("MUTE_DURATION_SECONDS", "86400"))

CONFIRM_MSG_DELETE_SECONDS = int(os.getenv("CONFIRM_MSG_DELETE_SECONDS", "10"))
DELETE_LOG_MESSAGE_SECONDS = int(os.getenv("DELETE_LOG_MESSAGE_SECONDS", "10"))

MONGO_URI = os.getenv("MONGO_URI", "").strip()
if not MONGO_URI:
    raise SystemExit("MONGO_URI not set in .env")

LOG_CHAT_ID_ENV = os.getenv("LOG_CHAT_ID", "").strip()
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
DEV_ABOUT_TEXT = "DLK DEVELOPER\nSEE THE FUTURE THROUGH MY VISION"

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")
log = logging.getLogger("NSFW-CLEANER")

# -------------------------
# MongoDB collections
# -------------------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["nsfw_guard"]

whitelist_col = db["sticker_whitelist"]
whitelist_col.create_index([("chat_id", ASCENDING), ("file_unique_id", ASCENDING)], unique=True)

pack_blacklist_col = db["sticker_pack_blacklist"]
pack_blacklist_col.create_index([("chat_id", ASCENDING), ("set_name", ASCENDING)], unique=True)

violations_col = db["nsfw_violations"]
violations_col.create_index([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)

pending_col = db["pending_actions"]
try:
    pending_col.create_index("ts", expireAfterSeconds=3600)
except Exception:
    pass

# -------------------------
# Helper DB functions
# -------------------------
def is_sticker_whitelisted(chat_id: int, file_unique_id: str) -> bool:
    return bool(whitelist_col.find_one({"chat_id": chat_id, "file_unique_id": file_unique_id}))

def add_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.update_one({"chat_id": chat_id, "file_unique_id": file_unique_id},
                             {"$set": {"chat_id": chat_id, "file_unique_id": file_unique_id, "ts": int(time.time())}},
                             upsert=True)

def remove_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.delete_one({"chat_id": chat_id, "file_unique_id": file_unique_id})

def is_pack_blacklisted(chat_id: int, set_name: str) -> bool:
    if not set_name:
        return False
    return bool(pack_blacklist_col.find_one({"chat_id": chat_id, "set_name": set_name}))

def add_pack_blacklist(chat_id: int, set_name: str) -> None:
    if not set_name:
        return
    pack_blacklist_col.update_one({"chat_id": chat_id, "set_name": set_name},
                                  {"$set": {"chat_id": chat_id, "set_name": set_name, "ts": int(time.time())}},
                                  upsert=True)

def remove_pack_blacklist(chat_id: int, set_name: str) -> None:
    if not set_name:
        return
    pack_blacklist_col.delete_one({"chat_id": chat_id, "set_name": set_name})

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

# -------------------------
# Pyrogram client
# -------------------------
app = Client("nsfw_cleaner_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# -------------------------
# NudeNet detector (lazy)
# -------------------------
_detector = None
def get_detector():
    global _detector
    if _detector is None:
        if NudeDetector is None:
            raise RuntimeError("NudeDetector not installed. pip install nudenet")
        log.info("Loading NudeNet detector (first run may download model)...")
        _detector = NudeDetector()
    return _detector

# Only labels considered explicit
EXPLICIT_LABELS = {
    "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "GENITALIA_EXPOSED", "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED", "FEMALE_NIPPLE_EXPOSED", "MALE_BREAST_EXPOSED",
    "BREAST_EXPOSED", "NUDE_FEMALE_CHEST", "NUDE_MALE_CHEST", "BUTTOCKS_EXPOSED",
    "SEXUAL_ACTIVITY", "SEXUAL_INTERCOURSE", "MASTURBATION", "ORAL_SEX", "ANAL_SEX",
    "PORNOGRAPHIC", "SEXUALIZED_NUDITY", "EXPLICIT_NUDITY", "ADULT_CONTENT",
}

# -------------------------
# Utility helpers
# -------------------------
async def safe_send_message(client: Client, chat_id: int, text: str, reply_markup=None, thread_id: Optional[int]=None):
    try:
        if thread_id:
            return await client.send_message(chat_id, text, reply_markup=reply_markup, message_thread_id=thread_id)
        else:
            return await client.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        log.warning(f"safe_send_message failed for {chat_id} (thread {thread_id}): {e}")
        return None

async def safe_copy_message(client: Client, to_chat_id: int, from_chat_id: int, message_id: int, thread_id: Optional[int]=None):
    try:
        if thread_id:
            return await client.copy_message(to_chat_id, from_chat_id, message_id, message_thread_id=thread_id)
        else:
            return await client.copy_message(to_chat_id, from_chat_id, message_id)
    except Exception as e:
        log.warning(f"safe_copy_message failed: {e}")
        return None

async def schedule_delete(msg: Message, delay: int):
    try:
        await asyncio.sleep(delay)
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception as e:
        log.debug(f"schedule_delete: {e}")

def extract_video_frames(src_path: str, temp_dir: str, max_frames: int = 3) -> List[str]:
    frames = []
    try:
        out_pattern = os.path.join(temp_dir, "frame_%03d.jpg")
        cmd = ["ffmpeg","-hide_banner","-loglevel","error","-y","-i",src_path,"-vf","fps=1","-vframes",str(max_frames),out_pattern]
        subprocess.run(cmd, check=True)
        for fname in sorted(os.listdir(temp_dir)):
            if fname.lower().endswith(".jpg") or fname.lower().endswith(".jpeg"):
                frames.append(os.path.join(temp_dir, fname))
    except Exception as e:
        log.warning(f"extract_video_frames failed: {e}")
    return frames

def convert_tgs_to_png(tgs_path: str, out_path: str) -> Optional[str]:
    try:
        from lottie import importers, exporters
        with open(tgs_path, "rb") as f:
            animation = importers.tgs.import_tgs(f)
        exporters.export_png(animation, out_path)
        return out_path
    except Exception as e:
        log.warning(f"convert_tgs_to_png failed: {e}")
        return None

def prepare_image_for_detector(src_path: str, out_dir: str) -> Optional[str]:
    if not src_path or not os.path.exists(src_path):
        return None
    try:
        img = Image.open(src_path).convert("RGB")
        out_path = os.path.join(out_dir, "scan_image.jpg")
        img.save(out_path, format="JPEG", quality=85)
        return out_path
    except Exception as e:
        log.warning(f"prepare_image_for_detector failed: {e}")
        return None

def scan_images_for_nsfw(image_paths: List[str]) -> float:
    if not image_paths:
        return 0.0
    detector = None
    try:
        detector = get_detector()
    except Exception as e:
        log.warning(f"Detector not available: {e}")
        return 0.0
    max_score = 0.0
    for path in image_paths:
        if not path or not os.path.exists(path):
            continue
        try:
            detections = detector.detect(path)
            log.info(f"[DETECT] {path} -> {detections}")
            if not detections:
                continue
            for det in detections:
                label = str(det.get("class","")).upper()
                score = float(det.get("score", 0.0))
                if label in EXPLICIT_LABELS:
                    if score > max_score:
                        max_score = score
        except Exception as e:
            log.warning(f"scan failed for {path}: {e}")
            continue
    return max_score

def format_uptime(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)

# -------------------------
# Admin / permission checks
# -------------------------
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
        log.warning(f"is_bot_admin check failed: {e}")
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
        log.warning(f"bot_can_restrict_members failed: {e}")
        return False

# -------------------------
# Logs & moderation buttons
# -------------------------
async def delete_nsfw_message(client: Client, message: Message, score: float):
    chat = message.chat
    user = message.from_user
    thread_id = getattr(message, "message_thread_id", None)

    try:
        await message.delete()
    except Exception as e:
        log.warning(f"delete message failed: {e}")

    # Build inline buttons only if we have a valid user id to act on
    kb_rows = []
    if user and getattr(user, "id", None):
        kb_rows.append([
            InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{chat.id}:{user.id}"),
            InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{chat.id}:{user.id}")
        ])
    kb_rows.append([InlineKeyboardButton("✖️ Close", callback_data="close_log")])
    kb = InlineKeyboardMarkup(kb_rows)

    mention = user.mention if user else "<b>Unknown</b>"
    reason = f"NSFW detection score {score:.2f} >= threshold {NSFW_THRESHOLD}"
    text = (
        "🔍 <b>NSFW content deleted</b>\n\n"
        f"👤 User: {mention}\n"
        f"💬 Chat: <code>{chat.title or chat.id}</code>\n"
        f"📊 Score: <code>{score:.2f}</code>\n"
        f"🆔 Chat ID: <code>{chat.id}</code>\n"
        f"🆔 User ID: <code>{user.id if user else 'N/A'}</code>\n"
        f"📝 Reason: {reason}"
    )

    try:
        sent = await safe_send_message(client, chat.id, text, reply_markup=kb, thread_id=thread_id)
        if sent:
            asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
    except Exception as e:
        log.warning(f"failed to post deletion log: {e}")

    # optional external log
    if LOG_CHAT_ID:
        try:
            await client.send_message(LOG_CHAT_ID, text)
        except Exception:
            pass

async def notify_mute_to_log(client: Client, chat_id: int, user, violations: int, score: float, reason: str, thread_id: Optional[int]=None):
    if not chat_id:
        return
    kb_rows = []
    if user and getattr(user, "id", None):
        kb_rows.append([
            InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{chat_id}:{user.id}"),
            InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{chat_id}:{user.id}")
        ])
    kb_rows.append([InlineKeyboardButton("✖️ Close", callback_data="close_log")])
    kb = InlineKeyboardMarkup(kb_rows)

    mention = user.mention if user else "<b>Unknown</b>"
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
    try:
        sent = await safe_send_message(client, chat_id, text, reply_markup=kb, thread_id=thread_id)
        if sent:
            asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
    except Exception as e:
        log.warning(f"notify_mute_to_log failed: {e}")

# -------------------------
# Violation handling: mute after limit (but DO delete always)
# -------------------------
async def handle_nsfw_sticker_violation(client: Client, message: Message, score: float, reason: str="NSFW content"):
    chat_id = message.chat.id
    user = message.from_user
    thread_id = getattr(message, "message_thread_id", None)
    if not user:
        return

    # Always delete the offending message (already done by caller in many cases)
    # Do not mute admins/owners; still delete
    if await is_user_admin(client, chat_id, user.id):
        log.info(f"[VIOLATION] user {user.id} is admin/owner — deleted but not muted.")
        # send mute log only indicating deleted
        return

    new_count = increment_violation(chat_id, user.id)
    log.info(f"[VIOLATION] user={user.id} chat={chat_id} new_count={new_count}")

    if new_count <= NSFW_STICKER_LIMIT:
        return

    if not await bot_can_restrict_members(client, chat_id):
        log.warning(f"Bot cannot restrict members in chat {chat_id}")
        return

    until_date = datetime.utcnow() + timedelta(seconds=MUTE_DURATION_SECONDS)
    perms = ChatPermissions(
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
        await client.restrict_chat_member(chat_id, user.id, permissions=perms, until_date=until_date)
        log.info(f"Muted user {user.id} in chat {chat_id}")
        await notify_mute_to_log(client, chat_id, user, new_count, score, reason, thread_id=thread_id)
    except Exception as e:
        log.warning(f"Failed to mute user {user.id} in chat {chat_id}: {e}")

# -------------------------
# Commands: start/help/about/status/ping/free/unfree
# -------------------------
def build_main_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add to group", url=f"https://t.me/{bot_username}?startgroup=nsfw_guard")],
        [InlineKeyboardButton("📖 How to use", callback_data="page_how"), InlineKeyboardButton("🛠 Features", callback_data="page_features")],
        [InlineKeyboardButton("🔐 Permissions", callback_data="page_perms"), InlineKeyboardButton("ℹ️ About", callback_data="page_about")],
        [InlineKeyboardButton("📢 Updates & Logs", url=LOG_PUBLIC_URL)],
    ])

def get_main_text() -> str:
    uptime = format_uptime(int(time.time() - START_TIME))
    return (
        "🛡 <b>DLK NSFW Cleaner</b>\n\n"
        "Automatically deletes explicit stickers/photos/videos in groups and topics.\n\n"
        f"• Mutes users after <b>{NSFW_STICKER_LIMIT}</b> NSFW stickers (non-admins)\n\n"
        f"Uptime: <code>{uptime}</code>"
    )

@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    me = await client.get_me()
    bot_username = me.username or "NSFWCleanerBot"
    kb = build_main_keyboard(bot_username)
    await message.reply_photo(START_PHOTO_URL, caption=get_main_text(), reply_markup=kb)

@app.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    await start_cmd(client, message)

@app.on_message(filters.command("about"))
async def about_cmd(client: Client, message: Message):
    await message.reply_text(f"{DEV_ABOUT_TEXT}\nLogs & updates: {LOG_PUBLIC_URL}")

@app.on_message(filters.command("status") & filters.group)
async def status_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    uptime = format_uptime(int(time.time() - START_TIME))
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
        priv = getattr(member, "privileges", None)
        can_delete = bool(getattr(priv, "can_delete_messages", False)) if priv else False
        can_restrict = bool(getattr(priv, "can_restrict_members", False) or getattr(priv, "can_restrict_users", False)) if priv else False
        txt = (
            "🛡 NSFW Guard Status\n\n"
            f"Chat: <code>{message.chat.title or chat_id}</code>\n"
            f"Delete messages: {'✅' if can_delete else '❌'}\n"
            f"Restrict members: {'✅' if can_restrict else '❌'}\n"
            f"Uptime: <code>{uptime}</code>"
        )
    except Exception as e:
        txt = f"Failed to read permissions: {e}"
    await message.reply_text(txt)

@app.on_message(filters.command("ping"))
async def ping_cmd(client: Client, message: Message):
    await message.reply_text(f"Pong! Uptime: {format_uptime(int(time.time() - START_TIME))}")

@app.on_message(filters.command("free") & filters.group)
async def free_cmd(client: Client, message: Message):
    # Admin-only, reply to sticker to whitelist
    chat_id = message.chat.id
    user = message.from_user
    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text("❌ Only group admins can use this command.", quote=True); return
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await message.reply_text("Please reply to a sticker with /free to whitelist it.", quote=True); return
    st = message.reply_to_message.sticker
    add_sticker_whitelist(chat_id, st.file_unique_id)
    await message.reply_text("✅ Sticker whitelisted in this chat.", quote=True)
    if LOG_CHAT_ID:
        try:
            await client.send_message(LOG_CHAT_ID, f"Sticker whitelisted in {chat_id} by {user.mention}\n{st.file_unique_id}")
        except Exception:
            pass

@app.on_message(filters.command("unfree") & filters.group)
async def unfree_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    user = message.from_user
    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text("❌ Only group admins can use this command.", quote=True); return
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await message.reply_text("Please reply to a sticker with /unfree to remove it from whitelist.", quote=True); return
    st = message.reply_to_message.sticker
    remove_sticker_whitelist(chat_id, st.file_unique_id)
    await message.reply_text("✅ Sticker removed from whitelist.", quote=True)

# -------------------------
# Callback handler for inline mod buttons & pages
# -------------------------
@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    data = query.data or ""
    user = query.from_user
    try:
        # simple pages
        if data == "page_main":
            me = await client.get_me()
            await edit_main_message(query.message, get_main_text(), build_main_keyboard(me.username or "NSFWCleanerBot"))
            await query.answer(); return
        if data == "page_how":
            await edit_main_message(query.message, "How to use: add bot, grant admin delete/restrict", build_subpage_keyboard(client.get_me().username if client else "bot"))
            await query.answer(); return
        if data == "close_log":
            try:
                await query.message.delete()
                await query.answer()
            except Exception:
                await query.answer("Unable to remove message.", show_alert=True)
            return

        # mod action: mod_action:unmute|ban:<chat_id>:<user_id>
        m = re.match(r"^mod_action:(unmute|ban):(-?\d+):(\d+)$", data)
        if m:
            action = m.group(1)
            target_chat = int(m.group(2))
            target_user = int(m.group(3))

            # permission check: owner env OR admin in target chat
            caller_id = user.id
            allowed = (caller_id in OWNER_IDS)
            if not allowed:
                try:
                    allowed = await is_user_admin(client, target_chat, caller_id)
                except Exception:
                    allowed = False
            if not allowed:
                await query.answer("You do not have permission to perform this action.", show_alert=True); return

            # ensure bot can restrict
            if not await bot_can_restrict_members(client, target_chat):
                await query.answer("Bot lacks restrict/ban permission in target chat.", show_alert=True); return

            if action == "unmute":
                perms = ChatPermissions(
                    can_send_messages=True, can_send_media_messages=True, can_send_polls=True,
                    can_send_other_messages=True, can_add_web_page_previews=True, can_change_info=False,
                    can_invite_users=True, can_pin_messages=False
                )
                try:
                    await client.restrict_chat_member(target_chat, target_user, permissions=perms)
                    await query.edit_message_text(f"🔈 User <a href='tg://user?id={target_user}'>user</a> unmuted in chat <code>{target_chat}</code>.", parse_mode="html")
                    await query.answer("User unmuted.")
                except Exception as e:
                    # fallback
                    log.warning(f"unmute failed: {e}")
                    try:
                        await client.restrict_chat_member(target_chat, target_user, permissions=perms, until_date=0)
                        await query.edit_message_text(f"🔈 User unmuted (fallback).", parse_mode="html")
                        await query.answer("User unmuted.")
                    except Exception as e2:
                        await query.answer(f"Failed to unmute: {e2}", show_alert=True)
                return

            if action == "ban":
                try:
                    await client.ban_chat_member(target_chat, target_user)
                    await query.edit_message_text(f"⛔ User banned from chat <code>{target_chat}</code>.", parse_mode="html")
                    await query.answer("User banned.")
                except Exception as e:
                    await query.answer(f"Failed to ban: {e}", show_alert=True)
                return

        await query.answer()
    except Exception as e:
        log.warning(f"callback_handler error: {e}")
        try:
            await query.answer("Internal error.", show_alert=True)
        except Exception:
            pass

# -------------------------
# edit helper for pages
# -------------------------
def build_subpage_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="page_main"), InlineKeyboardButton("➕ Add", url=f"https://t.me/{bot_username}?startgroup=nsfw_guard")],
    ])

async def edit_main_message(msg: Message, text: str, keyboard: InlineKeyboardMarkup):
    if msg is None:
        return
    try:
        if getattr(msg, "photo", None):
            await msg.edit_caption(text, reply_markup=keyboard)
        else:
            await msg.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        log.warning(f"edit_main_message failed: {e}")

# -------------------------
# Main media handler (group + topic aware)
# -------------------------
@app.on_message(filters.group & (filters.sticker | filters.photo | filters.video | filters.animation | filters.document))
async def media_guard(client: Client, message: Message):
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    user = message.from_user
    if not user or user.is_bot:
        return

    log.info(f"[MEDIA] chat={chat_id} thread={thread_id} user={user.id if user else 'N/A'} sticker={bool(message.sticker)} photo={bool(message.photo)} video={bool(message.video)} animation={bool(message.animation)}")

    if not await is_bot_admin(client, chat_id):
        log.info(f"Bot not admin or missing delete permission in chat {chat_id}, skipping.")
        return

    # If sticker pack blacklisted -> delete immediately and increment
    if message.sticker:
        st = message.sticker
        set_name = getattr(st, "set_name", None) or ""
        if set_name and is_pack_blacklisted(chat_id, set_name):
            try:
                await message.delete()
            except Exception:
                pass
            await handle_nsfw_sticker_violation(client, message, score=0.0, reason=f"pack-blacklisted:{set_name}")
            # log into thread
            try:
                sent = await safe_send_message(client, chat_id, f"🗑 Deleted sticker from blacklisted pack <code>{set_name}</code> by {user.mention}.", thread_id=thread_id)
                if sent:
                    asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
            except Exception:
                pass
            return
        if is_sticker_whitelisted(chat_id, st.file_unique_id):
            log.debug("Sticker whitelisted, skipping.")
            return

    # download and prepare images for scanning
    tmpdir = tempfile.mkdtemp(prefix="nsfwscan_")
    paths: List[str] = []
    try:
        if message.sticker:
            file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "sticker"))
            # convert tgs if needed
            if file_path and file_path.endswith(".tgs"):
                out_png = os.path.join(tmpdir, "sticker.png")
                converted = convert_tgs_to_png(file_path, out_png)
                if converted:
                    p = prepare_image_for_detector(converted, tmpdir)
                    if p: paths.append(p)
            else:
                p = prepare_image_for_detector(file_path, tmpdir)
                if p: paths.append(p)
        elif message.photo:
            file_path = await client.download_media(message.photo.file_id, file_name=os.path.join(tmpdir, "photo"))
            p = prepare_image_for_detector(file_path, tmpdir)
            if p: paths.append(p)
        elif message.animation or message.video:
            file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "video"))
            frames = extract_video_frames(file_path, tmpdir, max_frames=3)
            for f in frames:
                p = prepare_image_for_detector(f, tmpdir)
                if p: paths.append(p)
        elif message.document:
            file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "doc"))
            p = prepare_image_for_detector(file_path, tmpdir)
            if p: paths.append(p)

        if not paths:
            log.info("No images prepared for scan, skipping.")
            return

        score = scan_images_for_nsfw(paths)
        log.info(f"Scan result chat={chat_id} user={user.id} score={score:.2f}")

        if score >= NSFW_THRESHOLD:
            # if sticker pack present, blacklist pack
            if message.sticker:
                set_name = getattr(message.sticker, "set_name", None) or ""
                if set_name:
                    add_pack_blacklist(chat_id, set_name)
                    try:
                        sent = await safe_send_message(client, chat_id, f"🚫 Automatically blacklisted sticker pack <code>{set_name}</code> in chat <code>{chat_id}</code> (explicit sticker detected).", thread_id=thread_id)
                        if sent: asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
                    except Exception:
                        pass
            # delete and handle violations
            await delete_nsfw_message(client, message, score)
            await handle_nsfw_sticker_violation(client, message, score, reason=f"NSFW score {score:.2f}")
        else:
            log.debug("Content below threshold, OK.")
    except Exception as e:
        log.error(f"media_guard error: {e}")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

# -------------------------
# Start
# -------------------------
if __name__ == "__main__":
    log.info("Starting NSFW Cleaner bot...")
    app.run()
