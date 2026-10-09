
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

URL_PATTERN = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("universal_media_bot")


# ============================================================
# URL HELPERS
# ============================================================

def extract_url(text):
    """Extract the first HTTP(S) URL from a message."""
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


def detect_platform(url):
    """Identify common websites for optional cookie support."""
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    hostname = hostname.removeprefix("www.")

    if hostname == "youtu.be" or hostname.endswith(".youtube.com") or hostname == "youtube.com":
        return "youtube"

    if hostname == "instagram.com" or hostname.endswith(".instagram.com"):
        return "instagram"

    if hostname == "facebook.com" or hostname.endswith(".facebook.com"):
        return "facebook"

    if hostname == "fb.watch":
        return "facebook"

    if hostname == "tiktok.com" or hostname.endswith(".tiktok.com"):
        return "tiktok"

    return "generic"


def validate_public_url(url):
    """
    Basic SSRF protection: reject URLs resolving to private,
    loopback, link-local, or otherwise non-public IP addresses.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid HTTP or HTTPS URL.")

    hostname = parsed.hostname

    try:
        ip = ipaddress.ip_address(hostname)
        addresses = [ip]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(result[4][0].split("%")[0])
                for result in socket.getaddrinfo(hostname, None)
            ]
        except (socket.gaierror, ValueError, OSError) as exc:
            raise ValueError("The URL hostname could not be resolved.") from exc

    if not addresses:
        raise ValueError("The URL hostname has no valid IP addresses.")

    for address in addresses:
        if not address.is_global:
            raise ValueError("Private or local network URLs are not allowed.")


# ============================================================
# OPTIONAL COOKIES
# ============================================================

def create_cookie_file(platform):
    """Read optional Base64-encoded Netscape cookies from environment."""
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

        has_header = any(
            line.strip().startswith(
                (
                    "# Netscape HTTP Cookie File",
                    "# HTTP Cookie File",
                )
            )
            for line in content.splitlines()[:10]
        )

        if not has_header:
            logger.warning(
                "COOKIE CONFIGURATION ERROR: %s is not a Netscape cookies file.",
                variable,
            )
            return None

        cookie_path = COOKIE_DIR / f"{platform}.txt"
        cookie_path.write_text(content, encoding="utf-8")
        return str(cookie_path)

    except (binascii.Error, UnicodeDecodeError, OSError):
        logger.exception("COOKIE CONFIGURATION ERROR for %s.", variable)
        return None


# ============================================================
# TEMPORARY FILE HELPERS
# ============================================================

def find_media_files(folder):
    """Return finished files and ignore partial downloads and metadata."""
    ignored = {
        ".part", ".ytdl", ".json", ".description",
        ".vtt", ".srt", ".ass", ".lrc",
    }

    results = []

    for path in folder.iterdir():
        if not path.is_file():
            continue

        if path.suffix.lower() in ignored:
            continue

        if path.name.endswith((".part", ".ytdl")):
            continue

        try:
            if path.stat().st_size > 0:
                results.append(path)
        except OSError:
            logger.exception("Could not inspect a downloaded file.")

    return sorted(
        results,
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )


def clear_folder(folder):
    """Remove files before trying a second download method."""
    for path in folder.iterdir():
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            logger.warning("Could not remove temporary file: %s", path.name)


# ============================================================
# YT-DLP OPTIONS
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
        "file_access_retries": 1,
        "socket_timeout": 15,
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
        logger.info("Cookies configured for platform: %s", platform)

    return options


# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def download_with_ytdlp(url, folder):
    logger.info(
        "YTDLP START | platform=%s",
        detect_platform(url),
    )

    options = build_ydl_options(url, folder)

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(
            url,
            download=True,
        )

        if not info:
            raise RuntimeError("yt-dlp returned no media information.")

    files = find_media_files(folder)

    for path in files:
        if path.stat().st_size <= MAX_FILE_SIZE:
            logger.info(
                "YTDLP SUCCESS | extension=%s | size=%s",
                path.suffix,
                path.stat().st_size,
            )
            return path

    if files:
        raise RuntimeError("Downloaded media exceeds the 45 MB limit.")

    raise RuntimeError("yt-dlp finished without producing a media file.")


# ============================================================
# DIRECT FILE DOWNLOAD
# ============================================================

def download_direct_file(url, folder):
    """
    Download direct media URLs, checking every redirect.
    This method cannot extract media from an ordinary webpage.
    """
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
                timeout=(8, 15),
                allow_redirects=False,
            )

            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()

                if not location:
                    raise RuntimeError("Redirect has no Location header.")

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
                        "URL returned HTML, not a direct media file."
                    )

                content_length = response.headers.get("Content-Length")

                if content_length:
                    try:
                        if int(content_length) > MAX_FILE_SIZE:
                            raise RuntimeError("Direct file exceeds 45 MB.")
                    except ValueError:
                        pass

                extension = Path(urlparse(current_url).path).suffix.lower()

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

                with destination.open("wb") as output:
                    for chunk in response.iter_content(
                        chunk_size=64 * 1024
                    ):
                        if not chunk:
                            continue

                        total += len(chunk)

                        if total > MAX_FILE_SIZE:
                            raise RuntimeError("Direct file exceeds 45 MB.")

                        output.write(chunk)

                if total == 0:
                    destination.unlink(missing_ok=True)
                    raise RuntimeError("The direct file was empty.")

                logger.info(
                    "DIRECT SUCCESS | extension=%s | size=%s",
                    destination.suffix,
                    total,
                )

                return destination

            finally:
                response.close()

    raise RuntimeError("Too many redirects.")


# ============================================================
# DOWNLOAD WITH REAL ERROR LOGGING
# ============================================================

def download_media(url, folder):
    """
    Try yt-dlp first, then direct downloading.
    Log both failures so the real cause is visible in Railway.
    """
    validate_public_url(url)

    ytdlp_error = None

    try:
        return download_with_ytdlp(url, folder)

    except Exception as exc:
        ytdlp_error = exc

        logger.error(
            "YTDLP FAILURE | type=%s | reason=%s",
            type(exc).__name__,
            str(exc)[:2000],
            exc_info=True,
        )

    clear_folder(folder)

    try:
        return download_direct_file(url, folder)

    except Exception as exc:
        logger.error(
            "DIRECT DOWNLOAD FAILURE | type=%s | reason=%s",
            type(exc).__name__,
            str(exc)[:2000],
            exc_info=True,
        )

        raise RuntimeError(
            "Both download methods failed. See YTDLP FAILURE and "
            "DIRECT DOWNLOAD FAILURE in Railway logs."
        ) from ytdlp_error


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

async def send_media(message, path, deadline):
    """Send media in Telegram's appropriate format."""
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
                    connect_timeout=min(8, remaining),
                    pool_timeout=min(8, remaining),
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
                    connect_timeout=min(8, remaining),
                    pool_timeout=min(8, remaining),
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
                    connect_timeout=min(8, remaining),
                    pool_timeout=min(8, remaining),
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
                connect_timeout=min(8, remaining),
                pool_timeout=min(8, remaining),
            )

    await asyncio.wait_for(upload(), timeout=remaining)


