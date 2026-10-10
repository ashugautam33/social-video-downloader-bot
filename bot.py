import asyncio
import base64
import logging
import os
import re
import tempfile
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
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "45"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "180"))
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/telegram-media-bot"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("telegram-media-bot")

SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".rtf",
    ".jpg", ".jpeg", ".png", ".gif", ".svg",
    ".heic", ".heif",
    ".mp3", ".wav", ".m4a",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".zip", ".rar", ".apk", ".ipa", ".exe", ".dmg",
}

# Set these environment variables to Base64-encoded Netscape
# cookies.txt files for accounts you own or are authorized to use.
COOKIE_DOMAINS = {
    "youtube.com": "YOUTUBE_COOKIES_B64",
    "youtu.be": "YOUTUBE_COOKIES_B64",
    "instagram.com": "INSTAGRAM_COOKIES_B64",
    "facebook.com": "FACEBOOK_COOKIES_B64",
    "fb.watch": "FACEBOOK_COOKIES_B64",
    "snapchat.com": "SNAPCHAT_COOKIES_B64",
    "x.com": "X_COOKIES_B64",
    "twitter.com": "X_COOKIES_B64",
    "linkedin.com": "LINKEDIN_COOKIES_B64",
    "pinterest.com": "PINTEREST_COOKIES_B64",
    "reddit.com": "REDDIT_COOKIES_B64",
    "t.me": "TELEGRAM_COOKIES_B64",
    "whatsapp.com": "WHATSAPP_COOKIES_B64",
    "messenger.com": "MESSENGER_COOKIES_B64",
    "sharechat.com": "SHARECHAT_COOKIES_B64",
    "mojapp.in": "MOJ_COOKIES_B64",
    "joshapp.com": "JOSH_COOKIES_B64",
    "chingari.io": "CHINGARI_COOKIES_B64",
    "arattai.in": "ARATTAI_COOKIES_B64",
    "sandes.gov.in": "SANDES_COOKIES_B64",
    "kooapp.com": "KOO_COOKIES_B64",
}

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


# ============================================================
# HELPERS
# ============================================================

def find_cookie_variable(url: str):
    host = (urlparse(url).hostname or "").lower()

    for domain, env_name in COOKIE_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            value = os.getenv(env_name, "").strip()
            if value:
                return env_name, value

    return None, None


def create_cookie_file(url: str, directory: Path):
    env_name, encoded = find_cookie_variable(url)

    if not encoded:
        return None

    try:
        content = base64.b64decode(encoded, validate=True)

        # Refuse obviously invalid cookie files.
        if b"# Netscape HTTP Cookie File" not in content[:5000] and \
           b"# HTTP Cookie File" not in content[:5000]:
            log.warning("%s is not a recognizable Netscape cookie file", env_name)
            return None

        cookie_path = directory / "cookies.txt"
        cookie_path.write_bytes(content)
        cookie_path.chmod(0o600)
        return cookie_path

    except Exception:
        log.warning("Unable to decode cookie variable %s", env_name)
        return None


def safe_filename(name: str) -> str:
    name = unquote(Path(name).name)
    name = re.sub(r"[^A-Za-z0-9._() -]+", "_", name)
    return name.strip(" .")[:160] or "download.bin"


def extract_url(text: str):
    matches = URL_PATTERN.findall(text or "")
    if not matches:
        return None
    return matches[0].rstrip(".,!?)'\"")


def is_valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def check_size(path: Path):
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("The download did not produce a usable file.")

    if path.stat().st_size > MAX_FILE_BYTES:
        path.unlink(missing_ok=True)
        raise ValueError(
            f"File exceeds the configured {MAX_FILE_MB} MB limit."
        )


