
import asyncio
import base64
import ipaddress
import logging
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
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
DOWNLOAD_TIMEOUT = 110
REQUEST_TIMEOUT = (15, 30)

TEMP_ROOT = Path(tempfile.gettempdir()) / "universal_downloader"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

COOKIE_VARIABLES = {
    "YOUTUBE": "YOUTUBE_COOKIES_B64",
    "INSTAGRAM": "INSTAGRAM_COOKIES_B64",
    "FACEBOOK": "FACEBOOK_COOKIES_B64",
    "SNAPCHAT": "SNAPCHAT_COOKIES_B64",
    "X": "X_COOKIES_B64",
    "LINKEDIN": "LINKEDIN_COOKIES_B64",
    "PINTEREST": "PINTEREST_COOKIES_B64",
    "REDDIT": "REDDIT_COOKIES_B64",
    "WHATSAPP": "WHATSAPP_COOKIES_B64",
    "TELEGRAM": "TELEGRAM_COOKIES_B64",
    "SHARECHAT": "SHARECHAT_COOKIES_B64",
    "MOJ": "MOJ_COOKIES_B64",
    "JOSH": "JOSH_COOKIES_B64",
    "CHINGARI": "CHINGARI_COOKIES_B64",
    "ARATTAI": "ARATTAI_COOKIES_B64",
    "SANDES": "SANDES_COOKIES_B64",
    "KOO": "KOO_COOKIES_B64",
    "MESSENGER": "MESSENGER_COOKIES_B64",
}

URL_PATTERN = re.compile(r"""https?://[^\s<>"']+""", re.IGNORECASE)

PLATFORMS = {
    "YOUTUBE": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "INSTAGRAM": ("instagram.com",),
    "FACEBOOK": ("facebook.com", "fb.watch"),
    "SNAPCHAT": ("snapchat.com",),
    "X": ("x.com", "twitter.com"),
    "LINKEDIN": ("linkedin.com",),
    "TELEGRAM": ("t.me", "telegram.me", "telegram.org"),
    "PINTEREST": ("pinterest.com", "pin.it"),
    "REDDIT": ("reddit.com", "redd.it"),
    "WHATSAPP": ("whatsapp.com", "wa.me"),
    "MESSENGER": ("messenger.com", "m.me"),
    "SHARECHAT": ("sharechat.com",),
    "MOJ": ("mojapp.in", "mojapp.com"),
    "JOSH": ("joshapp.com", "myjosh.in"),
    "CHINGARI": ("chingari.io", "chingari.in"),
    "ARATTAI": ("arattai.in",),
    "SANDES": ("sandes.gov.in",),
    "KOO": ("kooapp.com",),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

for logger_name in (
    "httpx", "httpcore", "telegram", "telegram.ext", "urllib3"
):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

logger = logging.getLogger("universal_downloader")


# ============================================================
# URL HELPERS
# ============================================================

def platform_for_url(url):
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        return None

    for platform, domains in PLATFORMS.items():
        if any(host == domain or host.endswith("." + domain)
               for domain in domains):
            return platform

    return None


def extract_urls(message):
    if not message:
        return []

    text = message.text or message.caption or ""
    urls = [
        value.strip().rstrip(".,!?;:)]}>'\"")
        for value in URL_PATTERN.findall(text)
    ]

    entities = message.entities or message.caption_entities or []

    for entity in entities:
        if entity.type == "url":
            urls.append(
                text[entity.offset:entity.offset + entity.length]
                .strip()
                .rstrip(".,!?;:)]}>'\"")
            )
        elif entity.type == "text_link" and entity.url:
            urls.append(entity.url.strip())

    return list(dict.fromkeys(url for url in urls if url))


def validate_public_url(url):
    """Reject invalid URLs and local/private network destinations."""
    try:
        parsed = urlsplit(url)

        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False, "Only valid HTTP/HTTPS links are supported."

        if parsed.username or parsed.password:
            return False, "Links containing embedded credentials are unsupported."

        host = parsed.hostname.rstrip(".").lower()

        if host == "localhost" or host.endswith((".localhost", ".local")):
            return False, "Local network links are not supported."

        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            results = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = [
                ipaddress.ip_address(item[4][0]) for item in results
            ]

        if not addresses or any(not ip.is_global for ip in addresses):
            return False, "Private or local network addresses are not supported."

        return True, None

    except Exception:
        return False, "The link is invalid or its host cannot be resolved."


# ============================================================
# COOKIE SUPPORT
# ============================================================

def get_cookie_file(platform, folder):
    variable = COOKIE_VARIABLES.get(platform)
    encoded = os.getenv(variable, "").strip() if variable else ""

    if not encoded:
        return None

    try:
        encoded = re.sub(r"\s+", "", encoded)
        raw = base64.b64decode(encoded, validate=True)
        cookie_text = raw.decode("utf-8-sig")

        if not cookie_text.startswith((
            "# Netscape HTTP Cookie File",
            "# HTTP Cookie File",
        )):
            logger.warning("Invalid cookie-file format for %s.", platform)
            return None

        path = Path(folder) / f"{platform.lower()}_cookies.txt"
        path.write_text(cookie_text, encoding="utf-8")
        return path

    except Exception:
        logger.warning("Could not read optional cookies for %s.", platform)
        return None


# ============================================================
# FILE HELPERS
# ============================================================

def safe_filename(name):
    name = Path(unquote(name)).name
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)
    return name[:180] or "downloaded_file"


