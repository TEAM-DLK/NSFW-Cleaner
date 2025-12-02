# dlk_nsfw_guard_topics_logs.py
# Full updated NSFW Guard with:
# - Topic/forum support (works in Telegram topic groups)
# - Per-group log channel (set/unset)
# - Robust message attribute fallbacks (avoid 'Message' object has no attribute 'message_id')
# - Fast-path pack blacklist deletes + improved error handling
# - Inline Unmute / Ban buttons in logs, permission-checked
#
# Requirements: pyrogram, nudenet, pymongo, python-dotenv, ffmpeg, lottie (optional for tgs)
# Set environment variables: API_ID, API_HASH, BOT_TOKEN, MONGO_URI, LOG_CHAT_ID (optional)
# Add OWNER_IDS (comma separated) if you want extra privileged users for mod actions.

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
from nudenet import NudeDetector

# -------------------------------------------------
# Load environment
# -------------------------------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.75"))

NSFW_STICKER_LIMIT = int(os.getenv("NSFW_STICKER_LIMIT", "1"))
PACK_STICKER_LIMIT = int(os.getenv("PACK_STICKER_LIMIT", str(NSFW_STICKER_LIMIT)))

MUTE_DURATION_SECONDS = int(os.getenv("MUTE_DURATION_SECONDS", "86400"))
CONFIRM_MSG_DELETE_SECONDS = int(os.getenv("CONFIRM_MSG_DELETE_SECONDS", "10"))

MONGO_URI = os.getenv("MONGO_URI", "").strip()
if not MONGO_URI:
    raise SystemExit("MONGO_URI is not set in environment. Set it in your .env file.")

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

# -------------------------------------------------
# Logging
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
)
log = logging.getLogger("NSFW-GUARD")

# -------------------------------------------------
# MongoDB collections
# -------------------------------------------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["nsfw_guard"]

# sticker whitelist per-chat
whitelist_col = db["sticker_whitelist"]
whitelist_col.create_index([("chat_id", ASCENDING), ("file_unique_id", ASCENDING)], unique=True)

# sticker pack blacklist per-chat
pack_blacklist_col = db["sticker_pack_blacklist"]
pack_blacklist_col.create_index([("chat_id", ASCENDING), ("set_name", ASCENDING)], unique=True)

