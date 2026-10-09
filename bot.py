
import asyncio
import base64
import ipaddress
import logging
import mimetypes
import os
import re
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

TEMP_ROOT = Path(tempfile.gettempdir()) / "telegram_media_bot"
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
    "TIKTOK": "TIKTOK_COOKIES_B64",
}

URL_PATTERN = re.compile(r"""https?://[^\s<>"']+""", re.IGNORECASE)

SOCIAL_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "reddit.com",
    "pinterest.com",
    "snapchat.com",
    "threads.net",
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

for name in ("httpx", "httpcore", "telegram", "telegram.ext", "urllib3"):
    logging.getLogger(name).setLevel(logging.WARNING)

logger = logging.getLogger("media_bot")


# ============================================================
# URL HELPERS
# ============================================================

def clean_url(url):
    return url.strip().rstrip(".,!?;:)]}>'\"")


def extract_urls(message):
    if not message:
        return []

    text = message.text or message.caption or ""
    urls = [clean_url(x) for x in URL_PATTERN.findall(text)]

    entities = message.entities or message.caption_entities or []

    for entity in entities:
        if entity.type == "url":
            urls.append(
                clean_url(text[entity.offset:entity.offset + entity.length])
            )
        elif entity.type == "text_link" and entity.url:
            urls.append(clean_url(entity.url))

    result = []
    seen = set()

    for url in urls:
        if url and url not in seen:
            seen.add(url)
            result.append(url)

    return result


def platform_for_url(url):
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        return None

    if host == "youtu.be" or host.endswith(".youtube.com") or host == "youtube.com":
        return "YOUTUBE"

    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "INSTAGRAM"

    if host in ("facebook.com", "fb.watch") or host.endswith(".facebook.com"):
        return "FACEBOOK"

    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "TIKTOK"

    if host == "x.com" or host.endswith(".x.com"):
        return "X"

    if host == "twitter.com" or host.endswith(".twitter.com"):
        return "X"

    if host == "reddit.com" or host.endswith(".reddit.com"):
        return "REDDIT"

    if host == "pinterest.com" or host.endswith(".pinterest.com"):
        return "PINTEREST"

    if host == "threads.net" or host.endswith(".threads.net"):
        return "THREADS"

    return None


def is_social_url(url):
    return platform_for_url(url) is not None


def validate_public_url(url):
    """Reject invalid schemes and private/local IP addresses."""
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
            answers = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = [
                ipaddress.ip_address(answer[4][0]) for answer in answers
            ]

        if not addresses or any(not address.is_global for address in addresses):
            return False, "Private or local network addresses are not supported."

        return True, None

    except Exception:
        return False, "The link is invalid or its host cannot be resolved."


# ============================================================
# OPTIONAL COOKIE FILES
# ============================================================

def get_cookie_file(platform, folder):
    """
    Decode an optional Base64 Netscape cookies.txt file.

    Returns:
        (cookie_path, None) when valid or not configured.
        (None, error_message) when configured incorrectly.
    """
    variable = COOKIE_VARIABLES.get(platform)
    encoded = os.getenv(variable, "").strip() if variable else ""

    if not encoded:
        return None, None

    try:
        # Permit line breaks and spaces in a copied Base64 value.
        encoded = re.sub(r"\s+", "", encoded)
        cookie_bytes = base64.b64decode(encoded, validate=True)
        cookie_text = cookie_bytes.decode("utf-8-sig")

        valid_header = (
            cookie_text.startswith("# Netscape HTTP Cookie File")
            or cookie_text.startswith("# HTTP Cookie File")
        )

        if not valid_header:
            return None, (
                f"{variable} is not a Netscape-format cookies.txt file."
            )

        cookie_path = Path(folder) / f"{platform.lower()}_cookies.txt"
        cookie_path.write_text(cookie_text, encoding="utf-8")

        return cookie_path, None

    except Exception:
        return None, (
            f"Invalid {variable}. Use Base64-encoded Netscape cookies.txt "
            "or remove this optional Railway variable."
        )


def cookie_for_attempt(platform, folder):
    """
    Invalid optional cookies are logged without revealing their contents.
    The download is allowed to continue without cookies.
    """
    cookie_path, error = get_cookie_file(platform, folder)

    if error:
        logger.warning(
            "COOKIE CONFIGURATION ERROR | platform=%s | details=%s",
            platform or "OTHER",
            error,
        )
        return None, error

    return cookie_path, None


# ============================================================
# FILE HELPERS
# ============================================================

def find_downloaded_file(folder):
    candidates = []

    for path in Path(folder).rglob("*"):
        if not path.is_file():
            continue

        if path.name.endswith((".part", ".ytdl", ".json", ".txt")):
            continue

        if "cookie" in path.name.lower():
            continue

        try:
            if 0 < path.stat().st_size:
                candidates.append(path)
        except OSError:
            pass

    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def safe_filename(name):
    name = Path(unquote(name)).name
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)
    return name[:180] or "downloaded_file"


