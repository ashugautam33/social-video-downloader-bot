
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

from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote

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

SOCIAL_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "twitter.com",
    "x.com",
    "reddit.com",
    "pinterest.com",
    "snapchat.com",
    "threads.net",
)

# Never enable HTTP debug logging: request URLs can contain secrets.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

for noisy_logger in (
    "httpx",
    "httpcore",
    "telegram",
    "telegram.ext",
    "urllib3",
):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger("media_bot")


# ============================================================
# URL HELPERS
# ============================================================

URL_PATTERN = re.compile(
    r"""https?://[^\s<>"']+""",
    re.IGNORECASE,
)


def clean_url(url):
    """Remove common punctuation accidentally included after a URL."""
    return url.strip().rstrip(".,!?;:)]}>'\"")


def extract_urls(message):
    """Extract links from message text, captions, and Telegram entities."""
    if not message:
        return []

    text = message.text or message.caption or ""
    found = [clean_url(match) for match in URL_PATTERN.findall(text)]

    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type == "url":
            start = entity.offset
            end = start + entity.length
            found.append(clean_url(text[start:end]))

        elif entity.type == "text_link" and entity.url:
            found.append(clean_url(entity.url))

    # Preserve order while removing duplicates.
    result = []
    seen = set()

    for url in found:
        if url and url not in seen:
            seen.add(url)
            result.append(url)

    return result


def platform_for_url(url):
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        return None

    if host == "youtu.be" or host.endswith("youtube.com"):
        return "YOUTUBE"

    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "INSTAGRAM"

    if host == "facebook.com" or host.endswith(".facebook.com"):
        return "FACEBOOK"

    if host == "fb.watch":
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
    """
    Reject non-HTTP links and URLs resolving to local/private IP addresses.
    This reduces the risk of the bot being used to access internal services.
    """
    try:
        parsed = urlsplit(url)

        if parsed.scheme not in ("http", "https"):
            return False, "Only HTTP and HTTPS links are supported."

        if not parsed.hostname:
            return False, "The link is invalid."

        if parsed.username or parsed.password:
            return False, "URLs containing embedded credentials are not supported."

        host = parsed.hostname.rstrip(".").lower()

        if host == "localhost" or host.endswith(".localhost"):
            return False, "Local network URLs are not supported."

        if host.endswith(".local"):
            return False, "Local network URLs are not supported."

        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                answers = socket.getaddrinfo(
                    host,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
                addresses = [
                    ipaddress.ip_address(answer[4][0])
                    for answer in answers
                ]
            except Exception:
                return False, "The host could not be resolved."

        if not addresses:
            return False, "The host could not be resolved."

        for address in addresses:
            if not address.is_global:
                return False, "Private or local network addresses are not supported."

        return True, None

    except Exception:
        return False, "The link is invalid."


# ============================================================
# COOKIE CONFIGURATION
# ============================================================

COOKIE_VARIABLES = {
    "YOUTUBE": "YOUTUBE_COOKIES_B64",
    "INSTAGRAM": "INSTAGRAM_COOKIES_B64",
    "FACEBOOK": "FACEBOOK_COOKIES_B64",
    "TIKTOK": "TIKTOK_COOKIES_B64",
}


def get_cookie_file(platform, folder):
    """
    Decode an optional Base64-encoded Netscape cookies.txt variable.

    Cookie values are never printed or logged.
    Returns (cookie_path, error_message).
    """
    variable = COOKIE_VARIABLES.get(platform)
    encoded = os.getenv(variable, "").strip() if variable else ""

    if not encoded:
        return None, None

    try:
        # Allow accidental whitespace/newlines in the Base64 variable.
        encoded = re.sub(r"\s+", "", encoded)
        cookie_bytes = base64.b64decode(encoded, validate=True)

        cookie_text = cookie_bytes.decode("utf-8-sig")

        if not (
            cookie_text.startswith("# Netscape HTTP Cookie File")
            or cookie_text.startswith("# HTTP Cookie File")
            or cookie_text.startswith("#")
        ):
            return None, (
                f"{variable} does not appear to contain a Netscape-format "
                "cookies.txt file."
            )

        cookie_path = Path(folder) / f"{platform.lower()}_cookies.txt"
        cookie_path.write_text(cookie_text, encoding="utf-8")

        return cookie_path, None

    except Exception:
        return None, (
            f"Cookie configuration error for {variable}. "
            "Check that it contains valid Base64-encoded Netscape cookies."
        )


# ============================================================
# DIRECT FILE DOWNLOAD
# ============================================================

def filename_from_response(response, url):
    """Choose a filename from Content-Disposition or the URL."""
    content_disposition = response.headers.get("Content-Disposition", "")

    if content_disposition:
        try:
            from email.message import Message

            message = Message()
            message["content-disposition"] = content_disposition
            filename = message.get_filename()

            if filename:
                filename = unquote(filename)
                filename = Path(filename).name
                filename = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", filename)

                if filename and filename not in (".", ".."):
                    return filename[:180]
        except Exception:
            pass

    try:
        name = Path(unquote(urlsplit(url).path)).name
        name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name)

        if name and name not in (".", ".."):
            return name[:180]
    except Exception:
        pass

    return "downloaded_file"