# violations per-chat per-user
violations_col = db["nsfw_violations"]
violations_col.create_index([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)

# pending actions
pending_col = db["pending_actions"]
try:
    pending_col.create_index("ts", expireAfterSeconds=3600)
except Exception:
    pass

# per-group log target (if admin wants logs to go to a channel dedicated to that group)
chat_log_map_col = db["chat_log_map"]
chat_log_map_col.create_index([("chat_id", ASCENDING)], unique=True)

def get_log_for_chat(chat_id: int) -> Optional[str]:
    """Return a chat-specific log chat id/username, or global LOG_CHAT_ID, or None."""
    doc = chat_log_map_col.find_one({"chat_id": chat_id})
    if doc and doc.get("log_target"):
        return doc["log_target"]
    return LOG_CHAT_ID

def set_log_for_chat(chat_id: int, log_target: str):
    chat_log_map_col.update_one({"chat_id": chat_id}, {"$set": {"log_target": log_target, "ts": int(time.time())}}, upsert=True)

def unset_log_for_chat(chat_id: int):
    chat_log_map_col.delete_one({"chat_id": chat_id})

# helpers for whitelist/blacklist/violations/pending (same semantics as before)
def is_sticker_whitelisted(chat_id: int, file_unique_id: str) -> bool:
    doc = whitelist_col.find_one({"chat_id": chat_id, "file_unique_id": file_unique_id})
    return doc is not None

def add_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.update_one({"chat_id": chat_id, "file_unique_id": file_unique_id},
                             {"$set": {"chat_id": chat_id, "file_unique_id": file_unique_id, "ts": int(time.time())}},
                             upsert=True)

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

def create_pending_action(action_type: str, chat_id: int, admin_user_id: int, file_unique_id: str = "", set_name: str = "") -> str:
    doc = {"action": action_type, "chat_id": chat_id, "admin_user_id": admin_user_id,
           "file_unique_id": file_unique_id or "", "set_name": set_name or "", "stickers": [], "set_names": [], "state": "open", "ts": int(time.time())}
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
# NudeNet detector
# -------------------------------------------------
log.info("Loading NudeNet detector (first run may download the model)...")
detector = NudeDetector()

EXPLICIT_LABELS = {
    "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "GENITALIA_EXPOSED",
    "ANUS_EXPOSED", "FEMALE_BREAST_EXPOSED", "FEMALE_NIPPLE_EXPOSED",
    "BREAST_EXPOSED", "NUDE_FEMALE_CHEST", "NUDE_MALE_CHEST",
    "BUTTOCKS_EXPOSED", "SEXUAL_ACTIVITY", "SEX_ACT", "SEXUAL_INTERCOURSE",
    "MASTURBATION", "ORAL_SEX", "ANAL_SEX", "PORNOGRAPHIC", "SEXUALIZED_NUDITY",
    "EXPLICIT_NUDITY", "ADULT_CONTENT", "HARDCORE", "SOFTCORE", "LEWD_CONTENT",
    "OBSCENE_CONTENT", "INAPPROPRIATE_CONTENT", "MINOR_NUDITY", "CHILD_NUDITY",
    "CSAM_SUSPECT", "ADULT_TOY", "SEX_TOY", "FETISH_CONTENT",
}

# -------------------------------------------------
# Utilities & helpers (robust)
# -------------------------------------------------
async def is_bot_admin(client: Client, chat_id: int) -> bool:
    try:
        me = await client.get_me()
        member = await client.get_chat_member(chat_id, me.id)
        if member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            return False
        privileges = getattr(member, "privileges", None)
        if privileges is not None:
            can_delete = bool(getattr(privileges, "can_delete_messages", False))
            return can_delete
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
            can_restrict = bool(getattr(privileges, "can_restrict_members", False) or getattr(privileges, "can_restrict_users", False))
            return can_restrict
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
            if fname.lower().endswith((".jpg", ".jpeg", ".png")):
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

def scan_images_for_nsfw(image_paths: List[str]) -> float:
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
                if label in EXPLICIT_LABELS and score > max_score:
                    max_score = score
        except Exception as e:
            log.warning(f"Scanning failed for {path}: {e}")
            continue
    return max_score

async def delete_nsfw_message(client: Client, message: Message, score: float):
    # robust delete: message may be partially missing; attempt best-effort
    chat = getattr(message, "chat", None)
    user = getattr(message, "from_user", None)
    target_chat_id = getattr(chat, "id", None) if chat else None
    mention = None
    try:
        if getattr(message, "message_id", None) is not None and target_chat_id:
            await client.delete_messages(target_chat_id, message.message_id)
        else:
            # fallback: try message.delete()
            try:
                await message.delete()
            except Exception:
                pass
    except Exception as e:
        log.warning(f"Failed to delete NSFW message (best-effort): {e}")

    # Send log to per-chat log target (falls back to global LOG_CHAT_ID)
    target_log = get_log_for_chat(target_chat_id) if target_chat_id is not None else LOG_CHAT_ID
    if target_log:
        try:
            reason = f"NSFW detection score {score:.2f} >= threshold {NSFW_THRESHOLD}"
            mention = user.mention if user else f"<code>{getattr(message, 'from_user', {}).get('id', 'N/A')}</code>"
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{target_chat_id}:{user.id if user else 0}"),
                        InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{target_chat_id}:{user.id if user else 0}")
                    ],
                    [InlineKeyboardButton("📢 Group / Logs", url=LOG_PUBLIC_URL)]
                ]
            )
            text = (
                "🔍 <b>NSFW content deleted</b>\n\n"
                f"👤 User: {mention}\n"
                f"💬 Chat: <code>{chat.title or chat.id if chat else target_chat_id}</code>\n"
                f"📊 Score: <code>{score:.2f}</code>\n"
                f"🆔 Chat ID: <code>{target_chat_id}</code>\n"
                f"🆔 User ID: <code>{user.id if user else 'N/A'}</code>\n"
                f"📝 Reason: {reason}"
            )
            await client.send_message(target_log, text, reply_markup=kb)
        except Exception as e:
            log.warning(f"Failed to send log message: {e}")

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
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Add me to your group", url=f"https://t.me/{bot_username}?startgroup=nsfw_guard"),
            ],
            [InlineKeyboardButton("📖 How to use", callback_data="page_how"),
             InlineKeyboardButton("🛠 Features", callback_data="page_features")],
            [InlineKeyboardButton("🔐 Permissions", callback_data="page_perms"),
             InlineKeyboardButton("ℹ️ About", callback_data="page_about")],
            [InlineKeyboardButton("📢 Updates & Logs", url=LOG_PUBLIC_URL)],
        ]
    )

