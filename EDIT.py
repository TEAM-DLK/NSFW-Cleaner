# DLK NSFW Cleaner - FULL integrated version with optional detectors
# Requirements:
#   python >= 3.8
#   pip install pyrogram pymongo python-dotenv pillow lottie
#   optional: pip install nudenet mediapipe torch torchvision git+https://github.com/openai/CLIP.git
# System: ffmpeg installed and accessible in PATH
#
# Save as dlk_nsfw_cleaner.py and run: python dlk_nsfw_cleaner.py
# Create .env with: API_ID, API_HASH, BOT_TOKEN, MONGO_URI
# Optional .env: LOG_CHAT_ID, OWNER_IDS, NSFW_THRESHOLD, NUDE_WEIGHT, OPENNSFW2_WEIGHT, CLIP_WEIGHT, POSE_WEIGHT, YAHOO_WEIGHT

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

# Optional detector imports (safe)
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

# open_nsfw2 package name varies — leave optional
try:
    import open_nsfw2 as _open_nsfw2  # type: ignore
    OPENNSFW2 = _open_nsfw2
except Exception:
    OPENNSFW2 = None

# CLIP (OpenAI) optional
try:
    import clip as _clip  # type: ignore
    import torch  # type: ignore
    CLIP = {"clip": _clip, "torch": torch}
except Exception:
    CLIP = None

# MediaPipe optional
try:
    import mediapipe as _mp  # type: ignore
    mediapipe = _mp
except Exception:
    mediapipe = None

# Yahoo NSFW hypothetical wrapper optional
try:
    import yahoo_nsfw as _yahoo_nsfw  # type: ignore
    YahooNSFW = _yahoo_nsfw
except Exception:
    YahooNSFW = None

# Load .env
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
if not MONGO_URI:
    raise SystemExit("MONGO_URI is not set in environment. Set it in your .env file.")

LOG_CHAT_ID_ENV = os.getenv("LOG_CHAT_ID", "").strip()
LOG_CHAT_ID = int(LOG_CHAT_ID_ENV) if LOG_CHAT_ID_ENV else None

OWNER_IDS = set()
owner_env = os.getenv("OWNER_IDS", "").strip()
if owner_env:
    try:
        OWNER_IDS = set(int(s) for s in re.split(r"[,\s]+", owner_env) if s)
    except Exception:
        OWNER_IDS = set()

# Detector weights
NUDE_WEIGHT = float(os.getenv("NUDE_WEIGHT", "0.30"))
OPENNSFW2_WEIGHT = float(os.getenv("OPENNSFW2_WEIGHT", "0.30"))
CLIP_WEIGHT = float(os.getenv("CLIP_WEIGHT", "0.30"))
POSE_WEIGHT = float(os.getenv("POSE_WEIGHT", "0.30"))
YAHOO_WEIGHT = float(os.getenv("YAHOO_WEIGHT", "0.30"))

# per-model thresholds (not strictly required; used for heuristics)
OPENNSFW2_THRESHOLD = float(os.getenv("OPENNSFW2_THRESHOLD", "0.30"))
CLIP_THRESHOLD = float(os.getenv("CLIP_THRESHOLD", "0.30"))
POSE_THRESHOLD = float(os.getenv("POSE_THRESHOLD", "0.30"))
YAHOO_THRESHOLD = float(os.getenv("YAHOO_THRESHOLD", "0.30"))

START_TIME = time.time()

OFFICIAL_CHANNEL = "https://t.me/DLKDevelopers"
LOG_PUBLIC_URL = "https://t.me/DOOZY_OFF"
START_PHOTO_URL = "https://i.ibb.co/WNzKw5qk/DLKNSFWCleaner.png"

DEV_ABOUT_TEXT = (
    "DLK DEVELOPER\n"
    "SEE THE FUTURE THROUGH MY VISION"
)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
)
log = logging.getLogger("DLK-NSFW")

# MongoDB setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["nsfw_guard"]

