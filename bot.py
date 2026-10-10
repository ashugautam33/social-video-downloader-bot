
import os
import re
import base64
import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "45"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("media_bot")


# ============================================================
# URL HELPERS
# ============================================================

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def extract_url(text):
    if not text:
        return None

    match = URL_PATTERN.search(text.strip())
    if not match:
        return None

    url = match.group(0).rstrip(".,!?;:)]}")
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return None

    if not parsed.hostname:
        return None

    return url


def get_host(url):
    return (urlparse(url).hostname or "").lower()


def host_matches(host, domain):
    return host == domain or host.endswith("." + domain)


def get_cookie_environment(host):
    if host_matches(host, "youtube.com") or host_matches(host, "youtu.be"):
        return "YOUTUBE_COOKIES_B64"

    if host_matches(host, "instagram.com"):
        return "INSTAGRAM_COOKIES_B64"

    if host_matches(host, "facebook.com") or host_matches(host, "fb.watch"):
        return "FACEBOOK_COOKIES_B64"

    if host_matches(host, "tiktok.com"):
        return "TIKTOK_COOKIES_B64"

    if host_matches(host, "x.com") or host_matches(host, "twitter.com"):
        return "X_COOKIES_B64"

    return None


def prepare_cookies(host, workdir):
    """
    Optional Base64-encoded Netscape cookies.
    Only use cookies from accounts you control or are authorized to use.
    """
    variable = get_cookie_environment(host)
    if not variable:
        return None

    encoded = os.getenv(variable, "").strip()
    if not encoded:
        return None

    try:
        raw = base64.b64decode(encoded, validate=True)

        if b"# Netscape HTTP Cookie File" not in raw[:300]:
            log.warning("%s may not be a Netscape-format cookie file", variable)

        cookie_path = workdir / "cookies.txt"
        cookie_path.write_bytes(raw)
        cookie_path.chmod(0o600)
        return str(cookie_path)

    except Exception as exc:
        log.error("Could not decode %s: %s", variable, exc)
        return None


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(url, workdir):
    host = get_host(url)

    options = {
        "outtmpl": str(workdir / "%(title).100B_%(id)s.%(ext)s"),
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "ignoreerrors": False,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "overwrites": True,
    }

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg

    cookie_file = prepare_cookies(host, workdir)
    if cookie_file:
        options["cookiefile"] = cookie_file

    # YouTube: try compatible clients, with a general format fallback.
    if (
        host_matches(host, "youtube.com")
        or host_matches(host, "youtu.be")
    ):
        options["extractor_args"] = {
            "youtube": {
                "player_client": ["web_safari", "tv"]
            }
        }

    return options


# ============================================================
# DOWNLOAD FUNCTION
# ============================================================

def download_media(url, workdir):
    """
    Runs synchronously in a worker thread.
    Returns the downloaded file path, title, and host.
    """
    options = build_ydl_options(url, workdir)
    host = get_host(url)

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    if not info:
        raise RuntimeError("yt-dlp returned no media information.")

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        info = next((entry for entry in entries if entry), None)

        if not info:
            raise RuntimeError("No downloadable item was found.")

    title = info.get("title") or "Downloaded media"
    candidates = []

    # Prefer the filepath reported by yt-dlp.
    for key in ("filepath", "_filename"):
        value = info.get(key)
        if value:
            candidates.append(Path(value))

    for item in info.get("requested_downloads") or []:
        value = item.get("filepath")
        if value:
            candidates.append(Path(value))

    # Also inspect files produced in the temporary directory.
    for path in workdir.iterdir():
        if not path.is_file():
            continue

        if path.name == "cookies.txt":
            continue

        if path.name.endswith((".part", ".ytdl", ".json")):
            continue

        candidates.append(path)

    unique = []
    seen = set()

    for path in candidates:
        try:
            path = path.resolve()

            if path in seen or not path.is_file():
                continue

            seen.add(path)
            unique.append(path)
        except OSError:
            continue

    if not unique:
        raise RuntimeError(
            "No output file was found. Check FFmpeg availability "
            "and the hosting logs."
        )

    # Prefer common final media formats, then the largest file.
    preferred = [
        path for path in unique
        if path.suffix.lower() in {
            ".mp4", ".mkv", ".webm", ".mov", ".avi",
            ".mp3", ".m4a", ".aac", ".opus", ".wav", ".flac",
        }
    ]

    choices = preferred or unique
    output = max(choices, key=lambda path: path.stat().st_size)

    if output.stat().st_size == 0:
        raise RuntimeError("The downloaded output file is empty.")

    log.info(
        "Download complete: host=%s size_mb=%.2f",
        host,
        output.stat().st_size / (1024 * 1024),
    )

    return output, title, host


# ============================================================
# TELEGRAM STATUS HELPERS
# ============================================================

async def edit_status(message, text):
    try:
        await message.edit_text(text[:4000])
    except Exception:
        log.exception("Unable to update Telegram status")


# ============================================================
# SEND FILE
# ============================================================