def get_main_text() -> str:
    uptime = format_uptime(int(time.time() - START_TIME))
    return (
        "🛡 <b>DLK NSFW Cleaner (Topics & Per-Group Logs)</b>\n\n"
        "Protect your Telegram groups (including topics/forums) from explicit content.\n\n"
        f"• Auto-scan stickers, photos, GIFs & videos\n"
        f"• Fast-delete for blacklisted sticker packs\n"
        f"• Mutes after <b>{NSFW_STICKER_LIMIT}</b> NSFW stickers (non-admins only)\n\n"
        f"Uptime: <code>{uptime}</code>\n\n"
        "Use /setlog in the group to send logs to a dedicated channel, or leave unset to use global logs."
    )

# -------------------------
# Commands: start/help/about/status/ping + setlog/unsetlog
# -------------------------
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    me = await client.get_me()
    bot_username = me.username or "NSFWGuardBot"
    keyboard = build_main_keyboard(bot_username)
    await message.reply_photo(START_PHOTO_URL, caption=get_main_text(), reply_markup=keyboard)

@app.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    await start_cmd(client, message)

@app.on_message(filters.command("about"))
async def about_cmd(client: Client, message: Message):
    await message.reply_text("ℹ️ " + DEV_ABOUT_TEXT)

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

# Admin commands to set per-group log target
@app.on_message(filters.command("setlog") & filters.group)
async def setlog_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    user = message.from_user
    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text("❌ Only group admins can use this command.", quote=True)
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: /setlog <@channelusername or channel_id>\nExample: /setlog @mygroup_logs", quote=True)
        return
    target = parts[1].strip()
    try:
        set_log_for_chat(chat_id, target)
        await message.reply_text(f"✅ Logs for this group will be sent to: <code>{target}</code>")
    except Exception as e:
        await message.reply_text(f"Failed to set log target: {e}", quote=True)

@app.on_message(filters.command("unsetlog") & filters.group)
async def unsetlog_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    user = message.from_user
    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text("❌ Only group admins can use this command.", quote=True)
        return
    unset_log_for_chat(chat_id)
    await message.reply_text("✅ Per-group log target removed. Using global logs if configured.")

