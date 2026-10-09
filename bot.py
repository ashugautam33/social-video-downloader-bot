
import asyncio
import base64
import binascii
import ipaddress
import logging
import mimetypes
import os
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
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

PROCESS_TIMEOUT = 120
MAX_FILE_SIZE = 45 * 1024 * 1024
MAX_REDIRECTS = 5

BASE_DIR = Path(tempfile.gettempdir()) / "universal_media_bot"
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
    "facebook": "FACEBOOK_COOKIES_B64",
    "tiktok": "TIKTOK_COOKIES_B64",
}

SUPPORTED_DOMAINS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "tiktok.com": "tiktok",
    "x.com": "generic",
    "twitter.com": "generic",
    "reddit.com": "generic",
    "pinterest.com": "generic",
    "soundcloud.com": "generic",
    "vimeo.com": "generic",
}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".bmp", ".tif", ".tiff", ".avif", ".heic",
}

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".aac", ".ogg", ".opus",
    ".wav", ".flac", ".wma", ".aiff",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv",
    ".avi", ".mpeg", ".mpg", ".3gp", ".ts",
}

URL_PATTERN = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("universal_media_bot")


# ============================================================
# URL HELPERS
# ============================================================

def extract_url(text):
    """Extract the first HTTP(S) link from a message."""
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

            return url

        except ValueError:
            continue

    return None


def validate_public_url(url):
    """Reject obvious private-network destinations."""
    parsed = urlparse(url)
    hostname = parsed.hostname

    if parsed.scheme not in ("http", "https") or not hostname:
        raise ValueError("Invalid URL.")

    try:
        direct_ip = ipaddress.ip_address(hostname)
        addresses = [direct_ip]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(result[4][0].split("%")[0])
                for result in socket.getaddrinfo(hostname, None)
            ]
        except (socket.gaierror, ValueError, OSError):
            raise ValueError("Unable to resolve the URL hostname.")

    if not addresses:
        raise ValueError("Unable to resolve the URL hostname.")

    for address in addresses:
        if not address.is_global:
            raise ValueError("Private or local network URLs are not allowed.")


def detect_platform(url):
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    hostname = hostname.removeprefix("www.")

    for domain, platform in SUPPORTED_DOMAINS.items():
        if hostname == domain or hostname.endswith("." + domain):
            return platform

    return "generic"


# ============================================================
# OPTIONAL COOKIES
# ============================================================

def create_cookie_file(platform):
    """Load Base64-encoded Netscape cookies from environment variables."""
    variable = COOKIE_ENV.get(platform)

    if not variable:
        return None

    encoded = os.getenv(variable, "").strip()

    if not encoded:
        return None

    try:
        content = base64.b64decode(
            encoded,
            validate=True,
        ).decode("utf-8-sig")

        valid_header = any(
            line.strip().startswith(
                (
                    "# Netscape HTTP Cookie File",
                    "# HTTP Cookie File",
                )
            )
            for line in content.splitlines()[:10]
        )

        if not valid_header:
            logger.warning("%s is not a valid Netscape cookie file.", variable)
            return None

        cookie_path = COOKIE_DIR / f"{platform}.txt"
        cookie_path.write_text(content, encoding="utf-8")
        return str(cookie_path)

    except (binascii.Error, UnicodeDecodeError, OSError):
        logger.warning("Could not read cookies from %s.", variable)
        return None


# ============================================================
# FILE HELPERS
# ============================================================

def find_media_files(folder):
    ignored_extensions = {
        ".part", ".ytdl", ".json", ".description",
        ".vtt", ".srt", ".ass", ".lrc",
    }

    results = []

    for path in folder.iterdir():
        if not path.is_file():
            continue

        if path.suffix.lower() in ignored_extensions:
            continue

        if path.name.endswith((".part", ".ytdl")):
            continue

        try:
            if path.stat().st_size > 0:
                results.append(path)
        except OSError:
            continue

    return sorted(
        results,
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )


def clear_folder(folder):
    for path in folder.iterdir():
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            logger.warning("Could not remove a temporary file.")


# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def build_ydl_options(url, folder):
    platform = detect_platform(url)

    options = {
        "outtmpl": str(folder / "media_%(id)s.%(ext)s"),
        "format": "bestvideo*+bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 1,
        "fragment_retries": 1,
        "socket_timeout": 20,
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


def download_with_ytdlp(url, folder):
    options = build_ydl_options(url, folder)

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)

        if not info:
            raise RuntimeError("No media information was returned.")

    files = find_media_files(folder)

    for path in files:
        if path.stat().st_size <= MAX_FILE_SIZE:
            return path

    if files:
        raise RuntimeError("The downloaded file exceeds the size limit.")

    raise RuntimeError("No completed media file was created.")


# ============================================================
# DIRECT IMAGE/AUDIO/VIDEO/FILE DOWNLOAD
# ============================================================

def download_direct_file(url, folder):
    current_url = url
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }

    with requests.Session() as session:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_url(current_url)

            response = session.get(
                current_url,
                headers=headers,
                stream=True,
                timeout=(10, 20),
                allow_redirects=False,
            )

            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()

                if not location:
                    raise RuntimeError("Invalid redirect response.")

                current_url = urljoin(current_url, location)
                continue

            try:
                response.raise_for_status()

                content_type = (
                    response.headers.get("Content-Type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )

                if content_type in {
                    "text/html",
                    "application/xhtml+xml",
                }:
                    raise RuntimeError(
                        "The URL opens a webpage, not a direct media file."
                    )

                content_length = response.headers.get("Content-Length")

                if content_length:
                    try:
                        if int(content_length) > MAX_FILE_SIZE:
                            raise RuntimeError("The file exceeds 45 MB.")
                    except ValueError:
                        pass

                url_path = urlparse(current_url).path
                extension = Path(url_path).suffix.lower()

                allowed_extensions = (
                    IMAGE_EXTENSIONS
                    | AUDIO_EXTENSIONS
                    | VIDEO_EXTENSIONS
                )

                if extension not in allowed_extensions:
                    extension = (
                        mimetypes.guess_extension(content_type or "")
                        or ".bin"
                    )

                if extension == ".jpe":
                    extension = ".jpg"

                destination = folder / f"direct_media{extension}"
                total = 0

                with destination.open("wb") as file:
                    for chunk in response.iter_content(
                        chunk_size=64 * 1024
                    ):
                        if not chunk:
                            continue

                        total += len(chunk)

                        if total > MAX_FILE_SIZE:
                            raise RuntimeError("The file exceeds 45 MB.")

                        file.write(chunk)

                if total == 0:
                    destination.unlink(missing_ok=True)
                    raise RuntimeError("The server returned an empty file.")

                return destination

            finally:
                response.close()

    raise RuntimeError("Too many redirects.")


def download_media(url, folder):
    """Try a supported website extractor, then a direct file URL."""
    validate_public_url(url)

    try:
        return download_with_ytdlp(url, folder)

    except Exception as first_error:
        logger.info(
            "yt-dlp attempt failed: %s",
            type(first_error).__name__,
        )

        clear_folder(folder)

        try:
            return download_direct_file(url, folder)
        except Exception as second_error:
            logger.warning(
                "Direct download failed: %s",
                type(second_error).__name__,
            )

            raise RuntimeError(
                "This link could not be downloaded. It may be private, "
                "expired, unsupported, or restricted."
            ) from second_error


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

async def send_media(message, path, deadline):
    """Send a suitable Telegram media type within the remaining time."""
    extension = path.suffix.lower()
    size = path.stat().st_size

    if size <= 0:
        raise RuntimeError("The downloaded file is empty.")

    if size > MAX_FILE_SIZE:
        raise RuntimeError("The file exceeds the 45 MB upload limit.")

    remaining = deadline - time.monotonic()

    if remaining <= 0:
        raise asyncio.TimeoutError()

    caption = "✅ Download complete"

    async def upload():
        if extension in {".jpg", ".jpeg"}:
            with path.open("rb") as file:
                await message.reply_photo(
                    photo=file,
                    caption=caption,
                    read_timeout=remaining,
                    write_timeout=remaining,
                    connect_timeout=min(10, remaining),
                    pool_timeout=min(10, remaining),
                )
            return

        if extension in {".mp3", ".m4a"}:
            with path.open("rb") as file:
                await message.reply_audio(
                    audio=file,
                    filename=path.name,
                    caption=caption,
                    read_timeout=remaining,
                    write_timeout=remaining,
                    connect_timeout=min(10, remaining),
                    pool_timeout=min(10, remaining),
                )
            return

        if extension == ".mp4":
            with path.open("rb") as file:
                await message.reply_video(
                    video=file,
                    filename=path.name,
                    caption=caption,
                    supports_streaming=True,
                    read_timeout=remaining,
                    write_timeout=remaining,
                    connect_timeout=min(10, remaining),
                    pool_timeout=min(10, remaining),
                )
            return

        mime_type, _ = mimetypes.guess_type(path.name)

        with path.open("rb") as file:
            await message.reply_document(
                document=file,
                filename=path.name,
                caption=(
                    f"{caption}\n"
                    f"Format: {mime_type or extension or 'unknown'}"
                ),
                read_timeout=remaining,
                write_timeout=remaining,
                connect_timeout=min(10, remaining),
                pool_timeout=min(10, remaining),
            )

    await asyncio.wait_for(upload(), timeout=remaining)


# ============================================================
# COMMAND HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Intentionally silent.
    return


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Intentionally silent.
    return


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message is None:
        return

    text = message.text or message.caption or ""
    url = extract_url(text)

    # Ignore normal messages and messages without a link.
    if not url:
        return

    started_at = time.monotonic()
    deadline = started_at + PROCESS_TIMEOUT

    try:
        status = await message.reply_text(
            "🙏 Thanks for sharing! Your file is under process ⏳"
        )
    except Exception:
        logger.exception("Could not send the processing message.")
        return

    folder = Path(
        tempfile.mkdtemp(
            prefix="job_",
            dir=str(BASE_DIR),
        )
    )

    cleanup_folder = True
    download_task = None

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.TYPING,
        )

        download_task = asyncio.create_task(
            asyncio.to_thread(download_media, url, folder)
        )

        remaining = deadline - time.monotonic()

        if remaining <= 0:
            raise asyncio.TimeoutError()

        try:
            media_path = await asyncio.wait_for(
                asyncio.shield(download_task),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            # Python cannot forcibly stop a running worker thread.
            # Wait for the worker to finish before deleting its files.
            cleanup_folder = False

            def cleanup_when_finished(task):
                shutil.rmtree(folder, ignore_errors=True)

            download_task.add_done_callback(cleanup_when_finished)
            raise

        remaining = deadline - time.monotonic()

        if remaining <= 0:
            raise asyncio.TimeoutError()

        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.UPLOAD_DOCUMENT,
        )

        await send_media(message, media_path, deadline)

        try:
            await status.delete()
        except Exception:
            pass

        logger.info(
            "Request completed in %.1f seconds.",
            time.monotonic() - started_at,
        )

    except asyncio.TimeoutError:
        logger.warning(
            "Request exceeded the %s-second processing deadline.",
            PROCESS_TIMEOUT,
        )

        try:
            await status.edit_text(
                "⏱️ Processing exceeded 120 seconds.\n"
                "Please try again with a smaller file or a faster link."
            )
        except Exception:
            pass

    except Exception as exc:
        logger.warning(
            "Request failed: %s: %s",
            type(exc).__name__,
            str(exc)[:300],
        )

        try:
            await status.edit_text(
                "❌ Unable to download this link.\n\n"
                "The file may be private, expired, unsupported, restricted, "
                "or larger than 45 MB."
            )
        except Exception:
            pass

    finally:
        if cleanup_folder:
            shutil.rmtree(folder, ignore_errors=True)


# ============================================================
# ERROR HANDLER
# ============================================================

async def handle_error(update, context):
    error = context.error

    if error:
        logger.error(
            "Unhandled application error: %s",
            error,
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )


# ============================================================
# START BOT
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
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_error_handler(handle_error)

    logger.info(
        "Universal media downloader started. Deadline: %s seconds.",
        PROCESS_TIMEOUT,
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
