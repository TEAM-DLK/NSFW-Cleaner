# dlk_nsfw_cleaner_full.py
# DLK NSFW Cleaner - FULL (improved)
# Requirements: python >=3.8
# pip install pyrogram pymongo python-dotenv pillow lottie
# optional: pip install nudenet open_nsfw2 clip mediapipe torch torchvision
# Set .env: API_ID, API_HASH, BOT_TOKEN, MONGO_URI
# Optional .env: LOG_CHAT_ID, OWNER_IDS, NSFW_THRESHOLD, AUTO_BLACKLIST_GLOBAL

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

from PIL import Image, UnidentifiedImageError

# --- Optional detectors (safe to be missing) ---
NudeDetector = None
OPENNSFW2 = None
CLIP = None
mediapipe = None
YahooNSFW = None

try:
    from nudenet import NudeDetector as _NudeDetector
    NudeDetector = _NudeDetector
except Exception:
    NudeDetector = None

try:
    import open_nsfw2 as _open_nsfw2  # type: ignore
    OPENNSFW2 = _open_nsfw2
except Exception:
    OPENNSFW2 = None

try:
    import clip as _clip  # type: ignore
    import torch  # type: ignore
    CLIP = {"clip": _clip, "torch": torch}
except Exception:
    CLIP = None

try:
    import mediapipe as _mp  # type: ignore
    mediapipe = _mp
except Exception:
    mediapipe = None

# --- load env ---
load_dotenv()
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.30"))
NSFW_STICKER_LIMIT = int(os.getenv("NSFW_STICKER_LIMIT", "3"))
PACK_STICKER_LIMIT = int(os.getenv("PACK_STICKER_LIMIT", str(NSFW_STICKER_LIMIT)))
MUTE_DURATION_SECONDS = int(os.getenv("MUTE_DURATION_SECONDS", "86400"))
CONFIRM_MSG_DELETE_SECONDS = int(os.getenv("CONFIRM_MSG_DELETE_SECONDS", "20"))
DELETE_LOG_MESSAGE_SECONDS = int(os.getenv("DELETE_LOG_MESSAGE_SECONDS", "20"))
MONGO_URI = os.getenv("MONGO_URI", "").strip()
LOG_CHAT_ID_ENV = os.getenv("LOG_CHAT_ID", "").strip()
LOG_CHAT_ID = int(LOG_CHAT_ID_ENV) if LOG_CHAT_ID_ENV else None
OWNER_IDS = set()
owner_env = os.getenv("OWNER_IDS", "").strip()
if owner_env:
    try:
        OWNER_IDS = set(int(s) for s in re.split(r"[,\s]+", owner_env) if s)
    except Exception:
        OWNER_IDS = set()
AUTO_BLACKLIST_GLOBAL = os.getenv("AUTO_BLACKLIST_GLOBAL", "0") in ("1", "true", "True")

NUDE_WEIGHT = float(os.getenv("NUDE_WEIGHT", "0.35"))
OPENNSFW2_WEIGHT = float(os.getenv("OPENNSFW2_WEIGHT", "0.25"))
CLIP_WEIGHT = float(os.getenv("CLIP_WEIGHT", "0.20"))
POSE_WEIGHT = float(os.getenv("POSE_WEIGHT", "0.10"))
YAHOO_WEIGHT = float(os.getenv("YAHOO_WEIGHT", "0.10"))

# --- startup checks ---
if not MONGO_URI:
    raise SystemExit("MONGO_URI must be set in .env")

START_TIME = time.time()
DEV_ABOUT_TEXT = "DLK DEVELOPER — DLK NSFW Cleaner"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s")
log = logging.getLogger("DLK-NSFW")

# --- MongoDB ---
mongo = MongoClient(MONGO_URI)
db = mongo["nsfw_guard"]

whitelist_col = db["sticker_whitelist"]
whitelist_col.create_index([("chat_id", ASCENDING), ("file_unique_id", ASCENDING)], unique=True)