# -------------------------
# /free, /unfree, /blockpack flows (unchanged behavior but robust message id handling)
# -------------------------
@app.on_message(filters.command("free") & filters.group)
async def free_cmd(client: Client, message: Message):
    chat_id = message.chat.id
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
        confirm_kb_group = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Confirm in private", url=deep_link),
              InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel_group:{pending_id}")]]
        )
        try:
            grp_msg = await message.reply_text(
                "✅ Whitelist request created. Admin: please open my private chat and confirm.\nClick the button to open my private chat and confirm the whitelist.",
                reply_markup=confirm_kb_group,
                quote=True,
            )
            asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
        except Exception as e:
            log.warning(f"Failed to send group confirm button: {e}")

        try:
            pm_sticker_msg = await client.copy_message(user.id, message.chat.id, sticker_msg.message_id if getattr(sticker_msg, "message_id", None) else sticker_msg.message_id)
            confirm_kb_pm = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"),
                  InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")]]
            )
            pm_text = await client.send_message(
                user.id,
                f"You're about to whitelist a sticker in chat <code>{chat_id}</code> ({message.chat.title or 'group'}).\n\nPress Confirm to whitelist the sticker in that group (this is per-chat).",
                reply_markup=confirm_kb_pm,
            )
            asyncio.create_task(schedule_delete(pm_sticker_msg, CONFIRM_MSG_DELETE_SECONDS))
            asyncio.create_task(schedule_delete(pm_text, CONFIRM_MSG_DELETE_SECONDS))
        except Exception as e:
            log.info(f"Could not send PM to admin {user.id}: {e}")
    else:
        pending_id = create_pending_action("whitelist", chat_id, user.id, "", "")
        deep_link = f"https://t.me/{bot_username}?start=free_{pending_id}"
        confirm_kb_group = InlineKeyboardMarkup(
            [[InlineKeyboardButton("➡️ Open bot private (send sticker there)", url=deep_link),
              InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel_group:{pending_id}")]]
        )
        try:
            grp_msg = await message.reply_text(
                "✅ Whitelist request created. Open my private chat and send the sticker you want to whitelist, then press Confirm.",
                reply_markup=confirm_kb_group,
                quote=True,
            )
            asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
        except Exception as e:
            log.warning(f"Failed to send group helper for no-reply /free: {e}")

    # log to group log target/global log
    target_log = get_log_for_chat(chat_id)
    if target_log:
        try:
            await client.send_message(target_log, f"🛡 Whitelist requested by admin {user.mention} in chat <code>{chat_id}</code>. Pending ID: <code>{pending_id}</code>")
        except Exception:
            pass

@app.on_message(filters.command("unfree") & filters.group)
async def unfree_cmd(client: Client, message: Message):
    chat_id = message.chat.id
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
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Remove pack-blacklist (confirm in PM)", url=deep_link),
              InlineKeyboardButton("❌ Cancel", callback_data=f"unblack_cancel_group:{pending_id}")]]
        )
        try:
            grp = await message.reply_text(
                f"⚠️ Pack <code>{set_name}</code> is currently blacklisted in this chat. If you want to remove the pack-level blacklist so stickers from this pack stop being auto-deleted, confirm in private.",
                reply_markup=kb,
                quote=True,
            )
            asyncio.create_task(schedule_delete(grp, CONFIRM_MSG_DELETE_SECONDS))
        except Exception:
            pass
    target_log = get_log_for_chat(chat_id)
    if target_log:
        try:
            await client.send_message(target_log, f"❌ Unwhitelist requested by admin {user.mention} in chat <code>{chat_id}</code>.")
        except Exception:
            pass

@app.on_message(filters.command("blockpack") & filters.group)
async def blockpack_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    user = message.from_user
    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text("❌ Only group admins can use this command.", quote=True)
        return
    me = await client.get_me()
    bot_username = me.username or "NSFWGuardBot"
    pending_id = create_pending_action("bulk_block", chat_id, user.id, "", "")
    deep_link = f"https://t.me/{bot_username}?start=block_{pending_id}"
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("➡️ Open private and send stickers", url=deep_link),
          InlineKeyboardButton("❌ Cancel", callback_data=f"block_cancel_group:{pending_id}")]]
    )
    try:
        grp_msg = await message.reply_text(
            "📦 Bulk block: Click the button to open my private chat. Then send up to 50 stickers here (can be from multiple packs). When finished, send DONE to finalize and blacklist the collected packs for this chat.",
            reply_markup=kb,
            quote=True,
        )
        asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
    except Exception as e:
        log.warning(f"Failed to send group helper for /blockpack: {e}")
    target_log = get_log_for_chat(chat_id)
    if target_log:
        try:
            await client.send_message(target_log, f"Bulk block requested by admin {user.mention} in chat <code>{chat_id}</code>. Pending ID: <code>{pending_id}</code>")
        except Exception:
            pass

