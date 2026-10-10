
import os
import re
import sys
import time
import base64
import asyncio
import logging
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
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

# Optional: comma-separated Telegram user IDs.
# Empty means anyone can use the bot.
ALLOWED_USERS = {
    item.strip()
    for item in os.getenv("ALLOWED_USERS", "").split(",")
    if item.strip()
}

# Leave at 45 MB for compatibility with hosted environments.
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "45"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "180"))
MAX_CONCURRENT_DOWNLOADS = int(
    os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")
)

# Optional Netscape-format cookie files encoded in Base64.
# Create these from accounts you are authorized to use.
COOKIE_VARIABLES = {
    "youtube": ("YOUTUBE_COOKIES_B64",),
    "instagram": ("INSTAGRAM_COOKIES_B64",),
    "facebook": ("FACEBOOK_COOKIES_B64",),
    "x": ("X_COOKIES_B64",),
    "twitter": ("X_COOKIES_B64",),
    "tiktok": ("TIKTOK_COOKIES_B64",),
    "reddit": ("REDDIT_COOKIES_B64",),
    "linkedin": ("LINKEDIN_COOKIES_B64",),
    "snapchat": ("SNAPCHAT_COOKIES_B64",),
}

# File types the bot can receive, download, or forward.
ALLOWED_EXTENSIONS = {
    # Documents
    ".pdf", ".doc", ".docx", ".txt", ".rtf",

    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".svg",
    ".heic", ".heif",

    # Audio
    ".mp3", ".wav", ".m4a",

    # Video
    ".mp4", ".mov", ".avi", ".mkv",
    ".webm", ".m4v", ".ts",

    # Archives
    ".zip", ".rar",

    # Packages and disk images
    ".apk", ".ipa", ".exe", ".dmg",
}

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(
    max(1, MAX_CONCURRENT_DOWNLOADS)
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("social-downloader")

URL_PATTERN = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)

PLATFORM_HINTS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "twitter.com": "x",
    "x.com": "x",
    "tiktok.com": "tiktok",
    "reddit.com": "reddit",
    "redd.it": "reddit",
    "linkedin.com": "linkedin",
    "snapchat.com": "snapchat",
}

# ============================================================
# AUTHORIZATION
# ============================================================

def user_is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True

    user = update.effective_user
    return bool(user and str(user.id) in ALLOWED_USERS)


async def check_access(update: Update) -> bool:
    if user_is_allowed(update):
        return True

    if update.effective_message:
        await update.effective_message.reply_text(
            "⛔ You are not authorized to use this bot."
        )
    return False


# ============================================================
# COOKIE MANAGEMENT
# ============================================================

def identify_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    host = host.removeprefix("www.")

    for domain, platform in PLATFORM_HINTS.items():
        if host == domain or host.endswith("." + domain):
            return platform

    return "generic"


def create_cookie_file(platform: str, folder: Path):
    """Decode an optional Base64 Netscape cookie file to a temp file."""
    variable_names = COOKIE_VARIABLES.get(platform, ())

    # Generic fallback can be used for another compatible extractor.
    variable_names = (*variable_names, "COOKIES_B64")

    for variable in variable_names:
        value = os.getenv(variable, "").strip()
        if not value:
            continue

        try:
            raw = base64.b64decode(
                value,
                validate=True,
            )
            content = raw.decode("utf-8")

            if (
                "Netscape HTTP Cookie File" not in content
                and "#HttpOnly_" not in content
                and not any(
                    line.strip().count("\t") >= 6
                    for line in content.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                )
            ):
                raise ValueError(
                    "Cookie data is not in Netscape format."
                )

            path = folder / "cookies.txt"
            path.write_text(content, encoding="utf-8")
            path.chmod(0o600)

            logger.info("Cookie file loaded from %s", variable)
            return path

        except Exception as exc:
            logger.warning(
                "Could not load %s: %s",
                variable,
                type(exc).__name__,
            )
            raise ValueError(
                f"{variable} is invalid. Use a Base64-encoded "
                "Netscape-format cookies.txt file."
            ) from exc

    return None


# ============================================================
# URL VALIDATION
# ============================================================

def extract_url(text: str):
    match = URL_PATTERN.search(text or "")
    if not match:
        return None

    # Remove common punctuation added around links in messages.
    url = match.group(0).rstrip(".,!?;:)]}")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None

    return url