def download_direct_file(url, folder, deadline):
    """
    Download arbitrary direct HTTP/HTTPS files.

    HTML pages are rejected so a social-media webpage is not accidentally
    sent as if it were the actual video or image.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    current_url = url
    response = None

    try:
        for _ in range(6):
            if time.monotonic() > deadline:
                return None, "The download timed out."

            valid, reason = validate_public_url(current_url)
            if not valid:
                return None, reason

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

        if response is None:
            return None, "The server did not return a response."

        with response:
            if response.status_code >= 400:
                if response.status_code == 403:
                    return None, "The hosting server denied the request."

                if response.status_code == 429:
                    return None, "The hosting server is rate-limiting requests."

                return None, (
                    f"The hosting server returned HTTP {response.status_code}."
                )

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";")[0]
                .strip()
                .lower()
            )

            if content_type in (
                "text/html",
                "application/xhtml+xml",
            ):
                return None, (
                    "This link opened a webpage, not a direct downloadable file."
                )

            length = response.headers.get("Content-Length")

            if length:
                try:
                    if int(length) > MAX_FILE_SIZE:
                        return None, "The file exceeds the 45 MB limit."
                except ValueError:
                    pass

            filename = filename_from_response(response, current_url)
            output_path = Path(folder) / filename

            # Avoid overwriting a different file with the same name.
            if output_path.exists():
                output_path = output_path.with_name(
                    f"{output_path.stem}_{int(time.time())}{output_path.suffix}"
                )

            total = 0

            with output_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if time.monotonic() > deadline:
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

            # If the server supplied no extension, infer one from its MIME type.
            if not output_path.suffix and content_type:
                guessed = mimetypes.guess_extension(content_type)

                if guessed:
                    new_path = output_path.with_name(output_path.name + guessed)
                    output_path.rename(new_path)
                    output_path = new_path

            return str(output_path), None

    except requests.Timeout:
        return None, "The server took too long to respond."

    except requests.RequestException:
        return None, "Could not retrieve the direct file from the server."

    except OSError:
        return None, "Could not save the downloaded file."

    finally:
        session.close()


# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def find_downloaded_file(folder):
    """Find the downloaded file while ignoring cookie and metadata files."""
    candidates = []

    for path in Path(folder).rglob("*"):
        if not path.is_file():
            continue

        if path.name.endswith((".part", ".ytdl", ".json", ".txt")):
            continue

        if "cookies" in path.name.lower():
            continue

        try:
            if path.stat().st_size > 0:
                candidates.append(path)
        except OSError:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[0]


def download_with_ytdlp(url, folder, deadline):
    """
    Download a social-media link using yt-dlp.

    Returns (filepath, error_message).
    """
    platform = platform_for_url(url) or "OTHER"

    cookie_path, cookie_error = get_cookie_file(platform, folder)

    if cookie_error:
        logger.warning(
            "COOKIE CONFIGURATION ERROR | platform=%s | details=%s",
            platform,
            cookie_error,
        )
        return None, cookie_error

    output_template = str(Path(folder) / "%(title).80s-%(id)s.%(ext)s")

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-warnings",
        "--no-playlist",
        "--socket-timeout",
        "20",
        "--retries",
        "2",
        "--fragment-retries",
        "2",
        "--max-filesize",
        "45M",
        "--format",
        "bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format",
        "mp4",
        "--output",
        output_template,
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

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        code = result.returncode

    except subprocess.TimeoutExpired:
        return None, "The social-media download timed out."

    except Exception:
        logger.exception("Could not start yt-dlp process.")
        return None, "The downloader could not start."

    downloaded = find_downloaded_file(folder)

    if code == 0 and downloaded:
        try:
            if downloaded.stat().st_size > MAX_FILE_SIZE:
                downloaded.unlink(missing_ok=True)
                return None, "The file exceeds the 45 MB limit."
        except OSError:
            return None, "Could not inspect the downloaded file."

        logger.info(
            "YTDLP SUCCESS | platform=%s | extension=%s | size=%s",
            platform,
            downloaded.suffix.lower() or "unknown",
            downloaded.stat().st_size,
        )
        return str(downloaded), None

    # --------------------------------------------------------
    # DIAGNOSTIC HANDLING
    # --------------------------------------------------------

    diagnostic = (stderr + "\n" + stdout).lower()

    if "there is no video in this post" in diagnostic:
        return None, (
            "Instagram returned no video. The post may contain photos "
            "only, or Instagram may be restricting access."
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
        return None, (
            "The platform requires authentication, valid cookies, "
            "or access that is currently unavailable."
        )

    if "429" in diagnostic or "too many requests" in diagnostic:
        return None, "The hosting platform is rate-limiting requests."

    if "403" in diagnostic or "forbidden" in diagnostic:
        return None, (
            "The hosting platform denied the request. Check authentication "
            "and whether the host blocks Railway."
        )

    if any(word in diagnostic for word in (
        "file is larger than",
        "filesize exceeds",
        "maximum file size",
    )):
        return None, "The file exceeds the 45 MB limit."

    if "unsupported url" in diagnostic or "no suitable extractor" in diagnostic:
        return None, "This link format is not supported by yt-dlp."

    # Log only a short sanitized diagnostic; never log the URL or cookies.
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
        platform,
        code,
        " | ".join(safe_lines[-3:])[:700],
    )

    return None, (
        "Download failed. Check the latest yt-dlp diagnostic in Railway logs."
    )


# ============================================================
# GALLERY-DL FALLBACK
# ============================================================

def download_with_gallery_dl(url, folder, deadline):
    """Fallback for supported image galleries, especially Instagram."""
    platform = platform_for_url(url) or "OTHER"

    cookie_path, cookie_error = get_cookie_file(platform, folder)

    if cookie_error:
        return None, cookie_error

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--destination",
        folder,
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
        return None, "The gallery downloader could not start."

    downloaded = find_downloaded_file(folder)

    if result.returncode == 0 and downloaded:
        try:
            if downloaded.stat().st_size > MAX_FILE_SIZE:
                downloaded.unlink(missing_ok=True)
                return None, "The file exceeds the 45 MB limit."
        except OSError:
            return None, "Could not inspect the downloaded file."

        logger.info(
            "GALLERY-DL SUCCESS | platform=%s | extension=%s | size=%s",
            platform,
            downloaded.suffix.lower() or "unknown",
            downloaded.stat().st_size,
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
    valid, reason = validate_public_url(url)

    if not valid:
        return None, reason

    # Direct file URLs should be tried directly first.
    if not is_social_url(url):
        path, error = download_direct_file(url, folder, deadline)

        if path:
            logger.info(
                "DIRECT DOWNLOAD SUCCESS | extension=%s | size=%s",
                Path(path).suffix.lower() or "unknown",
                Path(path).stat().st_size,
            )
            return path, None

        return None, error or "Could not download this direct file."

    # Social-media URLs should be processed by their extractors first.
    path, ytdlp_error = download_with_ytdlp(url, folder, deadline)

    if path:
        return path, None

    if time.monotonic() >= deadline:
        return None, "The download timed out."

    # gallery-dl is useful for supported Instagram posts and galleries.
    if platform_for_url(url) == "INSTAGRAM":
        path, gallery_error = download_with_gallery_dl(
            url,
            folder,
            deadline,
        )

        if path:
            return path, None

    # This can succeed if the supplied URL redirects to an actual media file.
    # Normal social-media post pages are HTML and will be rejected.
    if time.monotonic() < deadline:
        path, direct_error = download_direct_file(url, folder, deadline)

        if path:
            return path, None

    if ytdlp_error:
        return None, ytdlp_error

    return None, "The platform could not provide a downloadable file."


# ============================================================
# SEND FILE TO TELEGRAM
# ============================================================

async def send_media_file(message, filepath):
    path = Path(filepath)
    extension = path.suffix.lower()
    mime_type, _ = mimetypes.guess_type(path.name)
    mime_type = (mime_type or "").lower()

    try:
        with path.open("rb") as file_obj:
            # Telegram photo uploads work best with common image formats.
            if extension in (".jpg", ".jpeg", ".png"):
                try:
                    await message.reply_photo(
                        photo=file_obj,
                        caption=path.name[:900],
                        read_timeout=60,
                        write_timeout=60,
                    )
                    return
                except TelegramError:
                    file_obj.seek(0)

            # Send audio files as audio where possible.
            if mime_type.startswith("audio/") or extension in (
                ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"
            ):
                try:
                    await message.reply_audio(
                        audio=file_obj,
                        filename=path.name,
                        read_timeout=60,
                        write_timeout=60,
                    )
                    return
                except TelegramError:
                    file_obj.seek(0)

            # Send common video formats as videos.
            if extension in (
                ".mp4", ".m4v", ".mov", ".webm"
            ):
                try:
                    await message.reply_video(
                        video=file_obj,
                        filename=path.name,
                        supports_streaming=True,
                        read_timeout=60,
                        write_timeout=60,
                    )
                    return
                except TelegramError:
                    file_obj.seek(0)

            # Documents are used for all other file types and fallbacks.
            await message.reply_document(
                document=file_obj,
                filename=path.name,
                read_timeout=60,
                write_timeout=60,
            )

    except TelegramError:
        logger.warning("Telegram could not send the downloaded file.")
        raise


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.message:
        await update.message.reply_text(
            "👋 Welcome to the Media Downloader Bot!\n\n"
            "Send me a supported social-media link or a direct file URL "
            "and I'll try to download it for you.\n\n"
            "Maximum file size: 45 MB\n"
            "Preferred video quality: up to 720p\n\n"
            "Use /help for more information."
        )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.message:
        await update.message.reply_text(
            "📥 Media Downloader Help\n\n"
            "Send a link to a supported public post or a direct file.\n\n"
            "Supported examples include YouTube, Instagram, Facebook, "
            "TikTok and direct HTTP/HTTPS downloads.\n\n"
            "Common documents, images, audio, videos, archives and other "
            "file types can be returned as Telegram documents when the "
            "server provides a direct downloadable file.\n\n"
            "Maximum file size: 45 MB.\n\n"
            "Private, expired, login-restricted or platform-blocked links "
            "may not work. Only download content you have permission to access."
        )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    urls = extract_urls(message)

    # Remain silent when a normal text message has no URL.
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

    # Process the first link in the message.
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
                await message.reply_text(
                    "❌ " + (error or "Unable to download this link.")
                )
                return

            file_size = Path(path).stat().st_size

            if file_size > MAX_FILE_SIZE:
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
                file_size,
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

    # Handle both normal text messages and media captions.
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