# -------------------------
# Private handlers for collecting stickers and confirming actions (unchanged)
# -------------------------
@app.on_message(filters.private & filters.sticker)
async def private_sticker_collector(client: Client, message: Message):
    user = message.from_user
    pending = get_latest_pending_for_admin(user.id, "bulk_block")
    if not pending:
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
            confirm_kb_pm = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"),
                  InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")]]
            )
            try:
                copied = await client.copy_message(user.id, message.chat.id, message.message_id)
                pm_text = await client.send_message(user.id, f"Sticker received for chat <code>{pending['chat_id']}</code>. Press Confirm to whitelist.", reply_markup=confirm_kb_pm)
                asyncio.create_task(schedule_delete(copied, CONFIRM_MSG_DELETE_SECONDS))
                asyncio.create_task(schedule_delete(pm_text, CONFIRM_MSG_DELETE_SECONDS))
            except Exception:
                await message.reply_text("Sticker received. Press Confirm to whitelist.", reply_markup=confirm_kb_pm)
            return
        return

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
    target_log = get_log_for_chat(chat_id)
    if target_log:
        try:
            await client.send_message(target_log, f"📦 Admin {user.mention} blocked sticker packs {', '.join(set_names)} for chat <code>{chat_id}</code>.")
        except Exception:
            pass

