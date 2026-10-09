
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
DIRECT_TIMEOUT = 12

ACK_MESSAGE = "🙏 Thanks for sharing! Your file is under process ⏳"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# Prevent HTTP logs from exposing Telegram API URLs and bot tokens.
for logger_name in (
    "httpx",
    "httpcore",
    "telegram",
    "telegram.ext",
):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

logger = logging.getLogger("universal_media_bot")

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

COOKIE_VARIABLES = {
    "youtube": "YOUTUBE_COOKIES_B64",
    "instagram": "INSTAGRAM_COOKIES_B64",
    "facebook": "FACEBOOK_COOKIES_B64",
    "tiktok": "TIKTOK_COOKIES_B64",
}

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)


class DownloadFailure(Exception):
    pass


# ============================================================
# URL HELPERS
# ============================================================

def clean_url(url: str) -> str:
    return url.strip().rstrip(".,!?;:)]}>")


def extract_urls(text: str) -> list[str]:
    if not text:
        return []

    results = []
    seen = set()

    for match in URL_PATTERN.findall(text):
        url = clean_url(match)
        if url not in seen:
            seen.add(url)
            results.append(url)

    return results


def platform_for_url(url: str) -> str | None:
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


def validate_public_url(url: str) -> None:
    """Allow public HTTP(S) links, not localhost or private IP addresses."""
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


def safe_filename(name: str, fallback: str = "download") -> str:
    name = Path(unquote(name or "")).name
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")

    if not name or name in (".", ".."):
        name = fallback

    return name[:150]


def list_downloads(folder: Path) -> list[Path]:
    """Find downloaded files without mistaking cookies for media."""
    results = []

    for path in folder.rglob("*"):
        if not path.is_file():
            continue

        if "_auth" in path.parts:
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
# OPTIONAL COOKIES
# ============================================================

def cookie_file_for(
    platform: str | None,
    folder: Path,
) -> Path | None:
    if not platform:
        return None

    variable = COOKIE_VARIABLES.get(platform)
    if not variable:
        return None

    value = os.getenv(variable, "").strip()
    if not value:
        return None

    try:
        if value.startswith(
            ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
        ):
            content = value
        else:
            decoded = base64.b64decode(
                "".join(value.split()),
                validate=True,
            )
            content = decoded.decode("utf-8-sig")

        if not content.startswith(
            ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
        ):
            raise ValueError("Invalid cookie-file format")

        rows = [
            line for line in content.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        if not rows:
            raise ValueError("No cookie entries")

        auth_folder = folder / "_auth"
        auth_folder.mkdir(exist_ok=True)

        path = auth_folder / f"{platform}_cookies.txt"
        path.write_text(content, encoding="utf-8")

        try:
            path.chmod(0o600)
        except OSError:
            pass

        return path

    except Exception:
        # Never print cookie values or contents.
        logger.warning("Ignoring invalid cookie variable: %s", variable)
        return None


# ============================================================
# SUBPROCESS RUNNER
# ============================================================

async def run_process(
    command: list[str],
    timeout_seconds: int,
) -> tuple[int, str, str]:
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
# YT-DLP SOCIAL MEDIA DOWNLOADER
# ============================================================

async def download_with_ytdlp(
    url: str,
    folder: Path,
    cookies: Path | None,
    timeout_seconds: int,
) -> tuple[Path | None, str]:
    output_template = str(folder / "%(title).80s-%(id)s.%(ext)s")

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
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

    # yt-dlp may print the final output path.
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
        return None, "INSTAGRAM_NO_VIDEO"

    if any(word in diagnostic for word in (
        "sign in to confirm",
        "login required",
        "authentication required",
        "private video",
    )):
        return None, "This content requires authentication or valid cookies."

    if "file is larger than" in diagnostic:
        return None, "The file exceeds the 45 MB limit."

    if any(word in diagnostic for word in (
        "unsupported url",
        "no suitable extractor",
    )):
        return None, "This link type is not supported by yt-dlp."

    # Do not print the URL or raw downloader diagnostics.
    logger.warning(
        "yt-dlp failed | platform=%s | exit=%s",
        platform_for_url(url) or "direct",
        code,
    )

    return None, "yt-dlp could not retrieve media from this link."


# ============================================================
# GALLERY-DL INSTAGRAM PHOTO/CAROUSEL FALLBACK
# ============================================================

async def download_with_gallery_dl(
    url: str,
    folder: Path,
    cookies: Path | None,
    timeout_seconds: int,
) -> tuple[Path | None, str]:
    gallery_folder = folder / "gallery"
    gallery_folder.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--destination",
        str(gallery_folder),
        "--no-mtime",
    ]

    if cookies:
        command.extend(["--cookies", str(cookies)])

    command.append(url)

    try:
        code, _stdout, _stderr = await run_process(
            command,
            timeout_seconds,
        )
    except DownloadFailure as exc:
        return None, str(exc)

    found = list_downloads(gallery_folder)
    if found:
        # Return the first downloaded item.
        return found[0], ""

    logger.info("gallery-dl found no media | exit=%s", code)
    return None, "The gallery downloader found no media."