# ============================================================
# COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Intentionally no reply.
    return


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Intentionally no reply.
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

    text = message.text or message.caption or ""
    url = extract_url(text)

    # Ignore ordinary text and messages without URLs.
    if not url:
        return

    started = time.monotonic()
    deadline = started + PROCESS_TIMEOUT

    try:
        status = await message.reply_text(
            "🙏 Thanks for sharing! Your file is under process ⏳"
        )
    except Exception:
        logger.exception("Could not send processing acknowledgement.")
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
            # A Python worker thread cannot be killed safely.
            # Remove its directory after it actually finishes.
            cleanup_folder = False

            def cleanup_after_worker(task):
                shutil.rmtree(folder, ignore_errors=True)

            download_task.add_done_callback(cleanup_after_worker)
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
            "REQUEST SUCCESS | duration=%.1fs",
            time.monotonic() - started,
        )

    except asyncio.TimeoutError:
        logger.warning("REQUEST TIMEOUT | deadline=%s seconds", PROCESS_TIMEOUT)

        try:
            await status.edit_text(
                "⏱️ Processing exceeded 120 seconds.\n"
                "Please try a smaller file or another link."
            )
        except Exception:
            pass

    except Exception as exc:
        logger.error(
            "REQUEST FAILURE | type=%s | reason=%s",
            type(exc).__name__,
            str(exc)[:1500],
            exc_info=True,
        )

        # Show the category, but avoid sending sensitive internal details.
        detail = str(exc).lower()

        if "sign in" in detail or "cookies" in detail or "login" in detail:
            reason = "The website may require valid login cookies."
        elif "too large" in detail or "45 mb" in detail:
            reason = "The file may exceed the 45 MB limit."
        elif "hostname" in detail or "resolve" in detail:
            reason = "The server could not resolve the link's hostname."
        elif "timed out" in detail or "timeout" in detail:
            reason = "The website took too long to respond."
        else:
            reason = (
                "The link may be restricted, expired, unsupported, "
                "or blocked by the hosting provider."
            )

        try:
            await status.edit_text(
                "❌ Unable to download this link.\n\n"
                f"Possible reason: {reason}\n\n"
                "The detailed error has been recorded in Railway logs."
            )
        except Exception:
            pass

    finally:
        if cleanup_folder:
            shutil.rmtree(folder, ignore_errors=True)


# ============================================================
# APPLICATION ERROR HANDLER
# ============================================================

async def handle_error(update, context):
    if context.error:
        logger.error(
            "TELEGRAM APPLICATION ERROR: %s",
            context.error,
            exc_info=(
                type(context.error),
                context.error,
                context.error.__traceback__,
            ),
        )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add it in Railway Variables."
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