def safe_filename(path: Path) -> str:
    name = unquote(path.name or "download")
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    return name[:180] or "download"


def file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def find_downloaded_file(folder: Path):
    candidates = [
        p for p in folder.rglob("*")
        if p.is_file()
        and p.name != "cookies.txt"
        and not p.name.endswith((".part", ".ytdl", ".tmp"))
    ]

    if not candidates:
        raise RuntimeError(
            "The extractor did not produce a complete file."
        )

    # Prefer the largest completed media file.
    return max(candidates, key=lambda p: p.stat().st_size)


# ============================================================
# DIRECT FILE DOWNLOAD
# ============================================================

def download_direct_file(url: str, folder: Path):
    """Download a direct file URL, enforcing size and timeout limits."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
    }

    with requests.get(
        url,
        stream=True,
        allow_redirects=True,
        timeout=(15, DOWNLOAD_TIMEOUT),
        headers=headers,
    ) as response:
        response.raise_for_status()

        final_host = (urlparse(response.url).hostname or "").lower()
        original_host = (urlparse(url).hostname or "").lower()

        # Basic protection against redirecting to local services.
        # Public DNS names and hosts are still not a complete SSRF defense.
        if (
            final_host in {"localhost", "127.0.0.1", "::1"}
            or final_host.startswith(("10.", "192.168.", "169.254."))
            or final_host.startswith("172.16.")
            or final_host == "0.0.0.0"
        ):
            raise ValueError("Downloads from local addresses are blocked.")

        disposition = response.headers.get(
            "Content-Disposition", ""
        )
        filename_match = re.search(
            r'filename\*?=(?:UTF-8\'\')?"?([^";]+)',
            disposition,
            re.IGNORECASE,
        )

        filename = (
            unquote(filename_match.group(1).strip())
            if filename_match
            else Path(urlparse(response.url).path).name
        )

        filename = safe_filename(Path(filename or "download"))
        destination = folder / filename

        extension = destination.suffix.lower()
        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        # Do not save HTML login pages or arbitrary web pages as files.
        if (
            "text/html" in content_type
            or "application/json" in content_type
        ):
            raise ValueError(
                "This URL returned a webpage/API response, not a media file. "
                "Send a direct downloadable file URL instead."
            )

        if extension not in ALLOWED_EXTENSIONS:
            # Try to infer a known extension from MIME type.
            mime_map = {
                "application/pdf": ".pdf",
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/svg+xml": ".svg",
                "audio/mpeg": ".mp3",
                "audio/wav": ".wav",
                "audio/x-wav": ".wav",
                "audio/mp4": ".m4a",
                "video/mp4": ".mp4",
                "video/quicktime": ".mov",
                "video/x-msvideo": ".avi",
                "video/x-matroska": ".mkv",
                "application/zip": ".zip",
                "application/vnd.android.package-archive": ".apk",
            }
            inferred = mime_map.get(content_type.split(";")[0].strip())
            if not inferred:
                raise ValueError(
                    "Unknown or unsupported direct-file format."
                )
            destination = destination.with_suffix(inferred)

        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_FILE_BYTES:
            raise ValueError(
                f"File exceeds the {MAX_FILE_MB} MB limit."
            )

        total = 0
        with destination.open("wb") as output:
            for chunk in response.iter_content(256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    output.close()
                    destination.unlink(missing_ok=True)
                    raise ValueError(
                        f"File exceeds the {MAX_FILE_MB} MB limit."
                    )
                output.write(chunk)

        if total == 0:
            destination.unlink(missing_ok=True)
            raise RuntimeError("The server returned an empty file.")

        return destination


# ============================================================
# SOCIAL MEDIA DOWNLOAD
# ============================================================

def download_social_media(url: str, folder: Path):
    platform = identify_platform(url)
    cookie_file = create_cookie_file(platform, folder)

    output_template = str(folder / "%(title).100s-%(id)s.%(ext)s")

    options = {
        # Keep the original audio stream; merge instead of re-encoding.
        "format": "bv*+ba/b",
        "outtmpl": output_template,
        "merge_output_format": "mkv",
        "noplaylist": True,
        "overwrites": True,
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 25,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/131 Safari/537.36"
            ),
        },
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "cachedir": False,
        "max_filesize": MAX_FILE_BYTES,
        "overwrites": True,
    }

    if cookie_file:
        options["cookiefile"] = str(cookie_file)

    # Optional extractor tuning. These settings do not bypass
    # authentication or guarantee compatibility with every platform.
    if platform == "youtube":
        options["extractor_args"] = {
            "youtube": {
                "player_client": ["web_safari", "default"],
            }
        }

    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.download([url])

    result = find_downloaded_file(folder)

    if file_size(result) > MAX_FILE_BYTES:
        result.unlink(missing_ok=True)
        raise ValueError(
            f"Downloaded media exceeds the {MAX_FILE_MB} MB limit."
        )

    return result


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

async def send_file(update: Update, path: Path):
    message = update.effective_message
    if not message:
        return

    size = file_size(path)
    if size == 0:
        raise RuntimeError("The downloaded file is empty.")

    if size > MAX_FILE_BYTES:
        raise ValueError(
            f"The file is larger than the configured {MAX_FILE_MB} MB limit."
        )

    name = safe_filename(path)
    caption = f"✅ {name}"

    # Send as a document to avoid Telegram transcoding the media.
    with path.open("rb") as stream:
        await message.reply_document(
            document=stream,
            filename=name,
            caption=caption[:1024],
            read_timeout=60,
            write_timeout=60,
            connect_timeout=30,
            pool_timeout=30,
        )


# ============================================================
# BOT COMMANDS
# ============================================================

HELP_TEXT = """
📥 Multi-Platform Downloader

