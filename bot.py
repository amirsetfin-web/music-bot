# -*- coding: utf-8 -*-
"""
بات موزیک تلگرام - نسخه‌ی کامل با منوی ادمین
------------------------------------------------
همه‌ی قابلیت‌های تنظیمی (کانال‌ها، تبلیغات، پست تبلیغاتی) فقط از طریق منوی /admin
و دکمه‌های شیشه‌ای در دسترسه. دستورات /newsong ، /editsong <کد> ، /mysongs و /ads
هم مستقیم قابل استفاده‌ان.

قابلیت‌ها:
  - افزودن/ویرایش آهنگ (عنوان، خواننده، کاور، بیوگرافی) با ویرایش تگ mp3
  - لینک اختصاصی هر آهنگ + بازدید + لایک/دیس‌لایک + کامنت عمومی
  - عضویت اجباری در کانال/گروه(های) دلخواه (واقعی، با getChatMember)
  - درخواست افتخاری ری‌اکشن روی ۶ پست آخر یه کانال (غیرقابل تأیید فنی، فقط یادآوری)
  - تبلیغات: نمایش قیمت + لینک پرداخت -> کاربر بنر می‌فرسته -> پیام «در حال بررسی توسط
    پشتیبانی، ۲۴ تا ۴۸ ساعت» -> ادمین تأیید/رد می‌کنه -> در صورت تأیید برای همه برودکست می‌شه
  - تبلیغ رایگان مستقیم توسط ادمین
  - پست خودکار «آهنگ + ویدیو» در یک کانال مشخص، با امکان برش یه تکه از آهنگ (نیاز به ffmpeg)
  - منوی کامل ادمین با /admin
"""

import asyncio
import logging
import os
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime

from dotenv import load_dotenv
from mutagen.id3 import ID3, TIT2, TPE1, APIC, COMM
from mutagen.mp3 import MP3
from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# تنظیمات اولیه
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# روی سرور (مثل Railway) می‌تونی DATA_DIR رو به مسیر یه Volume دائمی ست کنی
# تا اطلاعات (آهنگ‌ها، دیتابیس) با هر بار ری‌استارت/آپدیت پاک نشه.
DATA_DIR = os.getenv("DATA_DIR", BASE_DIR)
AUDIO_DIR = os.path.join(DATA_DIR, "media", "audio")
COVER_DIR = os.path.join(DATA_DIR, "media", "covers")
AD_DIR = os.path.join(DATA_DIR, "media", "ads")
PROMO_DIR = os.path.join(DATA_DIR, "media", "promo")
DB_PATH = os.path.join(DATA_DIR, "songs.db")