def filename_from_response(response, url):
    disposition = response.headers.get("Content-Disposition", "")

    if disposition:
        try:
            message = Message()
            message["content-disposition"] = disposition
            name = message.get_filename()

            if name:
                return safe_filename(name)
        except Exception:
            pass

    name = Path(unquote(urlsplit(url).path)).name
    return safe_filename(name or "downloaded_file")


# ============================================================
# DIRECT FILE DOWNLOAD
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
                return None, "The hosting server denied the request."

            if response.status_code == 429:
                return None, "The hosting server is rate-limiting requests."

            if response.status_code >= 400:
                return None, f"The hosting server returned HTTP {response.status_code}."

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";")[0]
                .strip()
                .lower()
            )

            if content_type in ("text/html", "application/xhtml+xml"):
                return None, (
                    "This URL opened a webpage rather than a direct file."
                )

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_FILE_SIZE:
                        return None, "The file exceeds the 45 MB limit."
                except ValueError:
                    pass

            output_path = Path(folder) / filename_from_response(
                response, current_url
            )

            if output_path.exists():
                output_path = output_path.with_name(
                    f"{output_path.stem}_{int(time.time())}{output_path.suffix}"
                )

            total = 0

            with output_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if time.monotonic() >= deadline:
                        output_path.unlink(missing_ok=True)
                        return None, "The download timed out."

                    if not chunk:
                        continue

                    total += len(chunk)

                    if total > MAX_FILE_SIZE:
                        output_path.unlink(missing_ok=True)
                        return None, "The file exceeds the 45 MB limit."

                    output.write(chunk)

            if total == 0:
                output_path.unlink(missing_ok=True)
                return None, "The server returned an empty file."

            if not output_path.suffix and content_type:
                extension = mimetypes.guess_extension(content_type)
                if extension:
                    new_path = output_path.with_name(output_path.name + extension)
                    output_path.rename(new_path)
                    output_path = new_path

            logger.info(
                "DIRECT DOWNLOAD SUCCESS | extension=%s | size=%s",
                output_path.suffix.lower() or "unknown",
                total,
            )

            return str(output_path), None

    except requests.Timeout:
        return None, "The server took too long to respond."
    except requests.RequestException:
        return None, "Could not retrieve the direct file."
    except OSError:
        return None, "Could not save the downloaded file."
    finally:
        session.close()


# ============================================================
# YT-DLP DIAGNOSTICS
# ============================================================

def classify_ytdlp_error(stderr, stdout, code, url):
    diagnostic = (stderr + "\n" + stdout).lower()

    if "there is no video in this post" in diagnostic:
        return (
            "Instagram returned no video. The post may contain photos only, "
            "or Instagram may be restricting access."
        )

    if any(word in diagnostic for word in (
        "sign in to confirm",
        "login required",
        "authentication required",
        "private video",
        "rate-limit reached",
        "requested content is not available",
        "login required to access",
    )):
        return (
            "The platform requires authentication, valid cookies, "
            "or access that is currently unavailable."
        )

    if "429" in diagnostic or "too many requests" in diagnostic:
        return "The hosting platform is rate-limiting requests."

    if "403" in diagnostic or "forbidden" in diagnostic:
        return (
            "The hosting platform denied the request. Check authentication "
            "and whether the host blocks Railway."
        )

    if any(word in diagnostic for word in (
        "file is larger than",
        "filesize exceeds",
        "maximum file size",
    )):
        return "The file exceeds the 45 MB limit."

    if "unsupported url" in diagnostic or "no suitable extractor" in diagnostic:
        return "This link format is not supported by yt-dlp."

    # Do not log URLs, cookie values, authorization headers, or signed links.
    safe_lines = [
        line.strip()[:250]
        for line in (stderr + "\n" + stdout).splitlines()
        if line.strip()
        and "cookie" not in line.lower()
        and "authorization" not in line.lower()
        and "https://" not in line.lower()
        and "http://" not in line.lower()
    ]

    logger.warning(
        "yt-dlp failure | platform=%s | exit=%s | details=%s",
        platform_for_url(url) or "OTHER",
        code,
        " | ".join(safe_lines[-3:])[:700],
    )

    return "Download failed. Check the latest yt-dlp diagnostic in Railway logs."


# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def download_with_ytdlp(url, folder, deadline):
    platform = platform_for_url(url) or "OTHER"

    cookie_path, cookie_error = cookie_for_attempt(platform, folder)

    # Crucial fix: invalid optional cookies no longer abort the download.
    # If cookie_path is None, yt-dlp attempts to access the link anonymously.

    output_template = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")

    command = [
        sys.executable,
        "-m", "yt_dlp",
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout", "20",
        "--retries", "2",
        "--fragment-retries", "2",
        "--max-filesize", "45M",
        "--format", "bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format", "mp4",
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
        return None, "The downloader could not start."

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    code = result.returncode

    downloaded = find_downloaded_file(folder)

    if code == 0 and downloaded:
        size = downloaded.stat().st_size

        if size > MAX_FILE_SIZE:
            downloaded.unlink(missing_ok=True)
            return None, "The file exceeds the 45 MB limit."

        logger.info(
            "YTDLP SUCCESS | platform=%s | extension=%s | size=%s",
            platform,
            downloaded.suffix.lower() or "unknown",
            size,
        )
        return str(downloaded), None

    error = classify_ytdlp_error(stderr, stdout, code, url)

    # When cookie configuration is invalid, we already tried anonymously.
    # Return the actual download error instead of the cookie error.
    return None, error


# ============================================================
# GALLERY-DL FALLBACK
# ============================================================

def download_with_gallery_dl(url, folder, deadline):
    platform = platform_for_url(url) or "OTHER"

    cookie_path, _ = cookie_for_attempt(platform, folder)

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
        size = downloaded.stat().st_size

        if size > MAX_FILE_SIZE:
            downloaded.unlink(missing_ok=True)
            return None, "The file exceeds the 45 MB limit."

        logger.info(
            "GALLERY-DL SUCCESS | platform=%s | extension=%s | size=%s",
            platform,
            downloaded.suffix.lower() or "unknown",
            size,
        )
        return str(downloaded), None

    diagnostic = ((result.stderr or "") + "\n" + (result.stdout or "")).lower()

    if "429" in diagnostic or "too many requests" in diagnostic:
        return None, "The hosting platform is rate-limiting requests."

    if "403" in diagnostic or "forbidden" in diagnostic:
        return None, "The hosting platform denied the request."

    safe_lines = [
        line.strip()[:200]
        for line in ((result.stderr or "") + "\n" + (result.stdout or "")).splitlines()
        if line.strip()
        and "cookie" not in line.lower()
        and "authorization" not in line.lower()
        and "https://" not in line.lower()
        and "http://" not in line.lower()
    ]

    logger.warning(
        "gallery-dl failure | platform=%s | exit=%s | details=%s",
        platform,
        result.returncode,
        " | ".join(safe_lines[-2:])[:400],
    )

    return None, "The gallery downloader could not retrieve this post."


# ============================================================
# DOWNLOAD ROUTER
# ============================================================

def download_media(url, folder, deadline):
    valid, error = validate_public_url(url)

    if not valid:
        return None, error

    if not is_social_url(url):
        return download_direct_file(url, folder, deadline)

    path, ytdlp_error = download_with_ytdlp(url, folder, deadline)

    if path:
        return path, None

    if time.monotonic() >= deadline:
        return None, "The download timed out."

    if platform_for_url(url) == "INSTAGRAM":
        path, _ = download_with_gallery_dl(url, folder, deadline)

        if path:
            return path, None

    # A social-media post URL usually returns HTML, not the actual media file.
    if time.monotonic() < deadline:
        path, _ = download_direct_file(url, folder, deadline)

        if path:
            return path, None

    return None, ytdlp_error or "The platform could not provide a downloadable file."


# ============================================================
# TELEGRAM FILE SENDING
# ============================================================

async def send_media_file(message, filepath):
    path = Path(filepath)
    extension = path.suffix.lower()
    mime_type, _ = mimetypes.guess_type(path.name)
    mime_type = (mime_type or "").lower()

    # Common photos
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

    # Audio
    if mime_type.startswith("audio/") or extension in (
        ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"
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

    # Video
    if extension in (".mp4", ".m4v", ".mov", ".webm"):
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

    # Everything else: documents, archives, other images, etc.
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
            "👋 Welcome to the Media Downloader Bot!\n\n"
            "Send a supported social-media link or a direct file URL.\n\n"
            "Supported examples: YouTube, Instagram, Facebook, TikTok "
            "and direct HTTP/HTTPS file links.\n\n"
            "Maximum file size: 45 MB\n"
            "Preferred video quality: up to 720p\n\n"
            "Use /help for instructions."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "📥 How to use this bot\n\n"
            "1. Send a supported public media link.\n"
            "2. Wait while the bot processes it.\n"
            "3. The bot sends the downloaded file back to you.\n\n"
            "Direct file links may include PDFs, documents, images, audio, "
            "videos, archives and other downloadable file types.\n\n"
            "Maximum file size: 45 MB.\n\n"
            "Private, expired, login-restricted or platform-blocked links "
            "may not work. Only download content you have permission to access."
        )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message:
        return

    urls = extract_urls(message)

    # Do not reply to ordinary messages without URLs.
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

    # Process the first URL in the message.
    url = urls[0]
    started = time.monotonic()
    deadline = started + DOWNLOAD_TIMEOUT

    try:
        with tempfile.TemporaryDirectory(
            prefix="media_",
            dir=str(TEMP_ROOT),
        ) as folder:
            path, error = await asyncio.to_thread(
                download_media,
                url,
                folder,
                deadline,
            )

            if not path:
                await message.reply_text("❌ " + (
                    error or "Unable to download this link."
                ))
                return

            size = Path(path).stat().st_size

            if size > MAX_FILE_SIZE:
                await message.reply_text(
                    "❌ The file exceeds the 45 MB limit."
                )
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
                "REQUEST SUCCESS | platform=%s | duration=%.1fs | size=%s",
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
                "❌ Something went wrong while processing this link. "
                "Please try again later."
            )
        except TelegramError:
            pass


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it in Railway Variables."
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

    logger.info("Media downloader bot is starting.")

    application.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