# ============================================================
# DIRECT URL: DOWNLOAD ANY FILE TYPE
# SVG, PDF, ZIP, APK, DOCX, TXT, MP4, ETC.
# ============================================================

def download_direct_file(
    url: str,
    folder: Path,
    deadline: float,
) -> Path:
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
        # Check every redirect destination.
        for _ in range(6):
            validate_public_url(current_url)

            if time.monotonic() >= deadline:
                raise DownloadFailure("The download timed out.")

            response = session.get(
                current_url,
                stream=True,
                allow_redirects=False,
                timeout=(4, 5),
            )

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                response = None

                if not location:
                    raise DownloadFailure("Invalid redirect from the server.")

                current_url = urljoin(current_url, location)
                continue

            break
        else:
            raise DownloadFailure("Too many redirects.")

        if response is None:
            raise DownloadFailure("No response from the file server.")

        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).split(";")[0].strip().lower()

        # A webpage is not the same as a direct file URL.
        if content_type in (
            "text/html",
            "application/xhtml+xml",
        ):
            raise DownloadFailure(
                "This link opens a webpage, not a direct file URL."
            )

        content_length = response.headers.get("Content-Length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > MAX_FILE_BYTES
        ):
            raise DownloadFailure("The file exceeds the 45 MB limit.")

        # Prefer a filename supplied by the server.
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
        counter = 1

        while destination.exists():
            destination = folder / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
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
# DOWNLOAD ROUTER
# ============================================================

async def download_media(url: str, folder: Path) -> Path:
    validate_public_url(url)

    started = time.monotonic()
    deadline = started + MAX_PROCESS_SECONDS
    platform = platform_for_url(url)
    cookies = cookie_file_for(platform, folder)

    # 1. Try yt-dlp for social links and supported media URLs.
    remaining = max(1, int(deadline - time.monotonic()))

    path, reason = await download_with_ytdlp(
        url,
        folder,
        cookies,
        min(YTDLP_TIMEOUT, remaining),
    )

    if path:
        return path

    # 2. Try gallery-dl for Instagram photos and carousels.
    if platform == "instagram" and time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))

        path, _gallery_reason = await download_with_gallery_dl(
            url,
            folder,
            cookies,
            min(GALLERY_TIMEOUT, remaining),
        )

        if path:
            return path

    # 3. Try as a direct URL to any file type.
    if time.monotonic() < deadline:
        try:
            return await asyncio.to_thread(
                download_direct_file,
                url,
                folder,
                min(deadline, time.monotonic() + DIRECT_TIMEOUT),
            )
        except DownloadFailure as exc:
            direct_reason = str(exc)
    else:
        direct_reason = "Processing timed out."

    if reason == "INSTAGRAM_NO_VIDEO":
        raise DownloadFailure(
            "Instagram did not provide a video. This may be a photo post, "
            "or access may be restricted. The photo downloader also failed. "
            "Try a public post or Reel that is accessible without login."
        )

    if "authentication" in reason.lower():
        raise DownloadFailure(
            "This content requires authentication. Check that the relevant "
            "Railway cookie variable contains valid Netscape-format cookies."
        )

    if "45 MB" in reason or "45 MB" in direct_reason:
        raise DownloadFailure("The file exceeds the 45 MB limit.")

    if time.monotonic() - started >= MAX_PROCESS_SECONDS:
        raise DownloadFailure("Processing timed out. Please try again.")

    raise DownloadFailure(
        "Unable to download this link. It may be restricted, expired, "
        "unsupported, or not a direct file URL."
    )