def download_direct_file(url: str, directory: Path) -> Path:
    """Download a direct file URL with a size limit."""
    with requests.get(
        url,
        stream=True,
        timeout=(15, DOWNLOAD_TIMEOUT),
        headers={"User-Agent": "Mozilla/5.0 TelegramMediaBot/1.0"},
        allow_redirects=True,
    ) as response:
        response.raise_for_status()

        path_suffix = Path(urlparse(response.url).path).suffix.lower()
        disposition = response.headers.get("Content-Disposition", "")

        if path_suffix not in SUPPORTED_EXTENSIONS:
            match = re.search(
                r'filename\*?=(?:UTF-8\'\')?"?([^";]+)',
                disposition,
                re.IGNORECASE,
            )
            if match:
                path_suffix = Path(unquote(match.group(1))).suffix.lower()

        if path_suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                "This is not a recognized direct file link. "
                "Send a direct file URL, not a webpage displaying a file."
            )

        length = int(response.headers.get("Content-Length", "0") or 0)
        if length > MAX_FILE_BYTES:
            raise ValueError(f"File exceeds {MAX_FILE_MB} MB.")

        target = directory / ("download" + path_suffix)
        total = 0

        with target.open("wb") as output:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue

                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    target.unlink(missing_ok=True)
                    raise ValueError(f"File exceeds {MAX_FILE_MB} MB.")

                output.write(chunk)

    check_size(target)
    return target


def download_with_ytdlp(url: str, directory: Path) -> Path:
    """Download media without intentionally re-encoding audio or video."""
    cookie_file = create_cookie_file(url, directory)

    options = {
        "outtmpl": str(directory / "%(title).100B_%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "retries": 2,
        "fragment_retries": 2,
        "max_filesize": MAX_FILE_BYTES,
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mkv",
        "overwrites": True,
    }

    if cookie_file:
        options["cookiefile"] = str(cookie_file)

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(url, download=True)

    candidates = [
        p for p in directory.iterdir()
        if p.is_file()
        and p.name != "cookies.txt"
        and not p.name.endswith((".part", ".ytdl", ".json"))
    ]

    if not candidates:
        raise RuntimeError("No downloadable media file was produced.")

    # A merged output is normally the largest completed file.
    result = max(candidates, key=lambda p: p.stat().st_size)
    check_size(result)
    return result


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 Welcome to Media Downloader Bot!\n\n"
        "Send a supported public media URL or a direct file link.\n"
        "I can also receive and return uploaded documents.\n\n"
        "Commands:\n"
        "/start - Start the bot\n"
        "/help - Help and limitations\n"
        "/status - Check bot status\n\n"
        f"Maximum file size: {MAX_FILE_MB} MB\n\n"
        "Only download content you own or are authorized to save. "
        "Private or DRM-protected content is not bypassed."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"✅ Bot is running.\nMaximum file size: {MAX_FILE_MB} MB."
    )


# ============================================================
# URL DOWNLOAD HANDLER
# ============================================================

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    url = extract_url(message.text or message.caption or "")

    if not url:
        await message.reply_text("Please send a valid media or file URL.")
        return

    if not is_valid_url(url):
        await message.reply_text("Please send a valid HTTP or HTTPS URL.")
        return

    status_message = await message.reply_text(
        "⏳ Checking the link and downloading your file..."
    )

    await context.bot.send_chat_action(
        chat_id=message.chat_id,
        action=ChatAction.TYPING,
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix="download-", dir=str(WORK_DIR)
        ) as temporary_directory:
            directory = Path(temporary_directory)
            suffix = Path(urlparse(url).path).suffix.lower()

            if suffix in SUPPORTED_EXTENSIONS:
                try:
                    path = await asyncio.wait_for(
                        asyncio.to_thread(download_direct_file, url, directory),
                        timeout=DOWNLOAD_TIMEOUT + 20,
                    )
                except (requests.RequestException, ValueError):
                    # Some URLs with file extensions are webpages rather
                    # than direct files, so try yt-dlp as a fallback.
                    path = await asyncio.wait_for(
                        asyncio.to_thread(download_with_ytdlp, url, directory),
                        timeout=DOWNLOAD_TIMEOUT + 30,
                    )
            else:
                path = await asyncio.wait_for(
                    asyncio.to_thread(download_with_ytdlp, url, directory),
                    timeout=DOWNLOAD_TIMEOUT + 30,
                )

            check_size(path)
            filename = safe_filename(path.name)

            await status_message.edit_text("📤 Uploading your file to Telegram...")

            with path.open("rb") as file_handle:
                await message.reply_document(
                    document=file_handle,
                    filename=filename,
                    caption="✅ Download complete.",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )

            await status_message.delete()

    except asyncio.TimeoutError:
        await status_message.edit_text(
            "❌ Download timed out. Try a smaller file or another link."
        )

    except yt_dlp.utils.DownloadError:
        await status_message.edit_text(
            "❌ This platform could not provide the media.\n\n"
            "Possible causes: unsupported site, expired cookies, private "
            "content, regional restrictions, or unavailable media."
        )

    except Exception as error:
        log.exception("Download failed")
        reason = str(error).replace(BOT_TOKEN, "[hidden]")[:500]
        await status_message.edit_text(
            f"❌ Download failed.\nReason: {reason}"
        )