whitelist_col = db["sticker_whitelist"]
whitelist_col.create_index(
    [("chat_id", ASCENDING), ("file_unique_id", ASCENDING)],
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

# DB helpers
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

# Pyrogram client
app = Client(
    "dlk_nsfw_guard",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# Detector helpers & scoring implementations
_detector_instance = None
def get_detector():
    global _detector_instance
    if _detector_instance is None:
        if NudeDetector is None:
            log.warning("NudeNet not installed; nudenet detector disabled.")
            _detector_instance = None
        else:
            try:
                log.info("Loading NudeNet detector...")
                _detector_instance = NudeDetector()
            except Exception as e:
                log.warning(f"Failed to load NudeNet: {e}")
                _detector_instance = None
    return _detector_instance

_open_nsfw2_inst = None
def get_opennsfw2():
    global _open_nsfw2_inst
    if _open_nsfw2_inst is None:
        if OPENNSFW2 is None:
            _open_nsfw2_inst = None
        else:
            try:
                _open_nsfw2_inst = OPENNSFW2.OpenNSFW2()  # package dependent
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

_yahoo_inst = None
def init_yahoo():
    global _yahoo_inst
    if YahooNSFW is None:
        _yahoo_inst = None
        return False
    try:
        _yahoo_inst = YahooNSFW.YahooNSFWModel()
        return True
    except Exception as e:
        log.warning(f"Yahoo init failed: {e}")
        _yahoo_inst = None
        return False

# Per-model scoring (return 0.0..1.0)
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
        log.debug(f"[NUDENET] {image_path} -> {max_score:.3f}")
        return float(max_score)
    except Exception as e:
        log.warning(f"NudeNet score error: {e}")
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
        log.warning(f"OpenNSFW2 score error: {e}")
        return 0.0

def score_with_clip(image_path: str) -> float:
    try:
        if CLIP is None:
            return 0.0
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
        log.warning(f"CLIP score error: {e}")
        return 0.0

def score_with_mediapipe_pose(image_path: str) -> float:
    try:
        if mediapipe is None:
            return 0.0
        global _mp_pose
        if _mp_pose is None:
            if not init_mediapipe_pose():
                return 0.0
        import cv2  # cv2 required for mediapipe image handling
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
        log.warning(f"MediaPipe score error: {e}")
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

# Explicit label set (NudeNet classes considered explicit)
EXPLICIT_LABELS = {
    "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "GENITALIA_EXPOSED", "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED", "FEMALE_NIPPLE_EXPOSED", "MALE_BREAST_EXPOSED", "BREAST_EXPOSED",
    "NUDE_FEMALE_CHEST", "NUDE_MALE_CHEST", "BUTTOCKS_EXPOSED", "FEMALE_BUTTOCKS_EXPOSED",
    "MALE_BUTTOCKS_EXPOSED", "SEXUAL_ACTIVITY", "SEX_ACT", "SEXUAL_INTERCOURSE",
    "MASTURBATION", "ORAL_SEX", "ANAL_SEX", "PORNOGRAPHIC", "SEXUALIZED_NUDITY",
    "EXPLICIT_NUDITY", "ADULT_CONTENT", "HARDCORE", "SOFTCORE", "LEWD_CONTENT",
    "OBSCENE_CONTENT", "INAPPROPRIATE_CONTENT", "MINOR_NUDITY", "CHILD_NUDITY",
    "CSAM_SUSPECT", "ADULT_TOY", "SEX_TOY", "FETISH_CONTENT",
}

# Aggregate multiple model signals into single score [0,1]
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

        # forced explicit override via nudenet labels if available
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
                        log.info(f"[FORCE] explicit label {lab} score={sc:.2f} -> forcing high agg")
                        break
        except Exception:
            pass

        # weighted average
        numerator = 0.0
        denom = 0.0
        for w, s in ((NUDE_WEIGHT, n_score),(OPENNSFW2_WEIGHT, o_score),(CLIP_WEIGHT, c_score),(POSE_WEIGHT, p_score),(YAHOO_WEIGHT, y_score)):
            if w > 0:
                numerator += w * s
                denom += w
        agg = (numerator / denom) if denom > 0 else 0.0

        # heuristics / boosts
        if c_score > 0.85 and o_score > 0.7:
            agg = max(agg, 0.95)
        if forced_explicit:
            agg = max(agg, 0.99)
        agg = max(0.0, min(1.0, agg))
        log.info(f"[AGG] {os.path.basename(path)} => n:{n_score:.3f} o:{o_score:.3f} c:{c_score:.3f} p:{p_score:.3f} y:{y_score:.3f} -> agg={agg:.3f}")
        best_overall = max(best_overall, agg)
    return best_overall

# Conversion & helper utilities (same as earlier)
def ffmpeg_convert_to_jpeg(in_path: str, out_path: str) -> Optional[str]:
    try:
        cmd = [
            "ffmpeg","-hide_banner","-loglevel","error","-y",
            "-i", in_path,
            "-frames:v","1","-q:v","2", out_path
        ]
        subprocess.run(cmd, check=True)
        if os.path.exists(out_path):
            return out_path
    except Exception as e:
        log.warning(f"ffmpeg convert failed: {e}")
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
        log.warning(f"PIL webp->jpeg failed: {e}")
        return None

def convert_tgs_to_png(tgs_path: str, out_path: str) -> Optional[str]:
    try:
        from lottie import importers, exporters
        with open(tgs_path, "rb") as f:
            animation = importers.tgs.import_tgs(f)
        exporters.export_png(animation, out_path)
        return out_path
    except Exception as e:
        log.warning(f"TGS convert failed: {e}")
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
            log.warning(f"prepare_image primary exception: {e}")
            ff = ffmpeg_convert_to_jpeg(src_path, out_path)
            if ff:
                return ff
            return None
    except Exception as e:
        log.warning(f"prepare_image failed: {e}")
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
        log.warning(f"Frame extraction failed: {e}")
    return frames

# safe send / copy
async def safe_send_message(client: Client, chat_id: int, text: str, reply_markup=None, thread_id: Optional[int] = None):
    try:
        if thread_id:
            return await client.send_message(chat_id, text, reply_markup=reply_markup, message_thread_id=thread_id)
        else:
            return await client.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        log.warning(f"safe_send_message failed: {e}")
        return None

async def safe_copy_message(client: Client, to_chat_id: int, from_chat_id: int, message_id: int, thread_id: Optional[int] = None):
    try:
        if thread_id:
            return await client.copy_message(to_chat_id, from_chat_id, message_id, message_thread_id=thread_id)
        else:
            return await client.copy_message(to_chat_id, from_chat_id, message_id)
    except Exception as e:
        log.warning(f"safe_copy_message failed: {e}")
        return None

async def delete_nsfw_message(client: Client, message: Message, score: float):
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
        log.warning(f"delete_nsfw_message error: {e}")

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
            f"🆔 Chat ID: <code>{chat.id}</code>\n"
            f"🆔 User ID: <code>{user.id if user else 'N/A'}</code>\n"
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
        log.warning(f"Failed to send deletion log: {e}")

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
        [InlineKeyboardButton("🔐 Permissions", callback_data="page_perms"), InlineKeyboardButton("ℹ️ About", callback_data="page_about")],
        [InlineKeyboardButton("📢 Updates & Logs", url=LOG_PUBLIC_URL)]
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
        "2) The bot scans stickers, photos, GIFs & videos; explicit content is deleted.\n"
        "3) /free to whitelist a sticker in chat; /blockpack to block packs.\n"
    )

def get_features_text() -> str:
    return "🛠 <b>Features</b>\n\n• Auto-scan • Pack blacklist • Per-chat whitelist • Auto-mute"

def get_perms_text() -> str:
    return "🔐 <b>Required Permissions</b>\n\nMake the bot an admin with Delete & Restrict permissions."

def get_about_text() -> str:
    return f"ℹ️ <b>About DLK NSFW Cleaner</b>\n\nDeveloper: <code>{DEV_ABOUT_TEXT}</code>\nLogs: {LOG_PUBLIC_URL}"

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

# Commands
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    me = await client.get_me()
    bot_username = me.username or "DLKNSFWBot"
    keyboard = build_main_keyboard(bot_username)
    main_text = get_main_text()
    payload = ""
    if message.text:
        parts = message.text.split()
        if len(parts) > 1:
            payload = parts[1].strip()
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
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"),
                                       InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")]])
            await message.reply_text(f"You're about to whitelist a sticker in chat <code>{pending['chat_id']}</code>.\nPress Confirm to whitelist it.", reply_markup=kb)
            return
        if action == "unblacklist":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Remove pack blacklist", callback_data=f"unblack_confirm:{pending_id}"),
                                       InlineKeyboardButton("❌ Cancel", callback_data=f"unblack_cancel:{pending_id}")]])
            await message.reply_text(f"You're about to remove pack blacklist <code>{pending.get('set_name')}</code> in chat <code>{pending['chat_id']}</code>.", reply_markup=kb)
            return
        if action == "bulk_block":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"block_cancel:{pending_id}")]])
            await message.reply_text("Bulk block flow — send stickers here, then send DONE to finalize.", reply_markup=kb)
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