def response_filename(response, url):
    disposition = response.headers.get("Content-Disposition", "")

    if disposition:
        try:
            message = Message()
            message["content-disposition"] = disposition
            filename = message.get_filename()
            if filename:
                return safe_filename(filename)
        except Exception:
            pass

    name = Path(unquote(urlsplit(url).path)).name
    return safe_filename(name or "downloaded_file")


def find_downloaded_file(folder):
    candidates = []

    for path in Path(folder).rglob("*"):
        if not path.is_file():
            continue

        if path.name.lower().endswith((".part", ".ytdl", ".json")):
            continue

        if "cookie" in path.name.lower():
            continue

        try:
            if path.stat().st_size > 0:
                candidates.append(path)
        except OSError:
            pass

    if not candidates:
        return None

    return max(candidates, key=lambda item: item.stat().st_mtime)


# ============================================================
# DIRECT FILE DOWNLOADER
# ============================================================

def download_direct_file(url, folder, deadline):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    current_url = url

    try:
        response = None

        for _ in range(6):
            if time.monotonic() >= deadline:
                return None, "The download timed out."

            valid, error = validate_public_url(current_url)
            if not valid:
                return None, error

            response = session.get(
                current_url,
                stream=True,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()

                if not location:
                    return None, "The server returned an invalid redirect."

                current_url = urljoin(current_url, location)
                continue

            break
        else:
            return None, "Too many redirects."

        with response:
            if response.status_code == 403:
                return None, "The server denied the request."

            if response.status_code == 429:
                return None, "The server is rate-limiting requests."

            if response.status_code >= 400:
                return None, f"The server returned HTTP {response.status_code}."

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";")[0].strip().lower()
            )

            if content_type in ("text/html", "application/xhtml+xml"):
                return None, (
                    "This link opens a webpage, not a direct downloadable file."
                )

            content_length = response.headers.get("Content-Length", "")
            if content_length.isdigit() and int(content_length) > MAX_FILE_SIZE:
                return None, "The file exceeds the 45 MB limit."

            output = Path(folder) / response_filename(response, current_url)

            if output.exists():
                output = output.with_name(
                    f"{output.stem}_{int(time.time())}{output.suffix}"
                )

            total = 0

            with output.open("wb") as file_obj:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if time.monotonic() >= deadline:
                        output.unlink(missing_ok=True)
                        return None, "The download timed out."

                    if not chunk:
                        continue

                    total += len(chunk)

                    if total > MAX_FILE_SIZE:
                        output.unlink(missing_ok=True)
                        return None, "The file exceeds the 45 MB limit."

                    file_obj.write(chunk)

            if total == 0:
                output.unlink(missing_ok=True)
                return None, "The server returned an empty file."

            if not output.suffix and content_type:
                extension = mimetypes.guess_extension(content_type)
                if extension:
                    renamed = output.with_name(output.name + extension)
                    output.rename(renamed)
                    output = renamed

            return str(output), None

    except requests.Timeout:
        return None, "The server took too long to respond."
    except requests.RequestException:
        return None, "Could not retrieve the direct file."
    except OSError:
        return None, "Could not save the downloaded file."
    finally:
        session.close()