pack_blacklist_col = db["sticker_pack_blacklist"]
pack_blacklist_col.create_index([("chat_id", ASCENDING), ("set_name", ASCENDING)], unique=True)

global_pack_blacklist_col = db["global_pack_blacklist"]
global_pack_blacklist_col.create_index([("set_name", ASCENDING)], unique=True)

violations_col = db["nsfw_violations"]
violations_col.create_index([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)

pending_col = db["pending_actions"]
try:
    pending_col.create_index("ts", expireAfterSeconds=3600)
except Exception:
    pass

# --- DB helpers ---
def is_sticker_whitelisted(chat_id: int, file_unique_id: str) -> bool:
    return whitelist_col.find_one({"chat_id": chat_id, "file_unique_id": file_unique_id}) is not None

def add_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.update_one({"chat_id": chat_id, "file_unique_id": file_unique_id},
                             {"$set": {"chat_id": chat_id, "file_unique_id": file_unique_id, "ts": int(time.time())}},
                             upsert=True)

def remove_sticker_whitelist(chat_id: int, file_unique_id: str) -> None:
    whitelist_col.delete_one({"chat_id": chat_id, "file_unique_id": file_unique_id})

def is_pack_blacklisted(chat_id: int, set_name: str) -> bool:
    if not set_name:
        return False
    return pack_blacklist_col.find_one({"chat_id": chat_id, "set_name": set_name}) is not None

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

def is_global_pack_blacklisted(set_name: str) -> bool:
    if not set_name:
        return False
    return global_pack_blacklist_col.find_one({"set_name": set_name}) is not None

def add_global_pack_blacklist(set_name: str) -> None:
    if not set_name:
        return
    global_pack_blacklist_col.update_one({"set_name": set_name},
                                        {"$set": {"set_name": set_name, "ts": int(time.time())}},
                                        upsert=True)

def remove_global_pack_blacklist(set_name: str) -> None:
    if not set_name:
        return
    global_pack_blacklist_col.delete_one({"set_name": set_name})

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
           "file_unique_id": file_unique_id or "", "set_name": set_name or "",
           "stickers": [], "set_names": [], "state": "open", "ts": int(time.time())}
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