# /free /unfree /blockpack implementations
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
    bot_username = me.username or "DLKNSFWBot"
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
            grp_msg = await message.reply_text("✅ Whitelist request created. Admin: open my private chat and confirm.", reply_markup=confirm_kb_group, quote=True)
            asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
        except Exception as e:
            log.warning(f"free_cmd group helper failed: {e}")
        try:
            copied = await safe_copy_message(client, user.id, sticker_msg.chat.id, getattr(sticker_msg, "message_id", getattr(sticker_msg, "id", None)))
            confirm_kb_pm = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"),
                                                  InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")]])
            pm_text = await client.send_message(user.id, f"You're about to whitelist a sticker in chat <code>{chat_id}</code>. Press Confirm.", reply_markup=confirm_kb_pm)
            if copied:
                asyncio.create_task(schedule_delete(copied, CONFIRM_MSG_DELETE_SECONDS))
            asyncio.create_task(schedule_delete(pm_text, CONFIRM_MSG_DELETE_SECONDS))
        except Exception as e:
            log.info(f"Could not PM admin for free_cmd: {e}")
    else:
        pending_id = create_pending_action("whitelist", chat_id, user.id, "", "")
        deep_link = f"https://t.me/{bot_username}?start=free_{pending_id}"
        confirm_kb_group = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Open private (send sticker there)", url=deep_link),
                                                  InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel_group:{pending_id}")]])
        try:
            grp_msg = await message.reply_text("✅ Whitelist request created. Open my private chat and send the sticker then Confirm.", reply_markup=confirm_kb_group, quote=True)
            asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
        except Exception as e:
            log.warning(f"free_cmd no-reply helper failed: {e}")
    try:
        await safe_send_message(client, chat_id, f"🛡 Whitelist requested by admin {user.mention} in chat <code>{chat_id}</code>.", thread_id=thread_id)
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
    if set_name and is_pack_blacklisted(chat_id, set_name):
        me = await client.get_me()
        bot_username = me.username or "DLKNSFWBot"
        pending_id = create_pending_action("unblacklist", chat_id, user.id, file_unique_id, set_name)
        deep_link = f"https://t.me/{bot_username}?start=unblack_{pending_id}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Remove pack-blacklist (confirm in PM)", url=deep_link),
                                     InlineKeyboardButton("❌ Cancel", callback_data=f"unblack_cancel_group:{pending_id}")]])
        try:
            grp = await message.reply_text(f"⚠️ Pack <code>{set_name}</code> is blacklisted in this chat. Confirm in my PM to remove pack-level blacklist.", reply_markup=kb, quote=True)
            asyncio.create_task(schedule_delete(grp, CONFIRM_MSG_DELETE_SECONDS))
        except Exception:
            pass
    try:
        await safe_send_message(client, chat_id, f"❌ Unwhitelist requested by admin {user.mention} in chat <code>{chat_id}</code>.", thread_id=thread_id)
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
    bot_username = me.username or "DLKNSFWBot"
    pending_id = create_pending_action("bulk_block", chat_id, user.id, "", "")
    deep_link = f"https://t.me/{bot_username}?start=block_{pending_id}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Open private and send stickers", url=deep_link),
                                InlineKeyboardButton("❌ Cancel", callback_data=f"block_cancel_group:{pending_id}")]])
    try:
        grp_msg = await message.reply_text("📦 Bulk block: open my private chat, send stickers, then send DONE to finalize.", reply_markup=kb, quote=True)
        asyncio.create_task(schedule_delete(grp_msg, CONFIRM_MSG_DELETE_SECONDS))
    except Exception as e:
        log.warning(f"blockpack_cmd helper failed: {e}")
    try:
        await safe_send_message(client, chat_id, f"Bulk block requested by admin {user.mention} in chat <code>{chat_id}</code>. Pending ID: <code>{pending_id}</code>", thread_id=thread_id)
    except Exception:
        pass