# ============================================================
# YT-DLP ERROR DIAGNOSTICS
# ============================================================

def classify_ytdlp_error(stderr, stdout, code, url):
    diagnostic = (stderr + "\n" + stdout).lower()
    platform = platform_for_url(url) or "OTHER"

    if any(word in diagnostic for word in (
        "sign in to confirm",
        "login required",
        "authentication required",
        "private video",
        "requested content is not available",
    )):
        return (
            "The platform requires authentication or valid cookies, "
            "or access is currently unavailable."
        )

    if any(word in diagnostic for word in (
        "javascript runtime",
        "javascript challenge",
        "challenge solving",
        "ejs",
    )):
        return (
            "YouTube's JavaScript challenge could not be solved. "
            "Check that Node.js and yt-dlp-ejs are installed on Railway."
        )

    if "429" in diagnostic or "too many requests" in diagnostic:
        return "The platform is rate-limiting requests."

    if "403" in diagnostic or "forbidden" in diagnostic:
        return "The platform denied the request."

    if any(word in diagnostic for word in (
        "file is larger than",
        "filesize exceeds",
        "maximum file size",
    )):
        return "The file exceeds the 45 MB limit."

    if "unsupported url" in diagnostic or "no suitable extractor" in diagnostic:
        return f"No supported downloader was found for this {platform} link."

    # Do not log full URLs or sensitive cookie/authentication data.
    safe_lines = [
        line.strip()[:200]
        for line in (stderr + "\n" + stdout).splitlines()
        if line.strip()
        and "cookie" not in line.lower()
        and "authorization" not in line.lower()
        and "https://" not in line.lower()
        and "http://" not in line.lower()
    ]

    logger.warning(
        "yt-dlp failure | platform=%s | exit=%s | details=%s",
        platform,
        code,
        " | ".join(safe_lines[-3:])[:600],
    )

    return (
        f"Could not download this {platform} link. "
        "It may be unsupported, restricted, or temporarily unavailable."
    )


# ============================================================
# SOCIAL MEDIA DOWNLOADER — YOUTUBE FIX INCLUDED
# ============================================================

def download_with_ytdlp(url, folder, deadline):
    platform = platform_for_url(url) or "OTHER"
    cookie_path = get_cookie_file(platform, folder)

    output_template = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")

    command = [
        sys.executable,
        "-m", "yt_dlp",
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout", "20",
        "--retries", "2",
        "--fragment-retries", "2",
        "--extractor-retries", "2",

        # YouTube JavaScript challenge support.
        # Node.js must be installed in the Railway environment.
        "--js-runtimes", "node",
        "--remote-components", "ejs:npm",

        "--max-filesize", "45M",

        # Prefer best available video and audio without re-encoding.
        "-f", "bestvideo*+bestaudio/best",
        "--merge-output-format", "mkv",

        "--output", output_template,
    ]

    if cookie_path:
        command.extend(["--cookies", str(cookie_path)])

    command.append(url)

    remaining = max(1, int(deadline - time.monotonic()))

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=min(remaining, DOWNLOAD_TIMEOUT),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "The social-media download timed out."
    except Exception:
        logger.exception("Could not start yt-dlp.")
        return None, "The social-media downloader could not start."

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    downloaded = find_downloaded_file(folder)

    if result.returncode == 0 and downloaded:
        size = downloaded.stat().st_size

        if size > MAX_FILE_SIZE:
            downloaded.unlink(missing_ok=True)
            return None, "The file exceeds the 45 MB limit."

        logger.info(
            "YTDLP SUCCESS | platform=%s | extension=%s | size=%d",
            platform,
            downloaded.suffix.lower() or "unknown",
            size,
        )
        return str(downloaded), None

    return None, classify_ytdlp_error(
        stderr, stdout, result.returncode, url
    )