# -------------------------
# Main group media handler (robust + fast paths)
# -------------------------
@app.on_message(filters.group & (filters.sticker | filters.photo | filters.video | filters.animation | filters.document))
async def group_media_handler(client: Client, message: Message):
    chat = getattr(message, "chat", None)
    chat_id = chat.id if chat else None
    user = getattr(message, "from_user", None)
    if not user or chat_id is None:
        return

    # If admin: do not auto-mute, but still delete explicit if found (admins exempt from mute)
    try:
        # FAST-PATH: stickers from blacklisted pack -> immediate delete (no scanning)
        if message.sticker:
            set_name = getattr(message.sticker, "set_name", None) or ""
            file_unique_id = message.sticker.file_unique_id
            if is_sticker_whitelisted(chat_id, file_unique_id):
                # whitelisted in this chat -> skip
                return
            if set_name and is_pack_blacklisted(chat_id, set_name):
                try:
                    # attempt deletion; permission might be missing, best-effort
                    try:
                        await client.delete_messages(chat_id, message.message_id)
                    except Exception:
                        try:
                            await message.delete()
                        except Exception:
                            pass
                except Exception:
                    pass
                new_count = increment_violation(chat_id, user.id)
                log.info(f"[PACK DELETE] Deleted sticker from blacklisted pack {set_name} in chat {chat_id}. user={user.id} count={new_count}")
                if new_count > PACK_STICKER_LIMIT and not await is_user_admin(client, chat_id, user.id) and await bot_can_restrict_members(client, chat_id):
                    reason = f"Sent stickers from blacklisted pack {set_name}"
                    await handle_nsfw_sticker_violation(client, message, score=0.0, reason=reason)
                target_log = get_log_for_chat(chat_id)
                if target_log:
                    try:
                        await client.send_message(target_log, f"🗑 Deleted sticker from blacklisted pack <code>{set_name}</code> by {user.mention} in chat <code>{chat_id}</code>.")
                    except Exception:
                        pass
                return

        # Otherwise: download necessary content into a temp dir and scan
        tmpdir = tempfile.mkdtemp(prefix="nsfwscan_")
        paths = []
        try:
            # sticker handling: don't re-download if sticker already local; still convert if tgs
            if message.sticker:
                file_path = None
                try:
                    file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "sticker"))
                except Exception as e:
                    log.warning(f"Failed to download sticker: {e}")
                if file_path and file_path.endswith(".tgs"):
                    out_png = os.path.join(tmpdir, "sticker.png")
                    converted = convert_tgs_to_png(file_path, out_png)
                    if converted:
                        paths.append(converted)
                else:
                    if file_path:
                        paths.append(file_path)
            elif message.photo:
                try:
                    file_path = await client.download_media(message.photo.file_id, file_name=os.path.join(tmpdir, "photo.jpg"))
                    if file_path:
                        paths.append(file_path)
                except Exception as e:
                    log.warning(f"Photo download failed: {e}")
            elif message.animation:
                try:
                    file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "anim"))
                    frames = extract_video_frames(file_path, tmpdir, max_frames=3)
                    paths.extend(frames)
                except Exception as e:
                    log.warning(f"Anim download/extract failed: {e}")
            elif message.video or (message.document and (getattr(message.document, "mime_type", "") or "").startswith("video/")):
                try:
                    file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "video"))
                    frames = extract_video_frames(file_path, tmpdir, max_frames=3)
                    paths.extend(frames)
                except Exception as e:
                    log.warning(f"Video download/extract failed: {e}")
            elif message.document:
                try:
                    file_path = await client.download_media(message, file_name=os.path.join(tmpdir, "doc"))
                    if file_path:
                        paths.append(file_path)
                except Exception as e:
                    log.warning(f"Document download failed: {e}")

            # Run scan (detector may raise; handle gracefully)
            score = 0.0
            try:
                score = scan_images_for_nsfw(paths)
            except Exception as e:
                log.warning(f"scan_images_for_nsfw failed: {e}")
                score = 0.0

            if score >= NSFW_THRESHOLD:
                # If sticker & part of pack => blacklist pack
                if message.sticker:
                    set_name = getattr(message.sticker, "set_name", None) or ""
                    if set_name:
                        add_pack_blacklist(chat_id, set_name)
                        target_log = get_log_for_chat(chat_id)
                        if target_log:
                            try:
                                await client.send_message(target_log, f"🚫 Automatically blacklisted sticker pack <code>{set_name}</code> in chat <code>{chat_id}</code> (explicit sticker detected).")
                            except Exception:
                                pass
                await delete_nsfw_message(client, message, score)
                await handle_nsfw_sticker_violation(client, message, score, reason=f"NSFW detection score {score:.2f}")
            else:
                log.debug(f"[SCAN] No explicit NSFW found (score {score:.2f}) for message id {getattr(message, 'message_id', 'N/A')} in chat {chat_id}")
        finally:
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"Error in group_media_handler: {e}")

# -------------------------------------------------
# Violation handling (mute after limit) - improved & robust
# -------------------------------------------------
async def notify_mute_to_log(client: Client, chat_id: int, user, violations: int, score: float, reason: str):
    if chat_id is None:
        return
    target_log = get_log_for_chat(chat_id)
    if not target_log:
        return
    try:
        mention = user.mention if user else f"<code>{getattr(user, 'id', 'N/A')}</code>"
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{chat_id}:{user.id}"),
                    InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{chat_id}:{user.id}"),
                ],
                [InlineKeyboardButton("📢 Group / Logs", url=LOG_PUBLIC_URL)],
            ]
        )
        text = (
            "🚫 <b>User muted for NSFW stickers</b>\n\n"
            f"👥 Chat: <code>{chat_id}</code>\n"
            f"👤 User: {mention}\n"
            f"🆔 User ID: <code>{user.id}</code>\n"
            f"📊 Last Score: <code>{score:.2f}</code>\n"
            f"🔢 Violations: <code>{violations}</code>\n"
            f"⏱ Duration: <code>{MUTE_DURATION_SECONDS}s</code>\n"
            f"📝 Reason: {reason}"
        )
        await client.send_message(target_log, text, reply_markup=kb)
    except Exception as e:
        log.warning(f"Failed to send mute log message: {e}")

