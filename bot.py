
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
from urllib.parse import unquote, urljoin, urlparse

import requests
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

MAX_FILE_BYTES = 45 * 1024 * 1024
MAX_PROCESS_SECONDS = 112
YTDLP_TIMEOUT = 65
GALLERY_TIMEOUT = 35
DIRECT_TIMEOUT = 20

ACK_MESSAGE = "🙏 Thanks for sharing! Your file is under process ⏳"

COOKIE_VARIABLES = {
    "youtube": "YOUTUBE_COOKIES_B64",
    "instagram": "INSTAGRAM_COOKIES_B64",
    "facebook": "FACEBOOK_COOKIES_B64",
    "tiktok": "TIKTOK_COOKIES_B64",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# Prevent Telegram API URLs, which can contain the bot token,
# from appearing in HTTP client logs.
for name in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(name).setLevel(logging.WARNING)

logger = logging.getLogger("universal_media_bot")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)


class DownloadFailure(Exception):
    pass


# ============================================================
# URL AND FILE HELPERS
# ============================================================

def clean_url(url):
    return url.strip().rstrip(".,!?;:)]}>")


def extract_urls(message):
    """Extract visible URLs and clickable hidden Telegram links."""
    urls = []
    seen = set()

    text = message.text or message.caption or ""

    for match in URL_PATTERN.findall(text):
        url = clean_url(match)
        if url and url not in seen:
            urls.append(url)
            seen.add(url)

    entities = list(message.entities or []) + list(
        message.caption_entities or []
    )

    for entity in entities:
        url = getattr(entity, "url", None)
        if url and url not in seen:
            urls.append(clean_url(url))
            seen.add(url)

    return urls


def platform_for_url(url):
    host = (urlparse(url).hostname or "").lower()

    if host == "youtu.be" or host.endswith(
        ("youtube.com", "youtube-nocookie.com")
    ):
        return "youtube"

    if host.endswith("instagram.com"):
        return "instagram"

    if host.endswith(("facebook.com", "fb.watch")):
        return "facebook"

    if host.endswith("tiktok.com"):
        return "tiktok"

    return None