Send a supported media URL to download it.

Commands:
/start  - Start the bot
/help   - Show instructions
/id     - Show your Telegram user ID

Features:
• Supported yt-dlp social-media extractors
• Optional authorized account cookies
• Original audio retained in downloaded videos
• Direct downloadable file URLs
• File upload/forward support for configured extensions

Important:
• Some platforms or links may not be supported.
• Private or restricted content requires legitimate access.
• Maximum file size: {max_mb} MB.
• Only use media you are authorized to download.
""".strip()


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await check_access(update):
        return

    await update.effective_message.reply_text(
        "👋 Welcome to the Multi-Platform Media Downloader!\n\n"
        "Send a supported social-media URL or a direct file URL.\n"
        "Use /help for instructions."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await check_access(update):
        return

    await update.effective_message.reply_text(
        HELP_TEXT.format(max_mb=MAX_FILE_MB)
    )


async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await check_access(update):
        return

    user = update.effective_user
    if user:
        await update.effective_message.reply_text(
            f"Your Telegram user ID: {user.id}"
        )


# ============================================================
# URL MESSAGE HANDLER
# ============================================================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await check_access(update):
        return

    message = update.effective_message
    if not message or not message.text:
        return

    url = extract_url(message.text)
    if not url:
        await message.reply_text(
            "❌ Please send a valid http:// or https:// URL."
        )
        return

    status = await message.reply_text(
        "⏳ Checking the link and preparing your download..."
    )

    try:
        async with DOWNLOAD_SEMAPHORE:
            await context.bot.send_chat_action(
                chat_id=message.chat_id,
                action=ChatAction.TYPING,
            )

            with tempfile.TemporaryDirectory(
                prefix="social_dl_"
            ) as temp:
                folder = Path(temp)

                # First try yt-dlp, which supports many social platforms.
                # Fall back to direct-file download for genuine file URLs.
                result = None
                extractor_error = None

                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            download_social_media,
                            url,
                            folder,
                        ),
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                except Exception as exc:
                    extractor_error = exc
                    logger.info(
                        "Extractor attempt failed: %s",
                        type(exc).__name__,
                    )

                if result is None:
                    # The extractor may have left temporary files behind.
                    for item in folder.iterdir():
                        if item.is_file():
                            item.unlink(missing_ok=True)

                    try:
                        result = await asyncio.wait_for(
                            asyncio.to_thread(
                                download_direct_file,
                                url,
                                folder,
                            ),
                            timeout=DOWNLOAD_TIMEOUT,
                        )
                    except Exception as direct_error:
                        raise RuntimeError(
                            explain_error(
                                extractor_error,
                                direct_error,
                            )
                        ) from direct_error

                await status.edit_text("📤 Uploading the file to Telegram...")
                await send_file(update, result)

        await status.delete()

    except asyncio.TimeoutError:
        await safe_edit(
            status,
            "⏰ Download timed out. Try again later or use a smaller file.",
        )
    except Exception as exc:
        logger.exception("Download failed")
        await safe_edit(
            status,
            f"❌ Download failed.\n\n{str(exc)[:900]}",
        )


def explain_error(extractor_error, direct_error) -> str:
    combined = (
        f"{type(extractor_error).__name__ if extractor_error else ''} "
        f"{extractor_error or ''} "
        f"{type(direct_error).__name__} {direct_error}"
    ).lower()

    if "sign in" in combined or "cookies" in combined or "login" in combined:
        return (
            "Authentication is required. Configure a valid cookie file "
            "for an account you are authorized to use. Cookies may expire."
        )

    if (
        "no video formats" in combined
        or "unsupported url" in combined
        or "not a media file" in combined
    ):
        return (
            "This link is not supported by the current extractor, the media "
            "is unavailable, or the URL is not a direct file link. "
            "Check that the link is public or that your account has access."
        )

    if "exceeds" in combined or "max_filesize" in combined:
        return f"The file exceeds the {MAX_FILE_MB} MB limit."

    if "403" in combined or "forbidden" in combined:
        return (
            "The server denied access. Check permissions, sign-in status, "
            "and whether the URL is still valid."
        )

    if "404" in combined or "not found" in combined:
        return "The link is unavailable or has expired."

    return (
        "Unable to download this link. It may be unsupported, restricted, "
        "expired, or require valid authentication. Check the server logs "
        "for the technical error."
    )


async def safe_edit(message, text: str):
    try:
        await message.edit_text(text)
    except Exception:
        try:
            await message.reply_text(text)
        except Exception:
            logger.exception("Unable to send error message")


# ============================================================
# TELEGRAM DOCUMENT HANDLER
# ============================================================

async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await check_access(update):
        return

    message = update.effective_message
    document = message.document if message else None

    if not document:
        return

    filename = safe_filename(Path(document.file_name or "file"))
    extension = Path(filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        await message.reply_text(
            "❌ Unsupported file extension.\n"
            "Please send a supported document, image, audio, video, "
            "archive, APK, IPA, EXE, or DMG file."
        )
        return

    if document.file_size and document.file_size > MAX_FILE_BYTES:
        await message.reply_text(
            f"❌ File exceeds the {MAX_FILE_MB} MB limit."
        )
        return

    # Files already uploaded to Telegram can be forwarded without
    # downloading and re-uploading them.
    try:
        await message.forward(chat_id=message.chat_id)
    except Exception:
        # If forwarding is unavailable, download and re-upload.
        status = await message.reply_text("⏳ Preparing your file...")

        try:
            with tempfile.TemporaryDirectory(
                prefix="telegram_file_"
            ) as temp:
                path = Path(temp) / filename
                telegram_file = await context.bot.get_file(
                    document.file_id
                )
                await telegram_file.download_to_drive(
                    custom_path=path
                )

                if file_size(path) > MAX_FILE_BYTES:
                    raise ValueError(
                        f"File exceeds the {MAX_FILE_MB} MB limit."
                    )

                await send_file(update, path)

            await status.delete()

        except Exception as exc:
            logger.exception("Telegram file processing failed")
            await safe_edit(
                status,
                f"❌ File processing failed: {str(exc)[:700]}",
            )


async def handle_other_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Reject unsupported Telegram photo/audio/video types clearly."""
    if not await check_access(update):
        return

    message = update.effective_message
    if message:
        await message.reply_text(
            "Send a supported document or a social-media URL. "
            "Use /help to see the supported file extensions."
        )


# ============================================================
# GLOBAL ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error
    logger.error(
        "Unhandled Telegram error: %s",
        type(error).__name__ if error else "Unknown",
        exc_info=error,
    )


# ============================================================
# STARTUP
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it in your hosting platform's "
            "environment variables."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO
            | filters.VIDEO
            | filters.AUDIO
            | filters.VOICE
            | filters.VIDEO_NOTE
            | filters.ANIMATION,
            handle_other_file,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Starting Multi-Platform Media Downloader...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