async def handle_nsfw_sticker_violation(client: Client, message: Message, score: float, reason: str = "NSFW content"):
    chat_id = getattr(message.chat, "id", None)
    user = getattr(message, "from_user", None)
    if user is None or chat_id is None:
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
            # reply to the message if present (non-blocking)
            try:
                await message.reply_text(f"🚫 {user.mention} has been muted for repeated NSFW content (>{NSFW_STICKER_LIMIT}).", reply_markup=kb)
            except Exception:
                pass
        except Exception:
            pass
        await notify_mute_to_log(client, chat_id, user, new_count, score, reason)
    except Exception as e:
        log.warning(f"[VIOLATION] Failed to mute user={user.id} in chat={chat_id}: {e}")

# -------------------------------------------------
# Callback query handlers (confirm flows + mod actions)
# -------------------------------------------------
@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    data = query.data or ""
    user = query.from_user

    # page nav (start)
    if data == "page_main":
        me = await client.get_me()
        bot_username = me.username or "NSFWGuardBot"
        await edit_main_message(query.message, get_main_text(), build_main_keyboard(bot_username))
        await query.answer()
        return
    if data == "page_how":
        await query.answer()
        return
    if data == "page_features":
        await query.answer()
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
        if not file_unique_id:
            await query.answer("No sticker was provided to whitelist.", show_alert=True)
            return
        add_sticker_whitelist(chat_id, file_unique_id)
        finalize_pending_action(pending_id)
        try:
            await query.edit_message_text("✅ Sticker has been whitelisted for that chat.")
        except Exception:
            pass
        await query.answer("Sticker whitelisted.")
        target_log = get_log_for_chat(chat_id)
        if target_log:
            try:
                await client.send_message(target_log, f"✅ Sticker whitelisted in chat <code>{chat_id}</code> by admin {user.mention}.")
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

    # Unblacklist confirm
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
        target_log = get_log_for_chat(chat_id)
        if target_log:
            try:
                await client.send_message(target_log, f"✅ Pack <code>{set_name}</code> un-blacklisted for chat <code>{chat_id}</code> by {user.mention}.")
            except Exception:
                pass
        return

    # Bulk block / cancel handlers (group and pm variants)
    for prefix in ["block_cancel", "free_cancel_group", "unblack_cancel_group", "block_cancel_group"]:
        m = re.match(rf"^{prefix}:([0-9a-fA-F]+)$", data)
        if m:
            pending_id = m.group(1)
            cancel_pending_action(pending_id)
            try:
                await query.edit_message_text("Cancelled.")
            except Exception:
                pass
            await query.answer("Cancelled.")
            return

    # Mod actions from log messages: mod_action:<action>:<chat_id>:<user_id>
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
                await client.restrict_chat_member(target_chat, target_user, permissions=permissions, until_date=0)
                try:
                    await query.edit_message_text(f"🔈 User <a href='tg://user?id={target_user}'>user</a> has been unmuted in chat <code>{target_chat}</code>.")
                except Exception:
                    pass
                await query.answer("User unmuted.")
            except Exception as e:
                await query.answer(f"Failed to unmute: {e}", show_alert=True)
        elif action == "ban":
            try:
                await client.ban_chat_member(target_chat, target_user)
                try:
                    await query.edit_message_text(f"⛔ User <a href='tg://user?id={target_user}'>user</a> has been banned from chat <code>{target_chat}</code>.")
                except Exception:
                    pass
                await query.answer("User banned.")
            except Exception as e:
                await query.answer(f"Failed to ban: {e}", show_alert=True)
        return

    await query.answer()

# -------------------------------------------------
# Start the bot
# -------------------------------------------------
if __name__ == "__main__":
    log.info("Starting NSFW Guard bot (topics + per-group logs) ...")
    app.run()