# --- Pyrogram client ---
app = Client("dlk_nsfw_guard", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Detector helpers (same as your baseline) ---
_detector_instance = None
def get_detector():
    global _detector_instance
    if _detector_instance is None:
        if NudeDetector is None:
            log.warning("NudeNet not installed; skipping nudenet.")
            _detector_instance = None
        else:
            try:
                _detector_instance = NudeDetector()
            except Exception as e:
                log.warning(f"NudeNet init failed: {e}")
                _detector_instance = None
    return _detector_instance

_open_nsfw2_inst = None
def get_opennsfw2():
    global _open_nsfw2_inst
    if _open_nsfw2_inst is None and OPENNSFW2 is not None:
        try:
            _open_nsfw2_inst = OPENNSFW2.OpenNSFW2()
        except Exception as e:
            log.warning(f"OpenNSFW2 init failed: {e}")
            _open_nsfw2_inst = None
    return _open_nsfw2_inst

_clip_model = None
_clip_preprocess = None
_clip_device = None
_clip_text_tokens = None
def init_clip():
    global _clip_model, _clip_preprocess, _clip_device, _clip_text_tokens
    if CLIP is None:
        return False
    try:
        clip_pkg = CLIP["clip"]
        torch = CLIP["torch"]
        _clip_device = "cuda" if torch.cuda.is_available() else "cpu"
        _clip_model, _clip_preprocess = clip_pkg.load("ViT-B/32", device=_clip_device)
        _clip_model.eval()
        texts = ["pornography", "sexual content", "explicit nudity", "nude person", "adult content"]
        with torch.no_grad():
            _clip_text_tokens = clip_pkg.tokenize(texts).to(_clip_device)
        return True
    except Exception as e:
        log.warning(f"CLIP init failed: {e}")
        return False

_mp_pose = None
def init_mediapipe_pose():
    global _mp_pose
    if mediapipe is None:
        return False
    try:
        _mp_pose = mediapipe.solutions.pose.Pose(static_image_mode=True, min_detection_confidence=0.4)
        return True
    except Exception as e:
        log.warning(f"MediaPipe init failed: {e}")
        return False

# scoring functions (kept similar)
def score_with_nudenet(image_path: str) -> float:
    try:
        det = get_detector()
        if not det:
            return 0.0
        res = det.detect(image_path)
        max_score = 0.0
        for d in res or []:
            s = float(d.get("score", 0.0))
            if s > max_score:
                max_score = s
        log.debug(f"[NUDE] {image_path} -> {max_score:.3f}")
        return float(max_score)
    except Exception as e:
        log.warning(f"NudeNet scoring error: {e}")
        return 0.0

def score_with_opennsfw2(image_path: str) -> float:
    try:
        inst = get_opennsfw2()
        if not inst:
            return 0.0
        s = float(inst.score(image_path))
        log.debug(f"[OPENNSFW2] {image_path} -> {s:.3f}")
        return s
    except Exception as e:
        log.warning(f"OpenNSFW2 scoring error: {e}")
        return 0.0

def score_with_clip(image_path: str) -> float:
    try:
        if CLIP is None:
            return 0.0
        global _clip_preprocess, _clip_model, _clip_text_tokens
        if _clip_model is None:
            if not init_clip():
                return 0.0
        import torch
        clip_pkg = CLIP["clip"]
        device = "cuda" if torch.cuda.is_available() else "cpu"
        from PIL import Image as PILImage
        img = PILImage.open(image_path).convert("RGB")
        inp = _clip_preprocess(img).unsqueeze(0).to(device)
        with torch.no_grad():
            img_feat = _clip_model.encode_image(inp)
            txt_feat = _clip_model.encode_text(_clip_text_tokens)
            img_feat = img_feat / img_feat.norm(dim=-1, keepdim=True)
            txt_feat = txt_feat / txt_feat.norm(dim=-1, keepdim=True)
            sims = (img_feat @ txt_feat.T).squeeze(0)
            max_sim = sims.max().item()
            score = float((max_sim + 1.0) / 2.0)
        log.debug(f"[CLIP] {image_path} -> {score:.3f}")
        return score
    except Exception as e:
        log.warning(f"CLIP scoring error: {e}")
        return 0.0

def score_with_mediapipe_pose(image_path: str) -> float:
    try:
        if mediapipe is None:
            return 0.0
        global _mp_pose
        if _mp_pose is None:
            if not init_mediapipe_pose():
                return 0.0
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            return 0.0
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        results = _mp_pose.process(img_rgb)
        if not results or not getattr(results, "pose_landmarks", None):
            return 0.0
        landmarks = results.pose_landmarks.landmark
        def vis(idx):
            try:
                return float(landmarks[idx].visibility)
            except Exception:
                return 0.0
        torso_vis = (vis(11) + vis(12) + vis(23) + vis(24)) / 4.0
        score = min(1.0, torso_vis * 0.6)
        log.debug(f"[POSE] {image_path} -> torso_vis={torso_vis:.3f} score={score:.3f}")
        return float(score)
    except Exception as e:
        log.warning(f"MediaPipe pose error: {e}")
        return 0.0

def score_with_yahoo(image_path: str) -> float:
    try:
        if YahooNSFW is None:
            return 0.0
        global _yahoo_inst
        if _yahoo_inst is None:
            if not init_yahoo():
                return 0.0
        s = float(_yahoo_inst.score(image_path))
        log.debug(f"[YAHOO] {image_path} -> {s:.3f}")
        return s
    except Exception as e:
        log.warning(f"Yahoo score error: {e}")
        return 0.0

EXPLICIT_LABELS = {
    "FEMALE_GENITALIA_EXPOSED","MALE_GENITALIA_EXPOSED","GENITALIA_EXPOSED","ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED","FEMALE_NIPPLE_EXPOSED","MALE_BREAST_EXPOSED","BREAST_EXPOSED",
    "NUDE_FEMALE_CHEST","NUDE_MALE_CHEST","BUTTOCKS_EXPOSED","FEMALE_BUTTOCKS_EXPOSED",
    "MALE_BUTTOCKS_EXPOSED","SEXUAL_ACTIVITY","SEX_ACT","SEXUAL_INTERCOURSE","MASTURBATION",
    "ORAL_SEX","ANAL_SEX","PORNOGRAPHIC","SEXUALIZED_NUDITY","EXPLICIT_NUDITY",
    "HARDCORE","SOFTCORE","LEWD_CONTENT","OBSCENE_CONTENT","MINOR_NUDITY","CHILD_NUDITY",
    "CSAM_SUSPECT"
}

def aggregate_scores(image_paths: List[str]) -> float:
    best_overall = 0.0
    for path in image_paths:
        if not path or not os.path.exists(path):
            continue
        n_score = score_with_nudenet(path) if NUDE_WEIGHT > 0 else 0.0
        o_score = score_with_opennsfw2(path) if OPENNSFW2_WEIGHT > 0 else 0.0
        c_score = score_with_clip(path) if CLIP_WEIGHT > 0 else 0.0
        p_score = score_with_mediapipe_pose(path) if POSE_WEIGHT > 0 else 0.0
        y_score = score_with_yahoo(path) if YAHOO_WEIGHT > 0 else 0.0

        forced_explicit = False
        try:
            det = get_detector()
            if det:
                dets = det.detect(path)
                for d in dets or []:
                    lab = str(d.get("class", "")).upper()
                    sc = float(d.get("score", 0.0))
                    if lab in EXPLICIT_LABELS and sc >= 0.85:
                        forced_explicit = True
                        log.info(f"[FORCE] explicit label {lab} {sc:.2f}")
                        break
        except Exception:
            pass

        numerator = 0.0
        denom = 0.0
        for w, s in ((NUDE_WEIGHT, n_score),(OPENNSFW2_WEIGHT, o_score),(CLIP_WEIGHT, c_score),(POSE_WEIGHT, p_score),(YAHOO_WEIGHT, y_score)):
            if w > 0:
                numerator += w * s
                denom += w
        agg = (numerator / denom) if denom > 0 else 0.0

        if c_score > 0.85 and o_score > 0.7:
            agg = max(agg, 0.95)
        if forced_explicit:
            agg = max(agg, 0.99)
        agg = max(0.0, min(1.0, agg))
        log.info(f"[AGG] {os.path.basename(path)} -> n:{n_score:.3f} o:{o_score:.3f} c:{c_score:.3f} p:{p_score:.3f} y:{y_score:.3f} => agg={agg:.3f}")
        best_overall = max(best_overall, agg)
    return best_overall

# --- Utilities: convert / frames / download with retries ---
def ffmpeg_convert_to_jpeg(in_path: str, out_path: str) -> Optional[str]:
    try:
        cmd = ["ffmpeg","-hide_banner","-loglevel","error","-y","-i", in_path, "-frames:v","1","-q:v","2", out_path]
        subprocess.run(cmd, check=True)
        if os.path.exists(out_path):
            return out_path
    except Exception as e:
        log.debug(f"ffmpeg convert failed: {e}")
    return None

def convert_webp_to_jpeg_try_pil(webp_path: str, out_path: str) -> Optional[str]:
    try:
        with Image.open(webp_path) as img:
            rgb = img.convert("RGB")
            rgb.save(out_path, format="JPEG", quality=85)
        return out_path
    except UnidentifiedImageError:
        return None
    except Exception as e:
        log.debug(f"PIL webp->jpeg failed: {e}")
        return None

def convert_tgs_to_png(tgs_path: str, out_path: str) -> Optional[str]:
    try:
        from lottie import importers, exporters
        with open(tgs_path, "rb") as f:
            animation = importers.tgs.import_tgs(f)
        exporters.export_png(animation, out_path)
        return out_path
    except Exception as e:
        log.debug(f"TGS convert failed: {e}")
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
                    except Exception:
                        pass
            ff = ffmpeg_convert_to_jpeg(src_path, out_path)
            if ff:
                return ff
            return None
        except Exception as e:
            log.debug(f"prepare_image primary exception: {e}")
            ff = ffmpeg_convert_to_jpeg(src_path, out_path)
            if ff:
                return ff
            return None
    except Exception as e:
        log.debug(f"prepare_image failed: {e}")
        return None

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
        log.debug(f"Frame extraction failed: {e}")
    return frames

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

# --- messaging helpers ---
async def safe_send_message(client: Client, chat_id: int, text: str, reply_markup=None, thread_id: Optional[int] = None):
    try:
        if thread_id:
            return await client.send_message(chat_id, text, reply_markup=reply_markup, message_thread_id=thread_id)
        else:
            return await client.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        log.debug(f"safe_send_message failed: {e}")
        return None

async def safe_copy_message(client: Client, to_chat_id: int, from_chat_id: int, message_id: int, thread_id: Optional[int] = None):
    try:
        if thread_id:
            return await client.copy_message(to_chat_id, from_chat_id, message_id, message_thread_id=thread_id)
        else:
            return await client.copy_message(to_chat_id, from_chat_id, message_id)
    except Exception as e:
        log.debug(f"safe_copy_message failed: {e}")
        return None

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
        [InlineKeyboardButton("📖 How to use", callback_data="page_how"), InlineKeyboardButton("🛠 Features", callback_data="page_features")],
        [InlineKeyboardButton("🔐 Permissions", callback_data="page_perms"), InlineKeyboardButton("ℹ️ About", callback_data="page_about")]
    ])