async def send_media(message, file_path, title):
    size = file_path.stat().st_size

    if size > MAX_UPLOAD_BYTES:
        await message.reply_text(
            "✅ The download completed, but the file is too large "
            "for this bot's configured upload limit.\n\n"
            f"File size: {size / (1024 * 1024):.1f} MB\n"
            f"Configured limit: {MAX_UPLOAD_MB} MB\n\n"
            "Increase MAX_UPLOAD_MB only if your Telegram API and "
            "hosting setup support the larger upload."
        )
        return

    suffix = file_path.suffix.lower()
    caption = title[:900]

    with file_path.open("rb") as file_obj:
        if suffix in {".mp4", ".mkv", ".mov", ".avi", ".webm"}:
            await message.reply_video(
                video=file_obj,
                caption=caption,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )

        elif suffix in {
            ".mp3", ".m4a", ".aac", ".wav", ".flac", ".opus"
        }:
            await message.reply_audio(
                audio=file_obj,
                title=title[:250],
                caption=caption,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )

        else:
            await message.reply_document(
                document=file_obj,
                filename=file_path.name[:250],
                caption=caption,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )


# ============================================================
# COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        "👋 Welcome to Media Downloader!\n\n"
        "Send a supported YouTube, YouTube Shorts, Instagram, "
        "or other yt-dlp-supported media URL.\n\n"
        "Commands:\n"
        "/start — Start the bot\n"
        "/help — Usage and limitations\n"
        "/status — Check configuration\n\n"
        "Some sites require valid, authorized cookies. "
        "Private or restricted media may not be downloadable."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        "📥 How to use\n\n"
        "1. Copy a supported media link.\n"
        "2. Send it to this bot.\n"
        "3. Wait for the download to finish.\n\n"
        "If it fails, the bot displays the technical reason. "
        "Keep your bot token and cookies private."
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_message:
        return

    ffmpeg_ok = shutil.which("ffmpeg") is not None
    version = getattr(yt_dlp.version, "__version__", "unknown")

    await update.effective_message.reply_text(
        "🛠 Downloader status\n\n"
        f"Bot token configured: {'Yes' if BOT_TOKEN else 'No'}\n"
        f"yt-dlp version: {version}\n"
        f"FFmpeg available: {'Yes' if ffmpeg_ok else 'No'}\n"
        f"Configured upload limit: {MAX_UPLOAD_MB} MB\n"
        "Instagram cookies configured: "
        f"{'Yes' if os.getenv('INSTAGRAM_COOKIES_B64') else 'No'}\n"
        "YouTube cookies configured: "
        f"{'Yes' if os.getenv('YOUTUBE_COOKIES_B64') else 'No'}"
    )


# ============================================================
# MEDIA LINK HANDLER
# ============================================================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not message.text:
        return

    url = extract_url(message.text)
    if not url:
        await message.reply_text(
            "Please send a valid HTTP or HTTPS media link."
        )
        return

    host = get_host(url)
    status = await message.reply_text(
        f"🔗 Link received: {host}\n"
        "⏳ Preparing download…"
    )

    workdir = Path(tempfile.mkdtemp(prefix="telegram_media_"))

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.TYPING,
        )

        await edit_status(
            status,
            f"⬇️ Downloading from {host}…\nPlease wait."
        )

        output, title, actual_host = await asyncio.wait_for(
            asyncio.to_thread(download_media, url, workdir),
            timeout=DOWNLOAD_TIMEOUT,
        )

        await edit_status(status, "📤 Download complete. Sending file…")
        await send_media(message, output, title)

        try:
            await status.delete()
        except Exception:
            pass

    except asyncio.TimeoutError:
        await edit_status(
            status,
            f"⏱️ Download timed out after {DOWNLOAD_TIMEOUT} seconds. "
            "Try a shorter video or check the server logs."
        )

    except yt_dlp.utils.DownloadError as exc:
        # Avoid logging the full URL; it can contain private tokens.
        reason = str(exc).replace(BOT_TOKEN, "[hidden]")[:1000]
        log.error("yt-dlp failed for host %s: %s", host, reason)

        await edit_status(
            status,
            "❌ Download failed.\n\n"
            f"Platform: {host}\n"
            f"Technical reason:\n{reason}\n\n"
            "Update yt-dlp and check the hosting logs. "
            "If login is required, use a current cookie file from "
            "an account you control."
        )

    except Exception as exc:
        reason = str(exc).replace(BOT_TOKEN, "[hidden]")[:1000]
        log.exception("Unexpected download error for host %s", host)

        await edit_status(
            status,
            "❌ An unexpected error occurred.\n\n"
            f"Platform: {host}\n"
            f"Technical reason: {reason or type(exc).__name__}"
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ============================================================
# FILE HANDLER
# ============================================================

async def handle_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_message:
        await update.effective_message.reply_text(
            "📄 File received. This bot downloads media from links; "
            "it does not currently convert arbitrary uploaded files."
        )


# ============================================================
# STARTUP
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it in your hosting environment."
        )

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL
            | filters.PHOTO
            | filters.VIDEO
            | filters.AUDIO
            | filters.VOICE,
            handle_file,
        )
    )

    log.info("Starting Telegram media downloader")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