def validate_public_url(url):
    """Reject invalid schemes and private/local destinations."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DownloadFailure("Please send a valid HTTP or HTTPS link.")

    host = parsed.hostname.lower()

    if host in ("localhost", "localhost.localdomain"):
        raise DownloadFailure("Local addresses are not allowed.")

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, None)
            ]
        except (socket.gaierror, ValueError):
            raise DownloadFailure("The link's host could not be reached.")

    if any(not address.is_global for address in addresses):
        raise DownloadFailure("Private or local addresses are not allowed.")


def safe_filename(name, fallback="download"):
    name = Path(unquote(name or "")).name
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")

    if not name or name in (".", ".."):
        name = fallback

    return name[:150]


def list_downloads(folder):
    """Find completed files and exclude authentication files."""
    results = []

    for path in folder.rglob("*"):
        if not path.is_file() or "_auth" in path.parts:
            continue

        if path.name.endswith((".part", ".ytdl", ".tmp")):
            continue

        try:
            size = path.stat().st_size
        except OSError:
            continue

        if size <= 0:
            continue

        if size > MAX_FILE_BYTES:
            path.unlink(missing_ok=True)
            continue

        results.append(path)

    return sorted(
        results,
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )


# ============================================================
# OPTIONAL PLATFORM COOKIES
# ============================================================

def cookie_file_for(platform, folder):
    if not platform:
        return None

    variable = COOKIE_VARIABLES.get(platform)
    value = os.getenv(variable, "").strip() if variable else ""

    if not value:
        return None

    try:
        if value.startswith(
            ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
        ):
            content = value
        else:
            raw = base64.b64decode(
                "".join(value.split()),
                validate=True,
            )
            content = raw.decode("utf-8-sig")

        if not content.startswith(
            ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
        ):
            raise ValueError("Invalid Netscape cookie format")

        entries = [
            line for line in content.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        if not entries:
            raise ValueError("No cookie entries")

        auth_folder = folder / "_auth"
        auth_folder.mkdir(exist_ok=True)

        cookie_path = auth_folder / f"{platform}_cookies.txt"
        cookie_path.write_text(content, encoding="utf-8")

        try:
            cookie_path.chmod(0o600)
        except OSError:
            pass

        return cookie_path

    except Exception:
        # Never log cookie contents.
        logger.warning("Ignoring invalid cookie variable: %s", variable)
        return None


# ============================================================
# SUBPROCESS WITH TIMEOUT
# ============================================================

async def run_process(command, timeout_seconds):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass

        await process.communicate()
        raise DownloadFailure("The download timed out.")

    return (
        process.returncode if process.returncode is not None else 1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


# ============================================================
# DIRECT DOWNLOAD: ANY FILE TYPE
# ============================================================

def download_direct_file(url, folder, deadline):
    """
    Download arbitrary files from direct URLs.

    Supports documents, images, archives, code, audio, video,
    design files and other non-HTML file types.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/132.0 Safari/537.36"
        ),
        "Accept": "*/*",
    })

    response = None
    current_url = url

    try:
        # Follow redirects manually and validate each destination.
        for _ in range(6):
            validate_public_url(current_url)

            if time.monotonic() >= deadline:
                raise DownloadFailure("The download timed out.")

            response = session.get(
                current_url,
                stream=True,
                allow_redirects=False,
                timeout=(5, 8),
            )

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                response = None

                if not location:
                    raise DownloadFailure("The server returned an invalid redirect.")

                current_url = urljoin(current_url, location)
                continue

            break
        else:
            raise DownloadFailure("Too many redirects.")

        if response is None:
            raise DownloadFailure("The file server did not respond.")

        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).split(";")[0].strip().lower()

        # A webpage is not a direct file.
        if content_type in ("text/html", "application/xhtml+xml"):
            raise DownloadFailure(
                "This URL opens a webpage rather than a direct file. "
                "Please share the actual file-download URL."
            )

        content_length = response.headers.get("Content-Length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > MAX_FILE_BYTES
        ):
            raise DownloadFailure("The file exceeds the 45 MB limit.")

        # Prefer the server's Content-Disposition filename.
        filename = None
        disposition = response.headers.get("Content-Disposition", "")

        if disposition:
            try:
                header = Message()
                header["Content-Disposition"] = disposition
                filename = header.get_filename()
            except Exception:
                filename = None

        if not filename:
            filename = Path(urlparse(current_url).path).name

        filename = safe_filename(filename, "download")

        if "." not in filename:
            extension = mimetypes.guess_extension(content_type) or ""
            filename += extension

        destination = folder / filename
        stem, suffix = destination.stem, destination.suffix
        counter = 1

        while destination.exists():
            destination = folder / f"{stem}_{counter}{suffix}"
            counter += 1

        total = 0

        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if time.monotonic() >= deadline:
                    raise DownloadFailure("The download timed out.")

                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_FILE_BYTES:
                    raise DownloadFailure("The file exceeds the 45 MB limit.")

                output.write(chunk)

        if total == 0:
            destination.unlink(missing_ok=True)
            raise DownloadFailure("The server returned an empty file.")

        return destination

    except requests.RequestException:
        raise DownloadFailure("Unable to access the direct file URL.")

    finally:
        if response is not None:
            response.close()

        session.close()


# ============================================================
# YT-DLP: SOCIAL AND SUPPORTED MEDIA
# ============================================================

async def download_with_ytdlp(url, folder, cookies, timeout_seconds):
    output_template = str(folder / "%(title).80s-%(id)s.%(ext)s")

    command = [
        sys.executable,
        "-m", "yt_dlp",
        "--no-warnings",
        "--no-playlist",
        "--no-progress",
        "--no-part",
        "--socket-timeout", "10",
        "--retries", "1",
        "--fragment-retries", "1",
        "--extractor-retries", "1",
        "--max-filesize", str(MAX_FILE_BYTES),
        "-f", "bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format", "mp4",
        "-o", output_template,
        "--print", "after_move:filepath",
    ]

    if cookies:
        command.extend(["--cookies", str(cookies)])

    command.extend(["--", url])

    try:
        code, stdout, stderr = await run_process(
            command,
            timeout_seconds,
        )
    except DownloadFailure as exc:
        return None, str(exc)

    for line in reversed(stdout.splitlines()):
        candidate = Path(line.strip())
        if candidate.is_file():
            if candidate.stat().st_size <= MAX_FILE_BYTES:
                return candidate, ""
            return None, "The file exceeds the 45 MB limit."

    found = list_downloads(folder)
    if found:
        return found[0], ""

    diagnostic = (stderr + "\n" + stdout).lower()

    if "there is no video in this post" in diagnostic:
        return None, "Instagram did not expose a video for this post."

    if any(word in diagnostic for word in (
        "sign in to confirm",
        "login required",
        "authentication required",
        "private video",
    )):
        return None, "This content requires authentication or valid cookies."

    if "file is larger than" in diagnostic:
        return None, "The file exceeds the 45 MB limit."

    logger.warning(
        "yt-dlp failed | platform=%s | exit=%s",
        platform_for_url(url) or "other",
        code,
    )

    return None, "The media extractor could not download this link."