for d in (AUDIO_DIR, COVER_DIR, AD_DIR, PROMO_DIR):
    os.makedirs(d, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None

# وضعیت‌های مکالمه‌ی افزودن/ویرایش آهنگ
ASK_AUDIO, ASK_TITLE, ASK_ARTIST, ASK_COVER, ASK_CAPTION = range(5)
# وضعیت‌های مکالمه‌ی تبلیغ رایگان ادمین
FREEAD_PHOTO, FREEAD_CAPTION = range(10, 12)


# ---------------------------------------------------------------------------
# دیتابیس
# ---------------------------------------------------------------------------
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            audio_path TEXT NOT NULL,
            cover_path TEXT,
            title TEXT,
            artist TEXT,
            caption TEXT,
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            dislikes INTEGER DEFAULT 0,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_seen TEXT
        );

        CREATE TABLE IF NOT EXISTS votes (
            song_code TEXT,
            user_id INTEGER,
            vote TEXT CHECK(vote IN ('like','dislike')),
            PRIMARY KEY (song_code, user_id)
        );

        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            song_code TEXT,
            user_id INTEGER,
            display_name TEXT,
            text TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS force_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_ref TEXT UNIQUE,
            title TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            photo_path TEXT,
            caption TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS channel_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT,
            message_id INTEGER,
            created_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = db_connect()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db_connect()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def touch_user(user_id: int):
    conn = db_connect()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
        (user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def all_user_ids():
    conn = db_connect()
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def count_users():
    conn = db_connect()
    n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    conn.close()
    return n


def count_pending_ads():
    conn = db_connect()
    n = conn.execute("SELECT COUNT(*) c FROM ads WHERE status = 'pending'").fetchone()["c"]
    conn.close()
    return n


# --- آهنگ‌ها ----------------------------------------------------------------
def db_insert_song(code, audio_path, cover_path, title, artist, caption):
    conn = db_connect()
    conn.execute(
        """INSERT INTO songs (code, audio_path, cover_path, title, artist, caption, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (code, audio_path, cover_path, title, artist, caption, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def db_update_song(code, cover_path, title, artist, caption):
    conn = db_connect()
    conn.execute(
        "UPDATE songs SET cover_path = ?, title = ?, artist = ?, caption = ? WHERE code = ?",
        (cover_path, title, artist, caption, code),
    )
    conn.commit()
    conn.close()


def db_get_song(code):
    conn = db_connect()
    row = conn.execute("SELECT * FROM songs WHERE code = ?", (code,)).fetchone()
    conn.close()
    return row


def db_list_songs(limit=None):
    conn = db_connect()
    q = "SELECT code, title, artist, views, likes, dislikes FROM songs ORDER BY id DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    conn.close()
    return rows


def db_count_songs():
    conn = db_connect()
    n = conn.execute("SELECT COUNT(*) c FROM songs").fetchone()["c"]
    conn.close()
    return n


def db_increment_view(code):
    conn = db_connect()
    conn.execute("UPDATE songs SET views = views + 1 WHERE code = ?", (code,))
    conn.commit()
    conn.close()


def db_vote(code, user_id, vote):
    conn = db_connect()
    existing = conn.execute(
        "SELECT vote FROM votes WHERE song_code = ? AND user_id = ?", (code, user_id)
    ).fetchone()

    if existing and existing["vote"] == vote:
        conn.execute("DELETE FROM votes WHERE song_code = ? AND user_id = ?", (code, user_id))
        conn.execute(f"UPDATE songs SET {vote}s = {vote}s - 1 WHERE code = ?", (code,))
    elif existing:
        old_vote = existing["vote"]
        conn.execute(
            "UPDATE votes SET vote = ? WHERE song_code = ? AND user_id = ?", (vote, code, user_id)
        )
        conn.execute(f"UPDATE songs SET {old_vote}s = {old_vote}s - 1 WHERE code = ?", (code,))
        conn.execute(f"UPDATE songs SET {vote}s = {vote}s + 1 WHERE code = ?", (code,))
    else:
        conn.execute(
            "INSERT INTO votes (song_code, user_id, vote) VALUES (?, ?, ?)", (code, user_id, vote)
        )
        conn.execute(f"UPDATE songs SET {vote}s = {vote}s + 1 WHERE code = ?", (code,))

    conn.commit()
    conn.close()


def db_add_comment(code, user_id, display_name, text):
    conn = db_connect()
    conn.execute(
        "INSERT INTO comments (song_code, user_id, display_name, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (code, user_id, display_name, text, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def db_get_comments(code, limit=15):
    conn = db_connect()
    rows = conn.execute(
        "SELECT display_name, text FROM comments WHERE song_code = ? ORDER BY id DESC LIMIT ?",
        (code, limit),
    ).fetchall()
    conn.close()
    return rows


# --- کانال‌های عضویت اجباری --------------------------------------------------
def db_add_force_channel(chat_ref, title):
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO force_channels (chat_ref, title) VALUES (?, ?)", (chat_ref, title)
    )
    conn.commit()
    conn.close()


def db_remove_force_channel(chat_ref):
    conn = db_connect()
    conn.execute("DELETE FROM force_channels WHERE chat_ref = ?", (chat_ref,))
    conn.commit()
    conn.close()


def db_list_force_channels():
    conn = db_connect()
    rows = conn.execute("SELECT chat_ref, title FROM force_channels").fetchall()
    conn.close()
    return rows


# --- پست‌های کانال ری‌اکشن ----------------------------------------------------
def db_log_channel_post(channel, message_id):
    conn = db_connect()
    conn.execute(
        "INSERT INTO channel_posts (channel, message_id, created_at) VALUES (?, ?, ?)",
        (channel, message_id, datetime.utcnow().isoformat()),
    )
    conn.execute(
        """DELETE FROM channel_posts WHERE channel = ? AND id NOT IN (
               SELECT id FROM channel_posts WHERE channel = ? ORDER BY id DESC LIMIT 20
           )""",
        (channel, channel),
    )
    conn.commit()
    conn.close()


def db_get_latest_posts(channel, limit=6):
    conn = db_connect()
    rows = conn.execute(
        "SELECT message_id FROM channel_posts WHERE channel = ? ORDER BY id DESC LIMIT ?",
        (channel, limit),
    ).fetchall()
    conn.close()
    return [r["message_id"] for r in rows]


# --- تبلیغات -----------------------------------------------------------------
def db_insert_ad(user_id, photo_path, caption):
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO ads (user_id, photo_path, caption, created_at) VALUES (?, ?, ?, ?)",
        (user_id, photo_path, caption, datetime.utcnow().isoformat()),
    )
    conn.commit()
    ad_id = cur.lastrowid
    conn.close()
    return ad_id


def db_get_ad(ad_id):
    conn = db_connect()
    row = conn.execute("SELECT * FROM ads WHERE id = ?", (ad_id,)).fetchone()
    conn.close()
    return row


def db_set_ad_status(ad_id, status):
    conn = db_connect()
    conn.execute("UPDATE ads SET status = ? WHERE id = ?", (status, ad_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# کمکی‌های عمومی
# ---------------------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def normalize_channel_ref(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("https://t.me/"):
        raw = raw.replace("https://t.me/", "")
    return raw.lstrip("@")


async def reply_to(update: Update, text: str, **kwargs):
    """هم برای پیام معمولی و هم برای کلیک روی دکمه‌ی شیشه‌ای کار می‌کنه."""
    if update.message:
        await update.message.reply_text(text, **kwargs)
    else:
        await update.callback_query.message.reply_text(text, **kwargs)


def edit_audio_tags(audio_path: str, title: str, artist: str, caption: str, cover_path):
    try:
        audio = MP3(audio_path, ID3=ID3)
    except Exception as e:
        logger.warning("نمی‌توان فایل را به‌عنوان mp3 باز کرد: %s", e)
        return False

    try:
        audio.add_tags()
    except Exception:
        pass

    if title:
        audio.tags.add(TIT2(encoding=3, text=title))
    if artist:
        audio.tags.add(TPE1(encoding=3, text=artist))
    if caption:
        audio.tags.add(COMM(encoding=3, lang="fas", desc="desc", text=caption))

    if cover_path and os.path.exists(cover_path):
        with open(cover_path, "rb") as img:
            audio.tags.add(
                APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img.read())
            )

    audio.save(v2_version=3)
    return True


def trim_audio(src_path: str, start_sec: int, duration_sec: int, dst_path: str) -> bool:
    """یه تکه از فایل صوتی رو با ffmpeg می‌بره. اگه ffmpeg نبود یا خطا داد، False برمی‌گردونه."""
    if not FFMPEG_AVAILABLE:
        return False
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", src_path,
                "-ss", str(start_sec),
                "-t", str(duration_sec),
                "-acodec", "copy",
                dst_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return os.path.exists(dst_path) and os.path.getsize(dst_path) > 0
    except Exception as e:
        logger.warning("خطا در برش فایل صوتی: %s", e)
        return False


async def is_member_of_all_required(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    missing = []
    for row in db_list_force_channels():
        ref = row["chat_ref"]
        chat_id = f"@{ref}"
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ("left", "kicked"):
                missing.append(row)
        except TelegramError as e:
            logger.warning("خطا در بررسی عضویت %s: %s", ref, e)
            missing.append(row)
    return missing


def song_keyboard(code):
    row = db_get_song(code)
    likes = row["likes"] if row else 0
    dislikes = row["dislikes"] if row else 0
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"👍 {likes}", callback_data=f"like|{code}"),
                InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislike|{code}"),
            ],
            [InlineKeyboardButton("💬 نظرات", callback_data=f"cmtmenu|{code}")],
        ]
    )


async def deliver_song(bot, chat_id, code):
    row = db_get_song(code)
    if not row or not os.path.exists(row["audio_path"]):
        await bot.send_message(chat_id, "فایل آهنگ پیدا نشد.")
        return

    db_increment_view(code)
    row = db_get_song(code)

    caption_lines = []
    if row["caption"]:
        caption_lines.append(row["caption"])
    caption_lines.append(f"👁 بازدید: {row['views']}")
    caption = "\n\n".join(caption_lines)

    with open(row["audio_path"], "rb") as f:
        await bot.send_audio(
            chat_id,
            audio=InputFile(f, filename=f"{row['title'] or 'song'}.mp3"),
            title=row["title"] or None,
            performer=row["artist"] or None,
            caption=caption,
            reply_markup=song_keyboard(code),
        )


async def send_join_prompt(bot, chat_id, code, missing_channels):
    buttons = [
        [InlineKeyboardButton(f"عضویت در {c['title'] or c['chat_ref']}", url=f"https://t.me/{c['chat_ref']}")]
        for c in missing_channels
    ]
    buttons.append([InlineKeyboardButton("✅ عضو شدم، بررسی کن", callback_data=f"checkjoin|{code}")])
    await bot.send_message(
        chat_id,
        "برای دریافت آهنگ، اول باید عضو کانال/گروه(های) زیر بشی:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def send_reaction_prompt(bot, chat_id, code, channel, post_ids):
    lines = ["🙏 لطفاً قبل از دریافت آهنگ، روی چند پست آخر کانال زیر ری‌اکشن بزن:"]
    for mid in post_ids:
        lines.append(f"https://t.me/{channel}/{mid}")
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ ری‌اکشن دادم، آهنگو بده", callback_data=f"reactok|{code}")]]
    )
    await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


async def process_song_request(update_or_bot, chat_id, user_id, code, context):
    bot = context.bot
    row = db_get_song(code)
    if not row:
        await bot.send_message(chat_id, "این لینک معتبر نیست یا آهنگ حذف شده.")
        return

    missing = await is_member_of_all_required(context, user_id)
    if missing:
        await send_join_prompt(bot, chat_id, code, missing)
        return

    reaction_channel = get_setting("reaction_channel")
    reacted_set = context.user_data.setdefault("reacted_songs", set())
    if reaction_channel and code not in reacted_set:
        posts = db_get_latest_posts(reaction_channel, 6)
        if posts:
            await send_reaction_prompt(bot, chat_id, code, reaction_channel, posts)
            return

    await deliver_song(bot, chat_id, code)


async def broadcast_photo(bot, photo_path, caption):
    sent, failed = 0, 0
    for uid in all_user_ids():
        try:
            with open(photo_path, "rb") as f:
                await bot.send_photo(uid, photo=InputFile(f), caption=caption)
            sent += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


# ---------------------------------------------------------------------------
# افزودن / ویرایش آهنگ (مکالمه) - قابل شروع با دستور یا با دکمه‌ی منو
# ---------------------------------------------------------------------------
async def newsong_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not is_admin(update.effective_user.id):
        await reply_to(update, "⛔️ فقط ادمین می‌تواند آهنگ اضافه کند.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["mode"] = "new"
    await reply_to(update, "🎵 فایل آهنگ (mp3) رو بفرست.\nبرای لغو /cancel بزن.")
    return ASK_AUDIO


async def editsong_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ فقط ادمین می‌تواند آهنگ را ویرایش کند.")
        return ConversationHandler.END
    if not context.args:
        await update.message.reply_text("فرمت درست: /editsong <کد آهنگ>\n(یا از منوی /admin استفاده کن)")
        return ConversationHandler.END
    return await _start_edit_flow(update, context, context.args[0])


async def editsong_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    code = query.data.split("|", 1)[1]
    return await _start_edit_flow(update, context, code)


async def _start_edit_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    row = db_get_song(code)
    if not row:
        await reply_to(update, "همچین آهنگی با این کد پیدا نشد.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data.update(
        {
            "mode": "edit",
            "code": code,
            "audio_path": row["audio_path"],
            "old_title": row["title"],
            "old_artist": row["artist"],
            "old_caption": row["caption"],
            "old_cover": row["cover_path"],
        }
    )
    await reply_to(update, f"در حال ویرایش «{row['title']}».\nعنوان جدید رو بفرست یا /skip بزن.")
    return ASK_TITLE


async def receive_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_obj = update.message.audio or (
        update.message.document
        if update.message.document and (update.message.document.mime_type or "").startswith("audio")
        else None
    )
    if not file_obj:
        await update.message.reply_text("این یه فایل صوتی نبود. یه mp3 بفرست یا /cancel بزن.")
        return ASK_AUDIO

    tg_file = await context.bot.get_file(file_obj.file_id)
    code = uuid.uuid4().hex[:8]
    local_path = os.path.join(AUDIO_DIR, f"{code}.mp3")
    await tg_file.download_to_drive(local_path)

    context.user_data["code"] = code
    context.user_data["audio_path"] = local_path
    await update.message.reply_text("✅ فایل دریافت شد.\nحالا عنوان آهنگ رو بفرست:")
    return ASK_TITLE


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/skip" and context.user_data.get("mode") == "edit":
        title = context.user_data.get("old_title")
    else:
        title = update.message.text.strip()
    context.user_data["title"] = title
    hint = f" (فعلی: {context.user_data.get('old_artist')})" if context.user_data.get("mode") == "edit" else ""
    await update.message.reply_text(f"اسم خواننده رو بفرست{hint}، یا /skip:")
    return ASK_ARTIST


async def receive_artist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/skip" and context.user_data.get("mode") == "edit":
        artist = context.user_data.get("old_artist")
    else:
        artist = update.message.text.strip()
    context.user_data["artist"] = artist
    await update.message.reply_text("حالا عکس کاور رو بفرست (به‌صورت عکس)، یا /skip:")
    return ASK_COVER


async def receive_cover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/skip":
        cover_path = context.user_data.get("old_cover")
    elif update.message.photo:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        code = context.user_data["code"]
        cover_path = os.path.join(COVER_DIR, f"{code}.jpg")
        await tg_file.download_to_drive(cover_path)
    else:
        await update.message.reply_text("لطفاً یه عکس بفرست یا /skip بزن.")
        return ASK_COVER

    context.user_data["cover_path"] = cover_path
    await update.message.reply_text("در آخر، بیوگرافی/توضیح آهنگ رو بنویس، یا /skip:")
    return ASK_CAPTION


async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "/skip" and context.user_data.get("mode") == "edit":
        caption = context.user_data.get("old_caption")
    elif update.message.text == "/skip":
        caption = ""
    else:
        caption = update.message.text.strip()
    context.user_data["caption"] = caption
    return await finalize_song(update, context)


async def finalize_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    code = data["code"]
    audio_path = data["audio_path"]
    title = data.get("title") or ""
    artist = data.get("artist") or ""
    caption = data.get("caption") or ""
    cover_path = data.get("cover_path")

    ok = edit_audio_tags(audio_path, title, artist, caption, cover_path)

    if data.get("mode") == "edit":
        db_update_song(code, cover_path, title, artist, caption)
    else:
        db_insert_song(code, audio_path, cover_path, title, artist, caption)

    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={code}"
    tag_note = "" if ok else "\n⚠️ فایل mp3 نبود، تگ‌ها ویرایش نشدن ولی موقع ارسال عنوان/بیوگرافی نمایش داده می‌شه."

    await update.message.reply_text(
        f"🎉 آماده شد!\n\n"
        f"🎵 عنوان: {title}\n🎤 خواننده: {artist}\n📝 بیوگرافی: {caption or '—'}\n\n"
        f"🔗 لینک:\n{link}\n\nکد آهنگ: {code}{tag_note}"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END


async def cancel_generic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لغو هر جریان دستی (منوی ادمین) که ConversationHandler نیست."""
    if context.user_data.get("awaiting"):
        context.user_data.clear()
        await update.message.reply_text("لغو شد.")


async def mysongs_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await reply_to(update, songs_list_text())


def songs_list_text():
    rows = db_list_songs()
    if not rows:
        return "هنوز آهنگی اضافه نشده."
    return "\n".join(
        f"• {r['code']} — {r['title'] or '(بی‌نام)'} / {r['artist'] or '-'} "
        f"| 👁{r['views']} 👍{r['likes']} 👎{r['dislikes']}"
        for r in rows
    )


# ---------------------------------------------------------------------------
# /start و تحویل آهنگ
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    touch_user(user_id)

    if context.args:
        code = context.args[0]
        await process_song_request(update, update.effective_chat.id, user_id, code, context)
        return

    if is_admin(user_id):
        await update.message.reply_text(
            "سلام ادمین 👋\nبرای مدیریت بات، از منوی کامل استفاده کن:\n/admin"
        )
    else:
        await update.message.reply_text("سلام! برای دریافت آهنگ باید از لینک مخصوص همون آهنگ وارد بشی.")


# ---------------------------------------------------------------------------
# منوی ادمین
# ---------------------------------------------------------------------------
def main_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎵 افزودن آهنگ", callback_data="menu_newsong"),
             InlineKeyboardButton("✏️ ویرایش آهنگ", callback_data="menu_editsong")],
            [InlineKeyboardButton("📃 لیست آهنگ‌ها", callback_data="menu_mysongs")],
            [InlineKeyboardButton("🔒 کانال‌های اجباری", callback_data="menu_forcechannels")],
            [InlineKeyboardButton("❤️ کانال ری‌اکشن", callback_data="menu_reactionchannel")],
            [InlineKeyboardButton("📢 تنظیمات تبلیغات", callback_data="menu_adsettings"),
             InlineKeyboardButton("🎁 تبلیغ رایگان", callback_data="menu_freead")],
            [InlineKeyboardButton("🎬 پست آهنگ+ویدیو در کانال", callback_data="menu_promopost")],
            [InlineKeyboardButton("📌 تنظیم کانال پست", callback_data="menu_setpromochannel")],
            [InlineKeyboardButton("📊 آمار سریع", callback_data="menu_stats")],
        ]
    )