def get_main_text() -> str:
    uptime = format_uptime(int(time.time() - START_TIME))
    return (
        "🛡 <b>DLK NSFW Cleaner</b>\n\n"
        "Protect your Telegram groups from explicit content automatically.\n\n"
        f"• Auto-scan stickers/photos/videos\n• NSFW threshold: <code>{NSFW_THRESHOLD}</code>\n• Per-chat pack blacklists & global blacklist\n\n"
        f"Uptime: <code>{uptime}</code>\n\n"
        "Use /help for commands."
    )

# --- Commands: start/help/about/status/ping ---
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    me = await client.get_me()
    bot_username = me.username or "DLKNSFWBot"
    main_text = get_main_text()
    keyboard = build_main_keyboard(bot_username)
    await message.reply_text(main_text, reply_markup=keyboard)

@app.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    help_text = (
        "📖 <b>Help - DLK NSFW Cleaner</b>\n\n"
        "• /free (reply to sticker) — whitelist this sticker for the chat (admin only)\n"
        "• /addpack (reply to sticker) — add pack to this chat's blacklist (admin only)\n"
        "• /addglobalpack (reply to sticker) — add pack to GLOBAL blacklist (owner/admin)\n"
        "• /rmglobalpack (reply to sticker) — remove pack from global blacklist (owner/admin)\n"
        "• /status — bot permissions in group\n"
        "Auto-detected explicit sticker packs may be auto-blacklisted."
    )
    await message.reply_text(help_text)