# ============================================================
# GALLERY-DL: INSTAGRAM PHOTO POSTS/CAROUSELS
# ============================================================

async def download_with_gallery_dl(url, folder, cookies, timeout_seconds):
    gallery_folder = folder / "gallery"
    gallery_folder.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m", "gallery_dl",
        "-d", str(gallery_folder),
        "--no-mtime",
    ]

    if cookies:
        command.extend(["--cookies", str(cookies)])

    command.append(url)

    try:
        code, stdout, stderr = await run_process(
            command,
            timeout_seconds,
        )
    except DownloadFailure as exc:
        return None, str(exc)

    found = list_downloads(gallery_folder)
    if found:
        return found[0], ""

    diagnostic = (stderr + "\n" + stdout).lower()

    if "429" in diagnostic or "too many requests" in diagnostic:
        reason = "Instagram is rate-limiting requests."
    elif "login" in diagnostic or "authentication" in diagnostic:
        reason = "Instagram requires valid login cookies."
    elif "checkpoint" in diagnostic or "challenge_required" in diagnostic:
        reason = "Instagram requires an account security check."
    elif "private" in diagnostic or "not found" in diagnostic:
        reason = "The Instagram post may be private or unavailable."
    else:
        reason = f"The Instagram photo downloader failed (exit {code})."

    logger.warning("gallery-dl failed | reason=%s", reason)
    return None, reason


# ============================================================
# DOWNLOAD ROUTER
# ============================================================

async def download_media(url, folder):
    validate_public_url(url)

    started = time.monotonic()
    deadline = started + MAX_PROCESS_SECONDS

    platform = platform_for_url(url)
    cookies = cookie_file_for(platform, folder)

    # For ordinary file URLs, attempt direct download FIRST.
    # This avoids asking yt-dlp to interpret PDFs, ZIPs, SVGs, etc.
    if platform is None:
        try:
            return await asyncio.to_thread(
                download_direct_file,
                url,
                folder,
                min(deadline, time.monotonic() + DIRECT_TIMEOUT),
            )
        except DownloadFailure as direct_error:
            logger.info(
                "Direct download failed | reason=%s",
                str(direct_error),
            )

        # If the URL is a webpage supported by yt-dlp, try extraction.
        remaining = max(1, int(deadline - time.monotonic()))
        path, reason = await download_with_ytdlp(
            url,
            folder,
            cookies,
            min(YTDLP_TIMEOUT, remaining),
        )

        if path:
            return path

        raise DownloadFailure(
            "Unable to retrieve this file. The link may be a webpage "
            "instead of a direct file URL, or the host may require login."
        )

    # Social links: use the appropriate media extractor first.
    remaining = max(1, int(deadline - time.monotonic()))
    path, reason = await download_with_ytdlp(
        url,
        folder,
        cookies,
        min(YTDLP_TIMEOUT, remaining),
    )

    if path:
        return path

    # Instagram photo/carousel fallback.
    if platform == "instagram" and time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        path, gallery_reason = await download_with_gallery_dl(
            url,
            folder,
            cookies,
            min(GALLERY_TIMEOUT, remaining),
        )

        if path:
            return path

        logger.info("Instagram fallback result: %s", gallery_reason)

    # Last attempt: the social URL might redirect directly to a file.
    if time.monotonic() < deadline:
        try:
            return await asyncio.to_thread(
                download_direct_file,
                url,
                folder,
                min(deadline, time.monotonic() + DIRECT_TIMEOUT),
            )
        except DownloadFailure:
            pass

    if "authentication" in reason.lower():
        raise DownloadFailure(
            "This content requires authentication. Check the relevant "
            "cookie variable in Railway."
        )

    if time.monotonic() - started >= MAX_PROCESS_SECONDS:
        raise DownloadFailure("Processing timed out. Please try again.")

    raise DownloadFailure(
        "Unable to download this social-media link. It may be private, "
        "expired, unsupported, or blocked by the hosting site."
    )