async def admin_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🛠 منوی مدیریت بات:", reply_markup=main_menu_keyboard())


async def show_main_menu(query):
    await query.message.reply_text("🛠 منوی مدیریت بات:", reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# تبلیغات
# ---------------------------------------------------------------------------
async def ads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = get_setting("ad_price", "هنوز قیمتی توسط ادمین تنظیم نشده")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 دریافت لینک پرداخت", callback_data="adpay")]])
    await update.message.reply_text(f"💰 قیمت تبلیغات: {price}\n\nبرای دریافت لینک پرداخت روی دکمه بزن 👇", reply_markup=kb)


async def handle_incoming_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عکسی که کاربر به‌عنوان بنر تبلیغ بعد از ادعای پرداخت می‌فرسته."""
    if not context.user_data.get("awaiting_ad_banner"):
        return

    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    ad_uid = uuid.uuid4().hex[:8]
    local_path = os.path.join(AD_DIR, f"{ad_uid}.jpg")
    await tg_file.download_to_drive(local_path)

    caption = update.message.caption or ""
    ad_id = db_insert_ad(update.effective_user.id, local_path, caption)
    context.user_data["awaiting_ad_banner"] = False

    await update.message.reply_text(
        "✅ درخواست شما ثبت شد.\n"
        "تیم پشتیبانی درخواست شما رو بررسی می‌کنه؛ این کار معمولاً بین ۲۴ تا ۴۸ ساعت طول می‌کشه.\n"
        "بعد از تأیید، بنر برای همه‌ی کاربران بات ارسال می‌شه. 🙏"
    )

    if ADMIN_ID:
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ تأیید و ارسال به همه", callback_data=f"adapprove|{ad_id}"),
                    InlineKeyboardButton("❌ رد (چرت)", callback_data=f"adreject|{ad_id}"),
                ]
            ]
        )
        with open(local_path, "rb") as f:
            await context.bot.send_photo(
                ADMIN_ID,
                photo=InputFile(f),
                caption=f"تبلیغ جدید از کاربر {update.effective_user.id}:\n{caption}",
                reply_markup=kb,
            )


# --- تبلیغ رایگان ادمین (مکالمه) --------------------------------------------
async def freead_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    context.user_data.clear()
    await reply_to(update, "📸 عکس بنر تبلیغ رایگان رو بفرست:")
    return FREEAD_PHOTO


async def freead_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("لطفاً یه عکس بفرست یا /cancel بزن.")
        return FREEAD_PHOTO
    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    local_path = os.path.join(AD_DIR, f"free_{uuid.uuid4().hex[:8]}.jpg")
    await tg_file.download_to_drive(local_path)
    context.user_data["freead_photo"] = local_path
    await update.message.reply_text("متن تبلیغ رو بفرست، یا /skip:")
    return FREEAD_CAPTION


async def freead_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = "" if update.message.text == "/skip" else update.message.text.strip()
    context.user_data["freead_caption"] = caption

    await update.message.reply_text("⏳ در حال ارسال برای همه‌ی کاربران... این ممکنه کمی طول بکشه.")
    sent, failed = await broadcast_photo(context.bot, context.user_data["freead_photo"], caption)
    await update.message.reply_text(f"✅ ارسال شد به {sent} نفر. ({failed} ناموفق/بلاک)")
    context.user_data.clear()
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# پست تبلیغاتیِ «آهنگ + ویدیو» در کانال
# ---------------------------------------------------------------------------
async def start_promopost(query, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["awaiting"] = "promo_song"
    context.user_data["promo"] = {}
    await query.message.reply_text(
        "🎵 آهنگ رو بفرست (فایل mp3)، یا کدِ یه آهنگ قبلی رو تایپ کن (با /mysongs کدها رو ببین).\n"
        "برای لغو /cancel بزن."
    )


async def handle_incoming_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """آهنگی که برای مرحله‌ی «پست تبلیغاتی» فرستاده می‌شه (خارج از مکالمه‌ی newsong)."""
    if context.user_data.get("awaiting") != "promo_song" or not is_admin(update.effective_user.id):
        return
    file_obj = update.message.audio or (
        update.message.document
        if update.message.document and (update.message.document.mime_type or "").startswith("audio")
        else None
    )
    if not file_obj:
        return

    tg_file = await context.bot.get_file(file_obj.file_id)
    local_path = os.path.join(PROMO_DIR, f"song_{uuid.uuid4().hex[:8]}.mp3")
    await tg_file.download_to_drive(local_path)

    context.user_data["promo"]["audio_path"] = local_path
    context.user_data["promo"]["title"] = None
    context.user_data["promo"]["artist"] = None
    context.user_data["awaiting"] = "promo_song_caption"
    await update.message.reply_text("متن پیام زیر آهنگ رو بفرست، یا /skip:")


async def handle_incoming_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting") != "promo_video" or not is_admin(update.effective_user.id):
        return
    video = update.message.video
    if not video:
        return
    tg_file = await context.bot.get_file(video.file_id)
    local_path = os.path.join(PROMO_DIR, f"video_{uuid.uuid4().hex[:8]}.mp4")
    await tg_file.download_to_drive(local_path)

    context.user_data["promo"]["video_path"] = local_path
    context.user_data["awaiting"] = "promo_video_caption"
    await update.message.reply_text("متن پیام زیر ویدیو رو بفرست، یا /skip:")


async def finalize_promo_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    promo = context.user_data.get("promo", {})
    promo_channel = get_setting("promo_channel")

    if not promo_channel:
        await update.message.reply_text(
            "⚠️ کانال پست تنظیم نشده. اول از منو، «تنظیم کانال پست» رو بزن."
        )
        context.user_data.clear()
        return

    chat_ref = f"@{promo_channel}"
    audio_path = promo.get("audio_path")
    trimmed_path = promo.get("trimmed_path")
    final_audio = trimmed_path if trimmed_path and os.path.exists(trimmed_path) else audio_path

    try:
        if final_audio and os.path.exists(final_audio):
            with open(final_audio, "rb") as f:
                await context.bot.send_audio(
                    chat_ref,
                    audio=InputFile(f),
                    title=promo.get("title"),
                    performer=promo.get("artist"),
                    caption=promo.get("song_caption") or None,
                )
        video_path = promo.get("video_path")
        if video_path and os.path.exists(video_path):
            with open(video_path, "rb") as f:
                await context.bot.send_video(
                    chat_ref,
                    video=InputFile(f),
                    caption=promo.get("video_caption") or None,
                )
        await update.message.reply_text(f"🎉 با موفقیت توی @{promo_channel} پست شد!")
    except TelegramError as e:
        await update.message.reply_text(
            f"❌ ارسال به کانال با خطا مواجه شد: {e}\n"
            "مطمئن شو بات ادمین کانال هست و یوزرنیم درسته."
        )

    context.user_data.clear()


# ---------------------------------------------------------------------------
# مسیریابیِ ورودی‌های متنی و رسانه‌ایِ جریان‌های دستیِ منوی ادمین
# ---------------------------------------------------------------------------
async def handle_generic_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    awaiting = context.user_data.get("awaiting")

    # --- کامنت زیر آهنگ (برای همه‌ی کاربران) ---
    comment_code = context.user_data.get("awaiting_comment_for")
    if comment_code and not awaiting:
        user = update.effective_user
        display_name = user.first_name or user.username or "کاربر"
        db_add_comment(comment_code, user.id, display_name, text)
        context.user_data["awaiting_comment_for"] = None
        await update.message.reply_text("✅ کامنتت ثبت شد.")
        return

    if not awaiting or not is_admin(update.effective_user.id):
        return

    # --- افزودن کانال اجباری ---
    if awaiting == "force_channel_ref":
        context.user_data["new_channel_ref"] = normalize_channel_ref(text)
        context.user_data["awaiting"] = "force_channel_title"
        await update.message.reply_text("یه عنوان دلخواه برای این کانال بفرست (مثلاً: کانال اصلی):")
        return

    if awaiting == "force_channel_title":
        ref = context.user_data.pop("new_channel_ref")
        db_add_force_channel(ref, text)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            f"✅ اضافه شد: @{ref}\n⚠️ یادت نره بات رو ادمین اون کانال/گروه کن تا بتونه عضویت رو چک کنه."
        )
        return

    # --- کانال ری‌اکشن ---
    if awaiting == "reaction_channel":
        ref = normalize_channel_ref(text)
        set_setting("reaction_channel", ref)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            f"✅ کانال ری‌اکشن تنظیم شد: @{ref}\n⚠️ بات باید عضو اون کانال باشه تا پست‌های جدید رو ثبت کنه."
        )
        return

    # --- کانال پست تبلیغاتی ---
    if awaiting == "promo_channel":
        ref = normalize_channel_ref(text)
        set_setting("promo_channel", ref)
        context.user_data["awaiting"] = None
        await update.message.reply_text(f"✅ کانال پست تنظیم شد: @{ref}")
        return

    # --- قیمت تبلیغات ---
    if awaiting == "ad_price":
        set_setting("ad_price", text)
        context.user_data["awaiting"] = None
        await update.message.reply_text("✅ قیمت تبلیغات ثبت شد.")
        return

    # --- لینک پرداخت ---
    if awaiting == "pay_link":
        set_setting("pay_link", text)
        context.user_data["awaiting"] = None
        await update.message.reply_text("✅ لینک پرداخت ثبت شد.")
        return

    # --- جریان پست تبلیغاتی آهنگ+ویدیو ---
    if awaiting == "promo_song":
        # کاربر به‌جای فایل، کدِ یه آهنگ قبلی رو تایپ کرده
        row = db_get_song(text)
        if not row:
            await update.message.reply_text("کد آهنگ پیدا نشد. یه فایل mp3 بفرست یا کدِ درست رو بنویس.")
            return
        context.user_data["promo"]["audio_path"] = row["audio_path"]
        context.user_data["promo"]["title"] = row["title"]
        context.user_data["promo"]["artist"] = row["artist"]
        context.user_data["awaiting"] = "promo_song_caption"
        await update.message.reply_text("متن پیام زیر آهنگ رو بفرست، یا /skip:")
        return

    if awaiting == "promo_song_caption":
        context.user_data["promo"]["song_caption"] = "" if text == "/skip" else text
        context.user_data["awaiting"] = "promo_trim"
        note = "" if FFMPEG_AVAILABLE else "\n(توجه: ffmpeg روی سرور نصب نیست، پس کل آهنگ فرستاده می‌شه.)"
        await update.message.reply_text(
            "می‌خوای فقط یه تکه از آهنگ فرستاده بشه؟ بنویس مثلاً 10-40 (از ثانیه‌ی ۱۰ تا ۴۰)، "
            f"یا /skip برای کل آهنگ.{note}"
        )
        return

    if awaiting == "promo_trim":
        if text != "/skip" and "-" in text:
            try:
                start_s, end_s = text.split("-")
                start_s, end_s = int(start_s.strip()), int(end_s.strip())
                if end_s > start_s and FFMPEG_AVAILABLE:
                    src = context.user_data["promo"]["audio_path"]
                    dst = os.path.join(PROMO_DIR, f"trim_{uuid.uuid4().hex[:8]}.mp3")
                    if trim_audio(src, start_s, end_s - start_s, dst):
                        context.user_data["promo"]["trimmed_path"] = dst
            except ValueError:
                pass  # فرمت اشتباه بود، از کل آهنگ استفاده می‌شه
        context.user_data["awaiting"] = "promo_video"
        await update.message.reply_text("🎬 حالا فایل ویدیو رو بفرست:")
        return

    if awaiting == "promo_video_caption":
        context.user_data["promo"]["video_caption"] = "" if text == "/skip" else text
        await finalize_promo_post(update, context)
        return


# ---------------------------------------------------------------------------
# دکمه‌های شیشه‌ای (Callback Query)
# ---------------------------------------------------------------------------
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    # موارد زیر خودشون در انتهای ConversationHandler.entry_points پاسخ داده می‌شن
    if data in ("menu_newsong",) or data.startswith("editsong_pick|") or data == "menu_freead":
        return  # این‌ها entry_point های مکالمه هستن، نیازی به answer دوباره نیست

    await query.answer()

    if data == "menu_main":
        await show_main_menu(query)
        return

    if data == "menu_editsong":
        rows = db_list_songs(limit=30)
        if not rows:
            await query.message.reply_text("هنوز آهنگی اضافه نشده.")
            return
        buttons = [[InlineKeyboardButton(f"{r['title'] or r['code']}", callback_data=f"editsong_pick|{r['code']}")] for r in rows]
        buttons.append([InlineKeyboardButton("🔙 برگشت", callback_data="menu_main")])
        await query.message.reply_text("کدوم آهنگ رو می‌خوای ویرایش کنی؟", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "menu_mysongs":
        await query.message.reply_text(songs_list_text())
        return

    if data == "menu_forcechannels":
        rows = db_list_force_channels()
        text = "\n".join(f"@{r['chat_ref']} — {r['title']}" for r in rows) or "چیزی ثبت نشده."
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ افزودن کانال", callback_data="menu_addforcechannel")],
                [InlineKeyboardButton("➖ حذف کانال", callback_data="menu_removeforcechannel")],
                [InlineKeyboardButton("🔙 برگشت", callback_data="menu_main")],
            ]
        )
        await query.message.reply_text(f"کانال‌های اجباری فعلی:\n{text}", reply_markup=kb)
        return

    if data == "menu_addforcechannel":
        context.user_data.clear()
        context.user_data["awaiting"] = "force_channel_ref"
        await query.message.reply_text("یوزرنیم کانال/گروه رو بفرست (مثلاً @mychannel):")
        return

    if data == "menu_removeforcechannel":
        rows = db_list_force_channels()
        if not rows:
            await query.message.reply_text("کانالی برای حذف نیست.")
            return
        buttons = [[InlineKeyboardButton(f"❌ {r['title']} (@{r['chat_ref']})", callback_data=f"rmch|{r['chat_ref']}")] for r in rows]
        buttons.append([InlineKeyboardButton("🔙 برگشت", callback_data="menu_main")])
        await query.message.reply_text("کدوم رو حذف کنم؟", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("rmch|"):
        ref = data.split("|", 1)[1]
        db_remove_force_channel(ref)
        await query.message.reply_text(f"✅ حذف شد: @{ref}")
        return

    if data == "menu_reactionchannel":
        current = get_setting("reaction_channel")
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ تغییر کانال", callback_data="menu_setreactionchannel")],
                [InlineKeyboardButton("🔙 برگشت", callback_data="menu_main")],
            ]
        )
        await query.message.reply_text(
            f"کانال ری‌اکشن فعلی: {'@' + current if current else 'تنظیم نشده'}", reply_markup=kb
        )
        return

    if data == "menu_setreactionchannel":
        context.user_data.clear()
        context.user_data["awaiting"] = "reaction_channel"
        await query.message.reply_text("یوزرنیم کانال ری‌اکشن رو بفرست (مثلاً @mychannel):")
        return

    if data == "menu_setpromochannel":
        context.user_data.clear()
        context.user_data["awaiting"] = "promo_channel"
        current = get_setting("promo_channel")
        note = f"\nفعلی: @{current}" if current else ""
        await query.message.reply_text(f"یوزرنیم کانالِ پست تبلیغاتی رو بفرست (مثلاً @mychannel):{note}")
        return

    if data == "menu_promopost":
        await start_promopost(query, context)
        return

    if data == "menu_adsettings":
        price = get_setting("ad_price", "تنظیم نشده")
        pay_link = get_setting("pay_link", "تنظیم نشده")
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💰 تغییر قیمت", callback_data="menu_setadprice")],
                [InlineKeyboardButton("🔗 تغییر لینک پرداخت", callback_data="menu_setpaylink")],
                [InlineKeyboardButton("🔙 برگشت", callback_data="menu_main")],
            ]
        )
        await query.message.reply_text(f"قیمت فعلی: {price}\nلینک پرداخت: {pay_link}", reply_markup=kb)
        return

    if data == "menu_setadprice":
        context.user_data.clear()
        context.user_data["awaiting"] = "ad_price"
        await query.message.reply_text("قیمت جدید رو بفرست (مثلاً: 50000 تومان):")
        return

    if data == "menu_setpaylink":
        context.user_data.clear()
        context.user_data["awaiting"] = "pay_link"
        await query.message.reply_text("لینک پرداخت جدید رو بفرست:")
        return

    if data == "menu_stats":
        text = (
            f"📊 آمار سریع:\n"
            f"👥 کاربران: {count_users()}\n"
            f"🎵 آهنگ‌ها: {db_count_songs()}\n"
            f"⏳ تبلیغ‌های در انتظار تأیید: {count_pending_ads()}"
        )
        await query.message.reply_text(text)
        return

    # --- تبلیغات ---
    if data == "adpay":
        pay_link = get_setting("pay_link")
        if pay_link:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 رفتن به درگاه پرداخت", url=pay_link)]])
            await query.message.reply_text(
                "بعد از پرداخت، عکس بنر (و در صورت تمایل متن) تبلیغت رو همینجا برام بفرست.",
                reply_markup=kb,
            )
        else:
            await query.message.reply_text("فعلاً لینک پرداختی تنظیم نشده، بعداً امتحان کن.")
        context.user_data["awaiting_ad_banner"] = True
        return

    if data.startswith("adapprove|") or data.startswith("adreject|"):
        if not is_admin(query.from_user.id):
            return
        ad_id = int(data.split("|", 1)[1])
        ad = db_get_ad(ad_id)
        if not ad:
            await query.message.reply_text("این تبلیغ پیدا نشد.")
            return
        if data.startswith("adapprove|"):
            db_set_ad_status(ad_id, "approved")
            await query.message.reply_text("⏳ در حال ارسال به همه...")
            sent, failed = await broadcast_photo(context.bot, ad["photo_path"], ad["caption"])
            await query.message.reply_text(f"✅ ارسال شد به {sent} نفر. ({failed} ناموفق/بلاک)")
        else:
            db_set_ad_status(ad_id, "rejected")
            if os.path.exists(ad["photo_path"]):
                os.remove(ad["photo_path"])
            await query.message.reply_text("❌ رد شد و بنر حذف شد.")
        return

    # --- تحویل آهنگ ---
    if data.startswith("checkjoin|"):
        code = data.split("|", 1)[1]
        await process_song_request(update, query.message.chat_id, query.from_user.id, code, context)
        return

    if data.startswith("reactok|"):
        code = data.split("|", 1)[1]
        context.user_data.setdefault("reacted_songs", set()).add(code)
        await deliver_song(context.bot, query.message.chat_id, code)
        return

    if data.startswith("like|") or data.startswith("dislike|"):
        vote, code = data.split("|", 1)
        db_vote(code, query.from_user.id, vote)
        try:
            await query.edit_message_reply_markup(reply_markup=song_keyboard(code))
        except TelegramError:
            pass
        return

    if data.startswith("cmtmenu|"):
        code = data.split("|", 1)[1]
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✍️ ثبت کامنت", callback_data=f"cmtadd|{code}")],
                [InlineKeyboardButton("👀 دیدن کامنت‌ها", callback_data=f"cmtview|{code}")],
            ]
        )
        await query.message.reply_text("چیکار می‌خوای بکنی؟", reply_markup=kb)
        return

    if data.startswith("cmtadd|"):
        code = data.split("|", 1)[1]
        context.user_data["awaiting_comment_for"] = code
        await query.message.reply_text("متن کامنتت رو بفرست:")
        return

    if data.startswith("cmtview|"):
        code = data.split("|", 1)[1]
        comments = db_get_comments(code)
        if not comments:
            await query.message.reply_text("هنوز کامنتی برای این آهنگ ثبت نشده.")
            return
        text = "\n\n".join(f"👤 {c['display_name']}:\n{c['text']}" for c in comments)
        await query.message.reply_text(text)
        return


async def log_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    reaction_channel = get_setting("reaction_channel")
    if not reaction_channel:
        return
    username = (chat.username or "").lower()
    if username == reaction_channel.lower():
        db_log_channel_post(reaction_channel, update.effective_message.message_id)


# ---------------------------------------------------------------------------
# اجرای بات
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN or not ADMIN_ID:
        raise SystemExit("BOT_TOKEN و ADMIN_ID رو در فایل .env تنظیم کن (نمونه: .env.example).")

    db_init()
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    text_filter = filters.TEXT & ~filters.COMMAND

    song_conv = ConversationHandler(
        entry_points=[
            CommandHandler("newsong", newsong_start),
            CommandHandler("editsong", editsong_start),
            CallbackQueryHandler(newsong_start, pattern="^menu_newsong$"),
            CallbackQueryHandler(editsong_pick_callback, pattern=r"^editsong_pick\|"),
        ],
        states={
            ASK_AUDIO: [MessageHandler(filters.AUDIO | filters.Document.AUDIO, receive_audio)],
            ASK_TITLE: [CommandHandler("skip", receive_title), MessageHandler(text_filter, receive_title)],
            ASK_ARTIST: [CommandHandler("skip", receive_artist), MessageHandler(text_filter, receive_artist)],
            ASK_COVER: [CommandHandler("skip", receive_cover), MessageHandler(filters.PHOTO, receive_cover)],
            ASK_CAPTION: [CommandHandler("skip", receive_caption), MessageHandler(text_filter, receive_caption)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    freead_conv = ConversationHandler(
        entry_points=[
            CommandHandler("freead", freead_start),
            CallbackQueryHandler(freead_start, pattern="^menu_freead$"),
        ],
        states={
            FREEAD_PHOTO: [MessageHandler(filters.PHOTO, freead_photo)],
            FREEAD_CAPTION: [CommandHandler("skip", freead_caption), MessageHandler(text_filter, freead_caption)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_menu_command))
    app.add_handler(CommandHandler("mysongs", mysongs_text))
    app.add_handler(CommandHandler("ads", ads_command))
    app.add_handler(CommandHandler("cancel", cancel_generic))

    app.add_handler(song_conv)
    app.add_handler(freead_conv)

    app.add_handler(CallbackQueryHandler(callback_router))

    # لاگ کردن پست‌های کانالِ ری‌اکشن (باید بات عضو اون کانال باشه)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, log_channel_post))

    # این‌ها باید بعد از ConversationHandler ها باشن تا اولویت با مکالمه‌ی فعال باشه
    app.add_handler(MessageHandler(filters.PHOTO, handle_incoming_photo))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, handle_incoming_audio))
    app.add_handler(MessageHandler(filters.VIDEO, handle_incoming_video))
    app.add_handler(MessageHandler(text_filter, handle_generic_text))

    logger.info("بات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
