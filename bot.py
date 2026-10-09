
import asyncio
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

MAX_FILE_SIZE = 45 * 1024 * 1024
DOWNLOAD_TIMEOUT = 600

BASE_DIR = Path(tempfile.gettempdir()) / "media_downloader"
COOKIE_DIR = BASE_DIR / "cookies"
BASE_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

COOKIE_ENV = {
    "youtube": "YOUTUBE_COOKIES_B64",
    "instagram": "INSTAGRAM_COOKIES_B64",
    "tiktok": "TIKTOK_COOKIES_B64",
    "facebook": "FACEBOOK_COOKIES_B64",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("media_downloader")

URL_PATTERN = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".bmp", ".tif", ".tiff", ".heic", ".avif",
}

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".ogg", ".opus",
    ".wav", ".flac", ".wma", ".aiff",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv",
    ".avi", ".mpeg", ".mpg", ".3gp", ".ts",
}


# ============================================================
# URL HELPERS
# ============================================================

def extract_url(text):
    """Find the first HTTP(S) URL in a message."""
    if not text:
        return None

    for match in URL_PATTERN.findall(text):
        url = match.rstrip(".,!?;:)]}")

        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                continue
            if not parsed.hostname:
                continue
            if parsed.username or parsed.password:
                continue
        except ValueError:
            continue

        return url

    return None


def check_public_host(url):
    """
    Reject obvious local/private network targets before downloading.
    This is a basic safeguard, not a complete network security boundary.
    """
    parsed = urlparse(url)
    host = parsed.hostname

    if not host:
        raise ValueError("Invalid URL.")

    if host.lower() in {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
    }:
        raise ValueError("This URL is not allowed.")

    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # yt-dlp will report the connection failure if the host
        # cannot be resolved during download.
        return

    for address in addresses:
        ip_text = address[4][0].split("%")[0]

        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("Private or local network URLs are not allowed.")


def detect_platform(url):
    """Identify common platforms for optional cookie support."""
    host = (urlparse(url).hostname or "").lower()
    host = host.removeprefix("www.")

    if host == "youtu.be" or host.endswith("youtube.com"):
        return "youtube"
    if host.endswith("instagram.com"):
        return "instagram"
    if host.endswith("tiktok.com"):
        return "tiktok"
    if host.endswith("facebook.com") or host == "fb.watch":
        return "facebook"

    return "generic"


# ============================================================
# OPTIONAL COOKIE SUPPORT
# ============================================================

def create_cookie_file(platform):
    """Read a Base64-encoded Netscape cookies.txt environment value."""
    import base64
    import binascii

    env_name = COOKIE_ENV.get(platform)
    encoded = os.getenv(env_name, "").strip() if env_name else ""

    if not encoded:
        return None

    try:
        content = base64.b64decode(
            encoded,
            validate=True,
        ).decode("utf-8-sig")

        header_found = any(
            line.strip().startswith(
                ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
            )
            for line in content.splitlines()[:10]
        )

        if not header_found:
            logger.warning("%s is not a valid Netscape cookie file.", env_name)
            return None

        path = COOKIE_DIR / f"{platform}.txt"
        path.write_text(content, encoding="utf-8")
        return str(path)

    except (binascii.Error, UnicodeDecodeError, OSError):
        logger.warning("Could not decode %s.", env_name)
        return None


# ============================================================
# DOWNLOAD ENGINE
# ============================================================