# Private handlers for collecting stickers & DONE
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
            pending_id = str(pending_free["_id"])
            confirm_kb_pm = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm whitelist", callback_data=f"free_confirm:{pending_id}"),
                                                  InlineKeyboardButton("❌ Cancel", callback_data=f"free_cancel:{pending_id}")]])
            try:
                copied = await safe_copy_message(client, user.id, message.chat.id, getattr(message, "message_id", getattr(message, "id", None)))
                pm_text = await client.send_message(user.id, f"Sticker received for chat <code>{pending_free['chat_id']}</code>. Press Confirm.", reply_markup=confirm_kb_pm)
                if copied:
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
        await message.reply_text("This sticker doesn't belong to a pack; send stickers from the pack(s) you want to block.")
        return
    push_sticker_to_pending(pending_id, st.file_unique_id)
    push_setname_to_pending(pending_id, set_name)
    await message.reply_text(f"Collected sticker from pack <code>{set_name}</code>. Send more or send DONE to finalize.", quote=True)

@app.on_message(filters.private & filters.regex(r"^\s*DONE\s*$", flags=re.IGNORECASE))
async def private_done_handler(client: Client, message: Message):
    user = message.from_user
    pending = get_latest_pending_for_admin(user.id, "bulk_block")
    if not pending:
        await message.reply_text("No open bulk-block request found.")
        return
    pending_id = str(pending["_id"])
    set_names = pending.get("set_names", []) or []
    if not set_names:
        await message.reply_text("No sticker packs collected. Send stickers first.", quote=True)
        return
    chat_id = pending.get("chat_id")
    for set_name in set_names:
        add_pack_blacklist(chat_id, set_name)
    finalize_pending_action(pending_id)
    await message.reply_text(f"✅ Blocked {len(set_names)} sticker pack(s) for chat <code>{chat_id}</code>.", quote=True)
    try:
        await safe_send_message(client, chat_id, f"📦 Admin {user.mention} blocked sticker packs {', '.join(set_names)} for chat <code>{chat_id}</code>.")
    except Exception:
        pass