# ============================================================
# GALLERY-DL FALLBACK
# ============================================================

def download_with_gallery_dl(url, folder, deadline):
    platform = platform_for_url(url) or "OTHER"
    cookie_path = get_cookie_file(platform, folder)

    command = [
        sys.executable,
        "-m", "gallery_dl",
        "--destination", folder,
        "--no-mtime",
    ]

    if cookie_path:
        command.extend(["--cookies", str(cookie_path)])

    command.append(url)

    remaining = max(1, int(deadline - time.monotonic()))

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=min(remaining, DOWNLOAD_TIMEOUT),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "The gallery download timed out."
    except Exception:
        logger.exception("Could not start gallery-dl.")
        return None, "The gallery downloader could not start."

    downloaded = find_downloaded_file(folder)

    if result.returncode == 0 and downloaded:
        if downloaded.stat().st_size > MAX_FILE_SIZE:
            downloaded.unlink(missing_ok=True)
            return None, "The file exceeds the 45 MB limit."

        return str(downloaded), None

    diagnostic = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()

    if "429" in diagnostic or "too many requests" in diagnostic:
        return None, "The platform is rate-limiting requests."

    if "403" in diagnostic or "forbidden" in diagnostic:
        return None, "The platform denied the request."

    return None, "The gallery downloader could not retrieve this post."


# ============================================================
# PLATFORM LIMITATIONS
# ============================================================

def platform_limitation(url):
    platform = platform_for_url(url)

    messages = {
        "WHATSAPP": (
            "WhatsApp chat, status and invite links are not necessarily "
            "direct media URLs. Send a direct file URL you can access."
        ),
        "MESSENGER": (
            "Messenger conversation links are not direct media files. "
            "Use a direct media URL you are authorized to access."
        ),
        "ARATTAI": (
            "Arattai may not have a supported public extractor. "
            "A direct media URL may be required."
        ),
        "SANDES": (
            "Sandes content may require authorized account access or "
            "a direct media URL."
        ),
        "KOO": "Koo links may be unavailable because the service was discontinued.",
    }

    return messages.get(platform)


# ============================================================
# DOWNLOAD ROUTER
# ============================================================

def download_media(url, folder, deadline):
    valid, error = validate_public_url(url)
    if not valid:
        return None, error

    platform = platform_for_url(url)

    if platform is None:
        return download_direct_file(url, folder, deadline)

    path, ytdlp_error = download_with_ytdlp(url, folder, deadline)
    if path:
        return path, None

    if time.monotonic() >= deadline:
        return None, "The download timed out."

    path, gallery_error = download_with_gallery_dl(url, folder, deadline)
    if path:
        return path, None

    limitation = platform_limitation(url)
    if limitation:
        return None, limitation

    return None, ytdlp_error or gallery_error or (
        "The platform could not provide a downloadable file."
    )


# ============================================================
# SEND DOWNLOADED FILE TO TELEGRAM
# ============================================================