def find_downloaded_files(folder):
    """Find finished files, excluding partials and metadata."""
    ignored = {
        ".part", ".ytdl", ".json", ".description",
        ".vtt", ".srt", ".ass", ".lrc",
    }

    files = []

    for path in folder.iterdir():
        if not path.is_file():
            continue

        if path.suffix.lower() in ignored:
            continue

        if path.name.endswith((".part", ".ytdl")):
            continue

        try:
            if path.stat().st_size > 0:
                files.append(path)
        except OSError:
            continue

    return sorted(
        files,
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def build_options(url, output_folder):
    """Configure yt-dlp without forcing everything to MP4."""
    platform = detect_platform(url)

    options = {
        "outtmpl": str(output_folder / "Downloaded_%(id)s.%(ext)s"),
        "format": "bestvideo*+bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        "continuedl": True,
        "overwrites": True,
        "max_filesize": MAX_FILE_SIZE,
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }

    cookie_file = create_cookie_file(platform)
    if cookie_file:
        options["cookiefile"] = cookie_file

    return options


def download_media(url, output_folder):
    """Download a supported media URL and return its saved file."""
    check_public_host(url)

    options = build_options(url, output_folder)

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)

        if not info:
            raise RuntimeError("No media information was returned.")

    files = find_downloaded_files(output_folder)

    if not files:
        raise RuntimeError("No downloadable media file was created.")

    # Prefer a media file that is within Telegram's upload limit.
    for path in files:
        if path.stat().st_size <= MAX_FILE_SIZE:
            return path

    raise RuntimeError("The downloaded file exceeds the 45 MB limit.")


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

async def send_media(message, path):
    """Send the file using the most suitable Telegram method."""
    extension = path.suffix.lower()
    size = path.stat().st_size

    if size <= 0:
        raise RuntimeError("The downloaded file is empty.")

    if size > MAX_FILE_SIZE:
        raise RuntimeError("The file is too large to upload.")

    caption = "✅ Download complete"

    # Telegram photo upload is most reliable with JPEG.
    if extension in {".jpg", ".jpeg"}:
        with path.open("rb") as file:
            await message.reply_photo(
                photo=file,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
            )
        return

    # Telegram audio upload is most reliable with MP3 or M4A.
    if extension in {".mp3", ".m4a"}:
        with path.open("rb") as file:
            await message.reply_audio(
                audio=file,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
            )
        return

    # MP4 is the preferred format for Telegram's video player.
    if extension == ".mp4":
        with path.open("rb") as file:
            await message.reply_video(
                video=file,
                caption=caption,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
            )
        return

    # Preserve other image, audio, video and file formats.
    mime_type, _ = mimetypes.guess_type(path.name)

    with path.open("rb") as file:
        await message.reply_document(
            document=file,
            filename=path.name,
            caption=f"{caption}\nType: {mime_type or extension or 'file'}",
            read_timeout=120,
            write_timeout=120,
        )


# ============================================================
# COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No reply for /start.
    return


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # No reply for /help.
    return


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message is None:
        return

    # Shared links may arrive as message text or as a caption.
    text = message.text or message.caption or ""
    url = extract_url(text)

    # Ignore ordinary text, commands, and messages without a URL.
    if not url:
        return

    status = await message.reply_text("⏳ Processing your media…")

    folder = Path(
        tempfile.mkdtemp(
            prefix="job_",
            dir=str(BASE_DIR),
        )
    )

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.TYPING,
        )

        # yt-dlp is synchronous, so run it in a worker thread.
        task = asyncio.create_task(
            asyncio.to_thread(download_media, url, folder)
        )

        try:
            media_path = await asyncio.wait_for(
                task,
                timeout=DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            # A timed-out worker may still be finishing its operation.
            # Do not remove its directory while it is running.
            task.add_done_callback(
                lambda _: shutil.rmtree(folder, ignore_errors=True)
            )
            folder = None
            raise RuntimeError("The download took too long. Please try again.")

        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.UPLOAD_DOCUMENT,
        )

        await send_media(message, media_path)

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as exc:
        logger.warning(
            "Media request failed (%s): %s",
            type(exc).__name__,
            str(exc)[:400],
        )

        error_message = (
            "❌ Unable to download this link.\n\n"
            "It may be private, unsupported, unavailable, or too large. "
            "Some websites require valid cookies or may block cloud servers."
        )

        try:
            await status.edit_text(error_message)
        except Exception:
            try:
                await message.reply_text(error_message)
            except Exception:
                pass

    finally:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)


# ============================================================
# APPLICATION ERROR HANDLER
# ============================================================

async def handle_error(update, context):
    error = context.error

    if error:
        logger.error(
            "Telegram application error: %s",
            error,
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add BOT_TOKEN in Railway Variables."
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(4)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.CAPTION & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_error_handler(handle_error)

    logger.info("Universal media downloader is starting.")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