# ============================================================
# SEND FILE TO TELEGRAM
# ============================================================

async def send_media_file(message, path):
    size = path.stat().st_size

    if size <= 0:
        raise DownloadFailure("The downloaded file is empty.")

    if size > MAX_FILE_BYTES:
        raise DownloadFailure("The file exceeds the 45 MB limit.")

    mime_type, _ = mimetypes.guess_type(path.name)
    extension = path.suffix.lower()

    with path.open("rb") as media:
        # Send common photos as images.
        if extension in (".jpg", ".jpeg", ".png"):
            await message.reply_photo(photo=media)

        # Audio files.
        elif mime_type and mime_type.startswith("audio/"):
            await message.reply_audio(
                audio=media,
                filename=path.name,
            )

        # Common video files, with Telegram preview.
        elif extension in (".mp4", ".m4v", ".mov", ".webm"):
            await message.reply_video(
                video=media,
                filename=path.name,
                supports_streaming=(extension == ".mp4"),
            )

        # All other files: SVG, GIF, PSD, RAW, PDF, ZIP,
        # RAR, APK, DOCX, XLSX, PPTX, code, JSON, etc.
        else:
            await message.reply_document(
                document=media,
                filename=path.name,
            )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message:
        await message.reply_text(
            "👋 Welcome to Universal File Downloader!\n\n"
            "Send a direct file link or a supported public social-media link.\n"
            "Use /help for supported file types."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message:
        await message.reply_text(
            "📥 Universal File Downloader\n\n"
            "Documents: DOCX, XLSX, PPTX, TXT, CSV, RTF\n"
            "PDF: PDF documents and e-books\n"
            "Images: JPG, PNG, GIF, SVG, PSD, TIFF, RAW, WebP\n"
            "Audio/video: MP3, WAV, M4A, MP4, MOV, MKV, WebM\n"
            "Archives: ZIP, RAR, 7Z, TAR, GZ\n"
            "Code/data: PY, JS, HTML, CSS, JSON, XML, SQL\n\n"
            "Maximum file size: 45 MB\n"
            "Preferred video quality: up to 720p\n\n"
            "Share a direct file URL for documents and archives. "
            "Private or login-protected links may fail."
        )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if not message:
        return

    urls = extract_urls(message)

    # No link: stay silent.
    if not urls:
        return

    url = urls[0]
    status = await message.reply_text(ACK_MESSAGE)

    try:
        validate_public_url(url)
    except DownloadFailure:
        await status.edit_text("❌ Please send a valid public HTTP/HTTPS link.")
        return

    async with DOWNLOAD_SEMAPHORE:
        try:
            await context.bot.send_chat_action(
                chat_id=message.chat_id,
                action=ChatAction.TYPING,
            )

            with tempfile.TemporaryDirectory(
                prefix="universal_file_bot_"
            ) as temp:
                folder = Path(temp)

                path = await asyncio.wait_for(
                    download_media(url, folder),
                    timeout=MAX_PROCESS_SECONDS + 3,
                )

                await context.bot.send_chat_action(
                    chat_id=message.chat_id,
                    action=ChatAction.UPLOAD_DOCUMENT,
                )

                await send_media_file(message, path)

            try:
                await status.delete()
            except Exception:
                pass

        except asyncio.TimeoutError:
            await status.edit_text(
                "⏱️ Processing timed out. Please try another link."
            )

        except DownloadFailure as exc:
            logger.info(
                "Download failed | platform=%s | reason=%s",
                platform_for_url(url) or "direct file",
                str(exc),
            )

            await status.edit_text(f"❌ {exc}")

        except Exception as exc:
            # Do not log URLs, tokens, or cookies.
            logger.error(
                "Request failed | exception_type=%s",
                type(exc).__name__,
            )

            try:
                await status.edit_text(
                    "❌ Something went wrong. Please try another link."
                )
            except Exception:
                pass


# ============================================================
# STARTUP
# ============================================================

async def post_init(application: Application):
    logger.info("Universal File Downloader started.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it in Railway Variables."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )
    application.add_handler(
        CommandHandler("help", help_command)
    )
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_message,
        )
    )

    # Keep only one active polling deployment per bot token.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