# ============================================================
# UPLOADED FILE HANDLER
# ============================================================

async def handle_uploaded_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    document = message.document

    if not document:
        return

    if document.file_size and document.file_size > MAX_FILE_BYTES:
        await message.reply_text(
            f"File exceeds the {MAX_FILE_MB} MB limit."
        )
        return

    filename = safe_filename(document.file_name or "uploaded_file")

    try:
        telegram_file = await document.get_file()

        with tempfile.TemporaryDirectory(
            prefix="telegram-upload-", dir=str(WORK_DIR)
        ) as temporary_directory:
            path = Path(temporary_directory) / filename
            await telegram_file.download_to_drive(custom_path=str(path))
            check_size(path)

            with path.open("rb") as file_handle:
                await message.reply_document(
                    document=file_handle,
                    filename=filename,
                    caption="📎 File received successfully.",
                )

    except Exception:
        log.exception("Uploaded document processing failed")
        await message.reply_text("❌ Unable to process this uploaded file.")


# ============================================================
# PHOTO / VIDEO / AUDIO HANDLER
# ============================================================

async def handle_uploaded_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    media = message.effective_attachment

    if not media:
        return

    size = getattr(media, "file_size", None)
    if size and size > MAX_FILE_BYTES:
        await message.reply_text(
            f"Media exceeds the {MAX_FILE_MB} MB limit."
        )
        return

    if message.photo:
        filename = "uploaded_photo.jpg"
    elif message.video:
        filename = "uploaded_video.mp4"
    elif message.audio:
        filename = safe_filename(
            message.audio.file_name or "uploaded_audio.mp3"
        )
    elif message.voice:
        filename = "uploaded_voice.ogg"
    else:
        filename = "uploaded_media.bin"

    try:
        telegram_file = await media.get_file()

        with tempfile.TemporaryDirectory(
            prefix="telegram-media-", dir=str(WORK_DIR)
        ) as temporary_directory:
            path = Path(temporary_directory) / filename
            await telegram_file.download_to_drive(custom_path=str(path))
            check_size(path)

            with path.open("rb") as file_handle:
                await message.reply_document(
                    document=file_handle,
                    filename=filename,
                    caption="📎 Media received successfully.",
                )

    except Exception:
        log.exception("Uploaded media processing failed")
        await message.reply_text("❌ Unable to process this media.")


# ============================================================
# START BOT
# ============================================================

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is missing. Set it in your environment variables."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))

    app.add_handler(
        MessageHandler(filters.Document.ALL, handle_uploaded_file)
    )
    app.add_handler(
        MessageHandler(
            filters.PHOTO
            | filters.VIDEO
            | filters.AUDIO
            | filters.VOICE,
            handle_uploaded_media,
        )
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)
    )

    log.info("Telegram Media Downloader Bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
