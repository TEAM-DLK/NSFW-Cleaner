import os
import time
import logging
import tempfile
import shutil
import subprocess

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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

MONGO_URI = os.getenv("MONGO_URI", "").strip()
if not MONGO_URI:
    raise SystemExit("MONGO_URI is not set in environment. Set it in your .env file.")

LOG_CHAT_ID_ENV = os.getenv("LOG_CHAT_ID", "").strip()
# Can be numeric (-100...) or @username
LOG_CHAT_ID = LOG_CHAT_ID_ENV if LOG_CHAT_ID_ENV else None

START_TIME = time.time()

# Your branding
OFFICIAL_CHANNEL = "https://t.me/DLKDevelopers"
DEV_ABOUT_TEXT = (
    "DLK DEVELOPER\n"
    "Telegram Bot Developer & Music Producer\n"
    "Channel: @DLKDevelopers"
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
# MongoDB (sticker whitelist)
# -------------------------------------------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["nsfw_guard"]
whitelist_col = db["sticker_whitelist"]

# Unique index for (chat_id, file_unique_id)
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
        {"$set": {"chat_id": chat_id, "file_unique_id": file_unique_id}},
        upsert=True,
    )


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
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    # If you also want to block male chest, uncomment:
    # "MALE_BREAST_EXPOSED",
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

    IMPORTANT:
    - Only detections whose 'class' is in EXPLICIT_LABELS
      are considered NSFW.
    - FACE, BELLY, FEET, ARMPITS, COVERED parts, etc. are ignored.
    """
    if not image_paths:
        return 0.0

    max_score = 0.0

    for path in image_paths:
        try:
            detections = detector.detect(path)  # [{class, score, box}, ...]
            log.info(f"[DETECT] {path} -> {detections}")

            for det in detections:
                label = str(det.get("class", "")).upper()
                score = float(det.get("score", 0.0))

                # Only consider truly explicit labels
                if label in EXPLICIT_LABELS:
                    log.info(f"[DETECT] EXPLICIT HIT label={label}, score={score:.2f}")
                    if score > max_score:
                        max_score = score
                else:
                    # For debugging: see what we are ignoring
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

    # Optional logging to a log chat/channel
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
                disable_web_page_preview=True,
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
                InlineKeyboardButton("📖 Help", callback_data="help_main"),
                InlineKeyboardButton("ℹ️ About", callback_data="about_main"),
            ],
            [
                InlineKeyboardButton("📢 Official Channel", url=OFFICIAL_CHANNEL),
            ],
        ]
    )


def get_help_text() -> str:
    uptime = format_uptime(int(time.time() - START_TIME))
    return (
        "📖 <b>NSFW Guard Help</b>\n\n"
        "<b>Basic usage:</b>\n"
        "• Add me to a group and make me admin.\n"
        "• I will silently delete ONLY explicit NSFW stickers/photos/gifs/videos.\n"
        "• Admins can reply to a sticker with <code>/free</code> to whitelist it.\n\n"
        f"<b>Uptime:</b> <code>{uptime}</code>"
    )


# -------------------------------------------------
# Commands
# -------------------------------------------------
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    me = await client.get_me()
    bot_username = me.username or "NSFWGuardBot"

    if message.chat.type == "private":
        keyboard = build_main_keyboard(bot_username)

        text = (
            "👋 <b>Hi!</b>\n\n"
            "I am an <b>NSFW Guard Bot</b>.\n\n"
            "• Automatically scans stickers, gifs, photos and videos\n"
            "• Deletes nude / explicit sexual content silently\n"
            "• Admins can use <code>/free</code> to allow safe stickers\n\n"
            "Add me to your group, give me admin with <b>Delete messages</b>, "
            "and I will keep it clean."
        )

        await message.reply_text(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    else:
        text = (
            "✅ NSFW Guard Bot is active in this chat.\n\n"
            "I silently delete explicit NSFW stickers/photos/videos.\n"
            "Admins can use <code>/free</code> as a reply to a sticker to allow it.\n\n"
            "Use <code>/help</code> for more info."
        )
        await message.reply_text(text, disable_web_page_preview=True)


@app.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📌 Features", callback_data="help_features"),
                InlineKeyboardButton("🔐 Permissions", callback_data="help_perms"),
            ],
            [
                InlineKeyboardButton("📢 Official Channel", url=OFFICIAL_CHANNEL),
            ],
        ]
    )

    await message.reply_text(
        get_help_text(),
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("about"))
async def about_cmd(client: Client, message: Message):
    text = (
        "ℹ️ <b>About this bot</b>\n\n"
        "This is an automated NSFW filter bot.\n"
        "It uses an AI detector to identify nude/sexual explicit content and deletes it "
        "to keep your groups clean and safe.\n\n"
        f"<b>Developer:</b>\n<code>{DEV_ABOUT_TEXT}</code>"
    )
    await message.reply_text(text, disable_web_page_preview=True)


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

        text = (
            "🛡 <b>NSFW Guard Status</b>\n\n"
            f"👥 Chat: <code>{message.chat.title or chat_id}</code>\n"
            f"🤖 Status: <code>{status}</code>\n"
            f"🗑 Delete messages: <b>{'✅ enabled' if can_delete else '❌ missing'}</b>\n\n"
            f"⏱ Uptime: <code>{uptime}</code>\n"
        )
    except Exception as e:
        text = f"⚠️ Failed to read permissions: <code>{e}</code>"

    await message.reply_text(text, disable_web_page_preview=True)


@app.on_message(filters.command("ping"))
async def ping_cmd(client: Client, message: Message):
    uptime = format_uptime(int(time.time() - START_TIME))
    await message.reply_text(
        f"🏓 <b>Pong!</b>\nUptime: <code>{uptime}</code>",
        disable_web_page_preview=True,
    )


@app.on_message(filters.command("free") & filters.group)
async def free_cmd(client: Client, message: Message):
    """
    /free
    Admin-only.
    Must be used as reply to a sticker message.
    Whitelists that sticker in this chat.
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

    add_sticker_whitelist(chat_id, file_unique_id)

    confirm_text = (
        "✅ This sticker has been whitelisted in this chat.\n"
        "It will no longer be deleted by the NSFW filter."
    )
    await message.reply_text(confirm_text, quote=True)

    # Optional log
    if LOG_CHAT_ID:
        try:
            await client.send_message(
                LOG_CHAT_ID,
                f"✅ Sticker whitelisted by admin {user.mention} in chat <code>{chat_id}</code>.\n"
                f"<code>{file_unique_id}</code>",
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning(f"Failed to send whitelist log message: {e}")


# -------------------------------------------------
# Callback queries (inline buttons)
# -------------------------------------------------
@app.on_callback_query()
async def callback_handler(client, callback_query):
    try:
        data = callback_query.data
        msg = callback_query.message

        if data == "help_main":
            await callback_query.answer()
            await msg.edit_text(
                get_help_text(),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("📌 Features", callback_data="help_features"),
                            InlineKeyboardButton("🔐 Permissions", callback_data="help_perms"),
                        ],
                        [
                            InlineKeyboardButton("📢 Official Channel", url=OFFICIAL_CHANNEL),
                        ],
                    ]
                ),
                disable_web_page_preview=True,
            )

        elif data == "about_main":
            await callback_query.answer()
            text = (
                "ℹ️ <b>About this bot</b>\n\n"
                "This is an automated NSFW filter bot.\n"
                "It uses an AI detector to identify nude/sexual explicit content and deletes it "
                "to keep your groups clean and safe.\n\n"
                f"<b>Developer:</b>\n<code>{DEV_ABOUT_TEXT}</code>"
            )
            await msg.edit_text(text, disable_web_page_preview=True)

        elif data == "help_features":
            await callback_query.answer()
            text = (
                "📌 <b>Features</b>\n\n"
                "• Detects explicit NSFW (nude / sexual) content using AI\n"
                "• Works on stickers, photos, gifs and videos\n"
                "• Silently deletes explicit NSFW content\n"
                "• Admins can whitelist safe stickers with /free\n"
            )
            await msg.edit_text(text, disable_web_page_preview=True)

        elif data == "help_perms":
            await callback_query.answer()
            text = (
                "🔐 <b>Required Permissions</b>\n\n"
                "To work correctly in a group, the bot needs:\n"
                "• Be <b>Admin</b>\n"
                "• <b>Delete messages</b> permission\n\n"
                "If these are missing, the bot cannot remove NSFW content."
            )
            await msg.edit_text(text, disable_web_page_preview=True)

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
        | filters.animation  # GIFs, etc.
    )
)
async def media_guard(client: Client, message: Message):
    chat_id = message.chat.id

    log.info(
        f"[MEDIA] New media in chat={chat_id}, user={getattr(message.from_user, 'id', 'N/A')}, "
        f"types: sticker={bool(message.sticker)}, photo={bool(message.photo)}, "
        f"video={bool(message.video)}, animation={bool(message.animation)}"
    )

    # Check bot admin permissions (delete messages)
    if not await is_bot_admin(client, chat_id):
        log.info(f"[MEDIA] Bot is not admin or cannot delete in chat={chat_id}, skipping.")
        return

    # Skip bots and anonymous
    if not message.from_user or message.from_user.is_bot:
        return

    user = message.from_user

    temp_root = tempfile.mkdtemp(prefix="nsfw_guard_")
    image_paths: list[str] = []

    try:
        # -------------------------------------------------
        # Stickers
        # -------------------------------------------------
        if message.sticker:
            st = message.sticker

            # Check whitelist for this sticker in this chat
            if is_sticker_whitelisted(chat_id, st.file_unique_id):
                log.info(
                    f"[MEDIA] Sticker is whitelisted (chat={chat_id}, file_unique_id={st.file_unique_id})"
                )
                return

            file_path = await message.download(
                file_name=os.path.join(temp_root, "sticker")
            )

            if st.is_animated:
                # TGS -> PNG
                png_path = os.path.join(temp_root, "sticker.png")
                converted = convert_tgs_to_png(file_path, png_path)
                if converted:
                    image_paths.append(converted)

            elif st.is_video:
                # Video sticker (WEBM) -> frames
                frame_dir = os.path.join(temp_root, "frames")
                os.makedirs(frame_dir, exist_ok=True)
                frames = extract_video_frames(file_path, frame_dir, max_frames=3)
                image_paths.extend(frames)

            else:
                # Static sticker (WEBP)
                image_paths.append(file_path)

        # -------------------------------------------------
        # Photos
        # -------------------------------------------------
        elif message.photo:
            file_path = await message.download(
                file_name=os.path.join(temp_root, "photo")
            )
            image_paths.append(file_path)

        # -------------------------------------------------
        # Video / GIF / Animation
        # -------------------------------------------------
        elif message.video or message.animation:
            file_path = await message.download(
                file_name=os.path.join(temp_root, "video")
            )
            frame_dir = os.path.join(temp_root, "frames")
            os.makedirs(frame_dir, exist_ok=True)
            frames = extract_video_frames(file_path, frame_dir, max_frames=3)
            image_paths.extend(frames)

        # -------------------------------------------------
        # Scan images (if any)
        # -------------------------------------------------
        if not image_paths:
            log.info(f"[MEDIA] No image paths extracted, skipping.")
            return

        score = scan_images_for_nsfw(image_paths)
        log.info(
            f"[MEDIA] Scan result chat={chat_id}, user={user.id}, score={score:.2f}, threshold={NSFW_THRESHOLD}"
        )

        if score >= NSFW_THRESHOLD:
            await delete_nsfw_message(client, message, score)
        else:
            log.info(
                f"[MEDIA] Content seems safe (chat={chat_id}, user={user.id}, score={score:.2f})"
            )

    except Exception as e:
        log.error(f"Error in media_guard: {e}")
    finally:
        # Remove temp files (privacy)
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