# Reliable download helper
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

# Group media handler — scans and deletes based on aggregate score
@app.on_message(filters.group & (filters.sticker | filters.photo | filters.video | filters.animation | filters.document))
async def group_media_handler(client: Client, message: Message):
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    user = message.from_user
    if not user:
        return
    if not await is_bot_admin(client, chat_id):
        log.info(f"Bot not admin in chat {chat_id}; skipping.")
        return
    try:
        if message.sticker:
            set_name = getattr(message.sticker, "set_name", None) or ""
            file_unique_id = message.sticker.file_unique_id
            if is_sticker_whitelisted(chat_id, file_unique_id):
                return
            if set_name and is_pack_blacklisted(chat_id, set_name):
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

            score = aggregate_scores(paths)
            if score >= NSFW_THRESHOLD:
                if message.sticker:
                    set_name = getattr(message.sticker, "set_name", None) or ""
                    if set_name:
                        add_pack_blacklist(chat_id, set_name)
                        try:
                            sent = await safe_send_message(client, chat_id, f"🚫 Automatically blacklisted sticker pack <code>{set_name}</code> in chat <code>{chat_id}</code>.", thread_id=thread_id)
                            if sent:
                                asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
                        except Exception:
                            pass
                await delete_nsfw_message(client, message, score)
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