# ============================================================
# SEND FILE TO TELEGRAM
# ============================================================

async def send_media_file(message, path: Path) -> None:
    size = path.stat().st_size

    if size <= 0:
        raise DownloadFailure("The downloaded file is empty.")

    if size > MAX_FILE_BYTES:
        raise DownloadFailure("The file exceeds the 45 MB limit.")

    mime_type, _ = mimetypes.guess_type(path.name)
    extension = path.suffix.lower()

    with path.open("rb") as media:
        # Send common image formats as photos.
        if extension in (".jpg", ".jpeg", ".png"):
            await message.reply_photo(photo=media)

        # Send audio files as audio.
        elif mime_type and mime_type.startswith("audio/"):
            await message.reply_audio(
                audio=media,
                filename=path.name,
            )

        # Send common video formats with Telegram video preview.
        elif extension in (".mp4", ".m4v", ".mov", ".webm"):
            await message.reply_video(
                video=media,
                filename=path.name,
                supports_streaming=(extension == ".mp4"),
            )

        # SVG, GIF, PDF, ZIP, APK, DOCX, TXT, and other file types.
        else:
            await message.reply_document(
                document=media,
                filename=path.name,
            )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if message:
        await message.reply_text(
            "👋 Welcome to Universal Media Downloader!\n\n"
            "Send a supported public media link or a direct file URL.\n"
            "Use /help for instructions."
        )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if message:
        await message.reply_text(
            "📥 Universal Media Downloader\n\n"
            "• Send a public social-media link or direct file URL.\n"
            "• Supports direct files such as SVG, PDF, ZIP, APK and DOCX.\n"
            "• Videos are preferred at up to 720p when available.\n"
            "• Maximum file size: 45 MB.\n"
            "• Processing limit: approximately 120 seconds.\n\n"
            "Private, expired, unsupported, or login-protected links may fail."
        )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message

    if not message or not message.text:
        return

    urls = extract_urls(message.text)

    # Keep ordinary text messages silent.
    if not urls:
        return

    url = urls[0]
    status = await message.reply_text(ACK_MESSAGE)

    try:
        validate_public_url(url)
    except DownloadFailure:
        await status.edit_text(
            "❌ Please send a valid public HTTP or HTTPS link."
        )
        return

    async with DOWNLOAD_SEMAPHORE:
        try:
            await context.bot.send_chat_action(
                chat_id=message.chat_id,
                action=ChatAction.TYPING,
            )

            with tempfile.TemporaryDirectory(
                prefix="universal_media_"
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
                platform_for_url(url) or "direct",
                str(exc),
            )

            await status.edit_text(f"❌ {exc}")

        except Exception as exc:
            # Never log URLs, bot tokens, or cookies.
            logger.error(
                "Request failed | exception_type=%s",
                type(exc).__name__,
            )

            try:
                await status.edit_text(
                    "❌ Something went wrong. Please try another public link."
                )
            except Exception:
                pass


# ============================================================
# START BOT
# ============================================================

async def post_init(application: Application) -> None:
    logger.info("Universal Media Downloader started.")


def main() -> None:
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
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    # Run only one active polling instance for this bot token.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