async def send_media_file(message, filepath):
    path = Path(filepath)
    extension = path.suffix.lower()
    mime_type, _ = mimetypes.guess_type(path.name)
    mime_type = (mime_type or "").lower()

    if extension in (".jpg", ".jpeg", ".png"):
        try:
            with path.open("rb") as file_obj:
                await message.reply_photo(
                    photo=file_obj,
                    caption=path.name[:900],
                    read_timeout=60,
                    write_timeout=60,
                )
            return
        except TelegramError:
            pass

    if mime_type.startswith("audio/") or extension in (
        ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac"
    ):
        try:
            with path.open("rb") as file_obj:
                await message.reply_audio(
                    audio=file_obj,
                    filename=path.name,
                    read_timeout=60,
                    write_timeout=60,
                )
            return
        except TelegramError:
            pass

    if extension in (
        ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"
    ):
        try:
            with path.open("rb") as file_obj:
                await message.reply_video(
                    video=file_obj,
                    filename=path.name,
                    supports_streaming=True,
                    read_timeout=60,
                    write_timeout=60,
                )
            return
        except TelegramError:
            pass

    with path.open("rb") as file_obj:
        await message.reply_document(
            document=file_obj,
            filename=path.name,
            read_timeout=60,
            write_timeout=60,
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "👋 Welcome to Universal Media Downloader!\n\n"
            "Send a supported public social-media link or direct file URL.\n\n"
            "Platforms attempted: YouTube, Instagram, Facebook, Snapchat, "
            "X, LinkedIn, Telegram, Pinterest, Reddit, WhatsApp, Messenger, "
            "ShareChat, Moj, Josh, Chingari, Arattai, Sandes and Koo.\n\n"
            "Supports common image, audio, video, document and archive files.\n"
            "Maximum file size: 45 MB.\n"
            "Use /help for details."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "📥 HOW TO USE\n\n"
            "1. Send a supported public post link or direct file URL.\n"
            "2. Wait while the bot attempts the download.\n"
            "3. The bot returns the downloaded file.\n\n"
            "You can configure supported platform cookies in Railway Variables.\n"
            "YouTube downloads use yt-dlp JavaScript challenge support.\n\n"
            "Private posts, expired links, platform restrictions and server "
            "IP blocks can prevent downloads.\n\n"
            "Maximum file size: 45 MB. Only download content you are "
            "authorized to access."
        )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    urls = extract_urls(message)
    if not urls:
        return

    await message.reply_text(
        "🙏 Thanks for sharing! Your file is under process ⏳"
    )

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.TYPING,
        )
    except TelegramError:
        pass

    url = urls[0]
    started = time.monotonic()
    deadline = started + DOWNLOAD_TIMEOUT

    try:
        with tempfile.TemporaryDirectory(
            prefix="download_",
            dir=str(TEMP_ROOT),
        ) as folder:
            path, error = await asyncio.to_thread(
                download_media, url, folder, deadline
            )

            if not path:
                await message.reply_text(
                    "❌ " + (error or "Unable to download this link.")
                )
                return

            size = Path(path).stat().st_size
            if size > MAX_FILE_SIZE:
                await message.reply_text("❌ The file exceeds the 45 MB limit.")
                return

            try:
                await context.bot.send_chat_action(
                    chat_id=message.chat_id,
                    action=ChatAction.UPLOAD_DOCUMENT,
                )
            except TelegramError:
                pass

            await send_media_file(message, path)

            logger.info(
                "REQUEST SUCCESS | platform=%s | duration=%.1fs | size=%d",
                platform_for_url(url) or "DIRECT",
                time.monotonic() - started,
                size,
            )

    except TelegramError:
        logger.warning(
            "Telegram send failed | platform=%s",
            platform_for_url(url) or "DIRECT",
        )
    except Exception:
        logger.exception(
            "REQUEST FAILED | platform=%s",
            platform_for_url(url) or "DIRECT",
        )
        try:
            await message.reply_text(
                "❌ Something went wrong while processing this link."
            )
        except TelegramError:
            pass


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add it in Railway Variables."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Universal Media Downloader is starting.")

    application.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