# Violation handling
async def notify_mute_to_log(client: Client, chat_id: int, user, violations: int, score: float, reason: str, thread_id: Optional[int] = None):
    if not chat_id:
        return
    try:
        mention = user.mention if user else "<b>Unknown</b>"
        kb_rows = []
        if user and getattr(user, "id", None):
            kb_rows.append([InlineKeyboardButton("🔈 Unmute", callback_data=f"mod_action:unmute:{chat_id}:{user.id}"),
                            InlineKeyboardButton("⛔ Ban", callback_data=f"mod_action:ban:{chat_id}:{user.id}")])
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
        sent = await safe_send_message(client, chat_id, text, reply_markup=kb, thread_id=thread_id)
        if sent:
            asyncio.create_task(schedule_delete(sent, DELETE_LOG_MESSAGE_SECONDS))
    except Exception as e:
        log.warning(f"notify_mute_to_log error: {e}")

async def handle_nsfw_sticker_violation(client: Client, message: Message, score: float, reason: str = "NSFW content"):
    chat_id = message.chat.id
    user = message.from_user
    thread_id = getattr(message, "message_thread_id", None)
    if not user:
        return
    if await is_user_admin(client, chat_id, user.id):
        log.info(f"User {user.id} is admin — skipping mute.")
        return
    new_count = increment_violation(chat_id, user.id)
    log.info(f"Violation count for user {user.id} in {chat_id}: {new_count}")
    if new_count <= NSFW_STICKER_LIMIT:
        return
    if not await bot_can_restrict_members(client, chat_id):
        log.warning("Bot lacks restrict permission; cannot mute.")
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
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 Bot Logs / Updates", url=LOG_PUBLIC_URL)]])
            reply_msg = None
            try:
                reply_msg = await message.reply_text(f"🚫 {user.mention} muted for repeated NSFW content (>{NSFW_STICKER_LIMIT}).", reply_markup=kb)
            except Exception:
                reply_msg = await safe_send_message(client, chat_id, f"🚫 {user.mention} muted for repeated NSFW content (>{NSFW_STICKER_LIMIT}).", reply_markup=kb, thread_id=thread_id)
            if reply_msg:
                asyncio.create_task(schedule_delete(reply_msg, DELETE_LOG_MESSAGE_SECONDS))
        except Exception:
            pass
        await notify_mute_to_log(client, chat_id, user, new_count, score, reason, thread_id=thread_id)
    except Exception as e:
        log.warning(f"Failed to mute user: {e}")