@app.on_message(filters.command("about"))
async def about_cmd(client: Client, message: Message):
    await message.reply_text(f"ℹ️ {DEV_ABOUT_TEXT}")

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
                f"🗑 Delete messages: <b>{'✅' if can_delete else '❌'}</b>\n"
                f"🚫 Restrict/Mute users: <b>{'✅' if can_restrict else '❌'}</b>\n\n"
                f"⏱ Uptime: <code>{uptime}</code>\n")
    except Exception as e:
        text = f"⚠️ Failed to read permissions: <code>{e}</code>"
    await message.reply_text(text)

@app.on_message(filters.command("ping"))
async def ping_cmd(client: Client, message: Message):
    uptime = format_uptime(int(time.time() - START_TIME))
    await message.reply_text(f"🏓 <b>Pong!</b>\nUptime: <code>{uptime}</code>")

# --- Admin commands for pack management ---
async def is_user_admin(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False

@app.on_message(filters.command("addpack") & filters.group)
async def addpack_cmd(client: Client, message: Message):
    user = message.from_user
    chat_id = message.chat.id
    if not await is_user_admin(client, chat_id, user.id):
        await message.reply_text("❌ Only group admins can use this.", quote=True)
        return
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await message.reply_text("Reply to a sticker from the pack you want to block with /addpack.", quote=True)
        return
    st = message.reply_to_message.sticker
    set_name = getattr(st, "set_name", None) or ""
    if not set_name:
        await message.reply_text("That sticker doesn't belong to a pack.", quote=True)
        return
    add_pack_blacklist(chat_id, set_name)
    await message.reply_text(f"✅ Pack <code>{set_name}</code> blacklisted for this chat.", quote=True)

@app.on_message(filters.command("addglobalpack") & filters.group)
async def addglobalpack_cmd(client: Client, message: Message):
    user = message.from_user
    chat_id = message.chat.id
    allowed = (user.id in OWNER_IDS) or await is_user_admin(client, chat_id, user.id)
    if not allowed:
        await message.reply_text("❌ Only Owners or group admins can use this.", quote=True)
        return
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await message.reply_text("Reply to a sticker from the pack you want to add to global blacklist with /addglobalpack.", quote=True)
        return
    st = message.reply_to_message.sticker
    set_name = getattr(st, "set_name", None) or ""
    if not set_name:
        await message.reply_text("That sticker doesn't belong to a pack.", quote=True)
        return
    add_global_pack_blacklist(set_name)
    await message.reply_text(f"✅ Pack <code>{set_name}</code> added to GLOBAL blacklist.", quote=True)

@app.on_message(filters.command("rmglobalpack") & filters.group)
async def rmglobalpack_cmd(client: Client, message: Message):
    user = message.from_user
    chat_id = message.chat.id
    allowed = (user.id in OWNER_IDS) or await is_user_admin(client, chat_id, user.id)
    if not allowed:
        await message.reply_text("❌ Only Owners or group admins can use this.", quote=True)
        return
    if not message.reply_to_message or not message.reply_to_message.sticker:
        await message.reply_text("Reply to a sticker from the pack you want to remove from global blacklist with /rmglobalpack.", quote=True)
        return
    st = message.reply_to_message.sticker
    set_name = getattr(st, "set_name", None) or ""
    if not set_name:
        await message.reply_text("That sticker doesn't belong to a pack.", quote=True)
        return
    remove_global_pack_blacklist(set_name)
    await message.reply_text(f"✅ Pack <code>{set_name}</code> removed from GLOBAL blacklist.", quote=True)

# --- core group media handler ---
async def delete_nsfw_message(client: Client, message: Message, score: float, reason: str = ""):
    chat = message.chat
    user = message.from_user
    thread_id = getattr(message, "message_thread_id", None)
    try:
        try:
            await message.delete()
        except Exception:
            try:
                await client.delete_messages(chat.id, getattr(message, "message_id", getattr(message, "id", None)))
            except Exception:
                pass
        log.info(f"Deleted NSFW content in chat={chat.id}, user={(user.id if user else 'N/A')}, score={score:.2f}")
    except Exception as e:
        log.debug(f"delete_nsfw_message error: {e}")

    try:
        mention = user.mention if user else "<b>Unknown</b>"
        kb_rows = []
        if user and getattr(user, "id", None):
            kb_rows.append([InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{chat.id}:{user.id}"),
                            InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{chat.id}:{user.id}")])
        kb_rows.append([InlineKeyboardButton("✖️ Close", callback_data="close_log")])
        kb = InlineKeyboardMarkup(kb_rows)
        text = (
            "🔍 <b>NSFW content deleted</b>\n\n"
            f"👤 User: {mention}\n"
            f"💬 Chat: <code>{chat.title or chat.id}</code>\n"
            f"📊 Score: <code>{score:.2f}</code>\n"
            f"📝 Reason: {reason}"
        )
        sent = await safe_send_message(client, chat.id, text, reply_markup=kb, thread_id=thread_id)
        if sent:
            asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
        if LOG_CHAT_ID:
            try:
                await client.send_message(LOG_CHAT_ID, text, reply_markup=kb)
            except Exception:
                pass
    except Exception as e:
        log.debug(f"Failed to send deletion log: {e}")

async def handle_nsfw_sticker_violation(client: Client, message: Message, score: float, reason: str = "NSFW content"):
    chat_id = message.chat.id
    user = message.from_user
    thread_id = getattr(message, "message_thread_id", None)
    if not user:
        return
    if await is_user_admin(client, chat_id, user.id):
        log.info(f"User {user.id} is admin — skipping punish.")
        return
    new_count = increment_violation(chat_id, user.id)
    log.info(f"Violation count for user {user.id} in {chat_id}: {new_count}")
    if new_count <= NSFW_STICKER_LIMIT:
        return
    if not await bot_can_restrict_members(client, chat_id):
        log.warning("Bot lacks restrict permission.")
        return
    until_date = datetime.utcnow() + timedelta(seconds=MUTE_DURATION_SECONDS)
    permissions = ChatPermissions(
        can_send_messages=False, can_send_media_messages=False, can_send_polls=False,
        can_send_other_messages=False, can_add_web_page_previews=False,
        can_change_info=False, can_invite_users=False, can_pin_messages=False
    )
    try:
        await client.restrict_chat_member(chat_id, user.id, permissions=permissions, until_date=until_date)
        try:
            reply_msg = await message.reply_text(f"🚫 {user.mention} muted for repeated NSFW content (>{NSFW_STICKER_LIMIT}).")
            asyncio.create_task(schedule_delete(reply_msg, DELETE_LOG_MESSAGE_SECONDS))
        except Exception:
            pass
    except Exception as e:
        log.debug(f"Failed to mute user: {e}")

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
    except Exception:
        return False

@app.on_message(filters.group & (filters.sticker | filters.photo | filters.video | filters.animation | filters.document))
async def group_media_handler(client: Client, message: Message):
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    user = message.from_user
    if not user:
        return
    if not await is_bot_admin(client, chat_id):
        log.debug(f"Bot not admin in {chat_id}; skipping scan.")
        return
    try:
        # Sticker pack blacklist quick check
        if message.sticker:
            set_name = getattr(message.sticker, "set_name", None) or ""
            file_unique_id = message.sticker.file_unique_id
            # whitelist check
            if is_sticker_whitelisted(chat_id, file_unique_id):
                return
            # per-chat pack blacklisted -> delete and increment
            if set_name and (is_pack_blacklisted(chat_id, set_name) or is_global_pack_blacklisted(set_name)):
                try:
                    await message.delete()
                except Exception:
                    try:
                        await client.delete_messages(chat_id, getattr(message, "message_id", getattr(message, "id", None)))
                    except Exception:
                        pass
                new_count = increment_violation(chat_id, user.id)
                if new_count > PACK_STICKER_LIMIT and not await is_user_admin(client, chat_id, user.id) and await bot_can_restrict_members(client, chat_id):
                    await handle_nsfw_sticker_violation(client, message, score=0.0, reason=f"Sent blacklisted pack {set_name}")
                try:
                    sent = await safe_send_message(client, chat_id, f"🗑 Deleted sticker from blacklisted pack <code>{set_name}</code> by {user.mention}.", thread_id=thread_id)
                    if sent:
                        asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
                except Exception:
                    pass
                return

        tmpdir = tempfile.mkdtemp(prefix="nsfwscan_")
        paths = []
        try:
            if message.sticker:
                base_dest = os.path.join(tmpdir, "sticker")
                file_ref = getattr(message.sticker, "file_id", message)
                file_path = await download_media_with_retries(client, message, file_ref, base_dest)
                if file_path:
                    prepared = prepare_image_for_detector(file_path, tmpdir)
                    if prepared:
                        paths.append(prepared)
            elif message.photo:
                dest = os.path.join(tmpdir, "photo.jpg")
                file_path = await download_media_with_retries(client, message.photo, message.photo, dest)
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

            score = aggregate_scores(paths)
            if score >= NSFW_THRESHOLD:
                # auto-blacklist pack if sticker and set_name present
                if message.sticker:
                    set_name = getattr(message.sticker, "set_name", None) or ""
                    if set_name:
                        add_pack_blacklist(chat_id, set_name)
                        # optionally add to global blacklist if ENV says so
                        if AUTO_BLACKLIST_GLOBAL:
                            add_global_pack_blacklist(set_name)
                        try:
                            sent = await safe_send_message(client, chat_id, f"🚫 Automatically blacklisted sticker pack <code>{set_name}</code> in chat <code>{chat_id}</code>.", thread_id=thread_id)
                            if sent:
                                asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
                        except Exception:
                            pass
                await delete_nsfw_message(client, message, score, reason=f"NSFW detection score {score:.2f}")
                await handle_nsfw_sticker_violation(client, message, score, reason=f"NSFW detection score {score:.2f}")
            else:
                log.debug(f"No NSFW detected (score {score:.3f}) in chat {chat_id}")
        finally:
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"group_media_handler error: {e}")

# --- callback handler for inline mod actions + pages ---
@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    data = query.data or ""
    user = query.from_user

    if data == "page_how":
        await query.edit_message_text("📖 How to use: Add bot as admin with delete & restrict permissions.")
        await query.answer()
        return
    if data == "close_log":
        try:
            await query.message.delete()
            await query.answer()
        except Exception:
            await query.answer("Unable to remove message.", show_alert=True)
        return

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
            await query.answer("You don't have permission.", show_alert=True)
            return
        if not await bot_can_restrict_members(client, target_chat):
            await query.answer("Bot lacks restrict/ban permissions in target chat.", show_alert=True)
            return
        if action == "unmute":
            permissions = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_change_info=False, can_invite_users=True, can_pin_messages=False)
            try:
                await client.restrict_chat_member(target_chat, target_user, permissions=permissions)
                await query.edit_message_text("🔈 Unmuted.")
                await query.answer("User unmuted.")
            except Exception as e:
                await query.answer(f"Failed to unmute: {e}", show_alert=True)
            return
        elif action == "ban":
            try:
                await client.ban_chat_member(target_chat, target_user)
                await query.edit_message_text("⛔ Banned.")
                await query.answer("User banned.")
            except Exception as e:
                await query.answer(f"Failed to ban: {e}", show_alert=True)
            return

    await query.answer()

# --- start-up initializers ---
if __name__ == "__main__":
    log.info("Starting DLK NSFW Cleaner - improved full")
    # optional detector warmups (non-blocking)
    loop = asyncio.get_event_loop()
    try:
        loop.run_in_executor(None, get_detector)
    except Exception:
        pass
    try:
        if CLIP is not None:
            loop.run_in_executor(None, init_clip)
    except Exception:
        pass
    try:
        if mediapipe is not None:
            loop.run_in_executor(None, init_mediapipe_pose)
    except Exception:
        pass
    app.run()