# Callback query handler (pages + confirm flows + mod actions)
@app.on_callback_query()
async def callback_handler(client: Client, query: CallbackQuery):
    data = query.data or ""
    user = query.from_user

    if data == "page_main":
        me = await client.get_me()
        bot_username = me.username or "DLKNSFWBot"
        await edit_main_message(query.message, get_main_text(), build_main_keyboard(bot_username))
        await query.answer()
        return
    if data == "page_how":
        me = await client.get_me()
        bot_username = me.username or "DLKNSFWBot"
        await edit_main_message(query.message, get_how_text(), build_subpage_keyboard(bot_username))
        await query.answer()
        return
    if data == "page_features":
        me = await client.get_me()
        bot_username = me.username or "DLKNSFWBot"
        await edit_main_message(query.message, get_features_text(), build_subpage_keyboard(bot_username))
        await query.answer()
        return
    if data == "page_perms":
        me = await client.get_me()
        bot_username = me.username or "DLKNSFWBot"
        await edit_main_message(query.message, get_perms_text(), build_subpage_keyboard(bot_username))
        await query.answer()
        return
    if data == "page_about":
        me = await client.get_me()
        bot_username = me.username or "DLKNSFWBot"
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
        try:
            await safe_send_message(client, chat_id, f"✅ Sticker whitelisted in chat <code>{chat_id}</code> by admin {user.mention}.")
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
            await safe_send_message(client, chat_id, f"✅ Pack <code>{set_name}</code> un-blacklisted for chat <code>{chat_id}</code> by {user.mention}.")
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

    m = re.match(r"^mod_action:(unmute|ban):(-?\d+):(\d+)$", data)
    if m:
        action = m.group(1)
        target_chat = int(m.group(2))
        target_user = int(m.group(3))
        if not target_chat or not target_user:
            await query.answer("Invalid target.", show_alert=True)
            return
        caller_id = user.id
        allowed = (caller_id in OWNER_IDS)
        if not allowed:
            try:
                allowed = await is_user_admin(client, target_chat, caller_id)
            except Exception:
                allowed = False
        if not allowed:
            await query.answer("You don't have permission to perform this action.", show_alert=True)
            return
        if not await bot_can_restrict_members(client, target_chat):
            await query.answer("Bot lacks restrict/ban permissions in target chat.", show_alert=True)
            return
        if action == "unmute":
            permissions = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_change_info=False, can_invite_users=True, can_pin_messages=False)
            try:
                await client.restrict_chat_member(target_chat, target_user, permissions=permissions)
                try:
                    await query.edit_message_text(f"🔈 User <a href='tg://user?id={target_user}'>user</a> unmuted in chat <code>{target_chat}</code>.", parse_mode="html")
                except Exception:
                    pass
                await query.answer("User unmuted.")
            except Exception as e:
                log.warning(f"unmute failed: {e}")
                try:
                    await client.restrict_chat_member(target_chat, target_user, permissions=permissions, until_date=0)
                    await query.answer("User unmuted.")
                except Exception as e2:
                    await query.answer(f"Failed to unmute: {e2}", show_alert=True)
            return
        elif action == "ban":
            try:
                await client.ban_chat_member(target_chat, target_user)
                try:
                    await query.edit_message_text(f"⛔ User <a href='tg://user?id={target_user}'>user</a> banned from chat <code>{target_chat}</code>.", parse_mode="html")
                except Exception:
                    pass
                await query.answer("User banned.")
            except Exception as e:
                await query.answer(f"Failed to ban: {e}", show_alert=True)
            return

    await query.answer()

# Small helpers for page keyboards (reused)
def build_subpage_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="page_main"), InlineKeyboardButton("➕ Add to group", url=f"https://t.me/{bot_username}?startgroup=nsfw_guard")],
        [InlineKeyboardButton("📖 How to use", callback_data="page_how"), InlineKeyboardButton("🛠 Features", callback_data="page_features")],
        [InlineKeyboardButton("🔐 Permissions", callback_data="page_perms"), InlineKeyboardButton("ℹ️ About", callback_data="page_about")],
        [InlineKeyboardButton("📢 Updates & Logs", url=LOG_PUBLIC_URL)]
    ])

# admin permission helpers
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
    except Exception:
        return False

# Start
if __name__ == "__main__":
    log.info("Starting DLK NSFW Cleaner - FULL")
    # Background init of optional detectors
    try:
        # nudenet immediate init (fast)
        get_detector()
        # mediapipe init in background
        loop = asyncio.get_event_loop()
        try:
            loop.run_in_executor(None, init_mediapipe_pose)
        except Exception:
            pass
        # CLIP init in background
        if CLIP is not None:
            try:
                loop.run_in_executor(None, init_clip)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"Background init warning: {e}")
    app.run()
