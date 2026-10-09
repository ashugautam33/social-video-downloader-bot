
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
from pathlib import Path
from urllib.parse import (
    unquote,
    urljoin,
    urlparse,
)

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

# HTTP libraries can include the Telegram bot token in request URLs.
# Do not enable their INFO/DEBUG request logging.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

logger = logging.getLogger("universal_media_bot")

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

PLATFORM_COOKIE_VARS = {
    "youtube": "YOUTUBE_COOKIES_B64",
    "instagram": "INSTAGRAM_COOKIES_B64",
    "facebook": "FACEBOOK_COOKIES_B64",
    "tiktok": "TIKTOK_COOKIES_B64",
}

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)


class DownloadFailure(Exception):
    """Safe-to-display download failure."""


# ============================================================
# BASIC HELPERS
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

    if host.endswith("facebook.com") or host.endswith("fb.watch"):
        return "facebook"

    if host.endswith("tiktok.com"):
        return "tiktok"

    return None


def validate_public_url(url: str) -> None:
    """Allow public HTTP(S) URLs only; reject localhost/private hosts."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise DownloadFailure("Only HTTP and HTTPS links are supported.")

    host = parsed.hostname

    if not host:
        raise DownloadFailure("This link does not contain a valid hostname.")

    if host.lower() in ("localhost", "localhost.localdomain"):
        raise DownloadFailure("Local addresses cannot be downloaded.")

    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise DownloadFailure("Private or local addresses are not allowed.")
    except ValueError:
        # Hostname rather than a literal IP address.
        try:
            addresses = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise DownloadFailure("The link's host could not be reached.")

        for address in addresses:
            resolved_ip = ipaddress.ip_address(address[4][0])
            if not resolved_ip.is_global:
                raise DownloadFailure("The link points to a private address.")


def safe_filename(name: str, fallback: str = "download") -> str:
    name = unquote(name or "")
    name = Path(name).name
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")

    if not name or name in (".", ".."):
        name = fallback

    return name[:150]


def cookie_file_for(
    platform: str | None,
    folder: Path,
) -> Path | None:
    """Read valid Netscape cookies; ignore malformed optional variables."""
    if not platform:
        return None

    variable = PLATFORM_COOKIE_VARS.get(platform)
    if not variable:
        return None

    value = os.getenv(variable, "").strip()
    if not value:
        return None

    try:
        # Permit a raw Netscape cookie file as well as Base64.
        if value.startswith(
            ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
        ):
            content = value
        else:
            compact = "".join(value.split())
            decoded = base64.b64decode(compact, validate=True)
            content = decoded.decode("utf-8-sig")

        if not content.startswith(
            ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
        ):
            raise ValueError("Not a Netscape-format cookies file")

        # A header-only file is not useful; don't pass it to the downloader.
        cookie_rows = [
            line for line in content.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not cookie_rows:
            raise ValueError("The cookie file contains no cookie entries")

        path = folder / f"{platform}_cookies.txt"
        path.write_text(content, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

        return path

    except Exception:
        # Do not log the variable value or cookie contents.
        logger.warning(
            "Ignoring invalid optional cookie configuration: %s",
            variable,
        )
        return None


def media_files(folder: Path) -> list[Path]:
    found = []

    for path in folder.rglob("*"):
        if not path.is_file():
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
            try:
                path.unlink()
            except OSError:
                pass
            continue

        found.append(path)

    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


# ============================================================
# SUBPROCESS HELPER
# ============================================================

async def run_process(
    command: list[str],
    timeout_seconds: int,
) -> tuple[int, str, str]:
    """Run a downloader with a real process timeout."""
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
        raise DownloadFailure(
            f"Download exceeded the {timeout_seconds}-second time limit."
        )

    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


# ============================================================
# YT-DLP: SOCIAL VIDEO/AUDIO AND SUPPORTED MEDIA
# ============================================================

async def download_with_ytdlp(
    url: str,
    folder: Path,
    cookie_file: Path | None,
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
        "--no-simulate",
        "--restrict-filenames",
        "--socket-timeout",
        "12",
        "--retries",
        "1",
        "--fragment-retries",
        "1",
        "--extractor-retries",
        "1",
        "--max-filesize",
        f"{MAX_FILE_BYTES // (1024 * 1024)}M",
        "-f",
        "bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format",
        "mp4",
        "-o",
        output_template,
        "--print",
        "after_move:filepath",
    ]

    if cookie_file:
        command.extend(["--cookies", str(cookie_file)])

    command.extend(["--", url])

    try:
        return_code, stdout, stderr = await run_process(
            command,
            timeout_seconds,
        )
    except DownloadFailure as exc:
        return None, str(exc)

    # Prefer the path printed by yt-dlp; also check the output directory.
    printed_paths = [
        Path(line.strip())
        for line in stdout.splitlines()
        if line.strip()
    ]

    for path in reversed(printed_paths):
        if path.is_file():
            if path.stat().st_size <= MAX_FILE_BYTES:
                return path, ""
            return None, "The downloaded file exceeds the 45 MB limit."

    files = media_files(folder)
    if files:
        return files[0], ""

    # Return a limited, non-secret diagnostic for error classification.
    diagnostic = (stderr + "\n" + stdout).lower()

    if "there is no video in this post" in diagnostic:
        return None, "INSTAGRAM_POST_HAS_NO_VIDEO"

    if any(term in diagnostic for term in (
        "sign in to confirm",
        "login required",
        "authentication required",
        "private video",
    )):
        return None, "This content requires login or valid cookies."

    if any(term in diagnostic for term in (
        "unsupported url",
        "no suitable extractor",
    )):
        return None, "This site or link type is not supported by yt-dlp."

    if "file is larger than" in diagnostic:
        return None, "The media exceeds the 45 MB limit."

    # Never log the complete URL or raw downloader output.
    logger.warning(
        "yt-dlp failed | platform=%s | exit_code=%s",
        platform_for_url(url) or "direct",
        return_code,
    )

    return None, "yt-dlp could not retrieve media from this link."


# ============================================================
# GALLERY-DL: INSTAGRAM PHOTO POSTS/CAROUSELS
# ============================================================

async def download_with_gallery_dl(
    url: str,
    folder: Path,
    cookie_file: Path | None,
    timeout_seconds: int,
) -> tuple[Path | None, str]:
    gallery_folder = folder / "gallery"
    gallery_folder.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--dest",
        str(gallery_folder),
        "--no-mtime",
    ]

    if cookie_file:
        command.extend(["--cookies", str(cookie_file)])

    command.append(url)

    try:
        return_code, _stdout, _stderr = await run_process(
            command,
            timeout_seconds,
        )
    except DownloadFailure as exc:
        return None, str(exc)

    files = media_files(gallery_folder)
    if files:
        return files[0], ""

    logger.info(
        "gallery-dl did not return media | platform=%s | exit_code=%s",
        platform_for_url(url) or "unknown",
        return_code,
    )

    return None, "Instagram photo download was unsuccessful."


# ============================================================
# DIRECT HTTP FILE DOWNLOAD
# ============================================================

def download_direct_file(
    url: str,
    folder: Path,
    deadline: float,
) -> Path:
    current_url = url
    response = None

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/132.0 Safari/537.36"
        ),
        "Accept": "*/*",
    })

    try:
        # Handle redirects manually so every destination is checked.
        for _ in range(6):
            validate_public_url(current_url)

            if time.monotonic() >= deadline:
                raise DownloadFailure("Direct download timed out.")

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
                    raise DownloadFailure(
                        "The media server returned an invalid redirect."
                    )

                current_url = urljoin(current_url, location)
                continue

            break
        else:
            raise DownloadFailure("Too many redirects from the media server.")

        if response is None:
            raise DownloadFailure("The media server did not respond.")

        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).split(";")[0].strip().lower()

        if content_type in (
            "text/html",
            "application/xhtml+xml",
            "text/plain",
        ):
            raise DownloadFailure(
                "This link opens a webpage, not a direct media file."
            )

        allowed_prefixes = ("video/", "audio/", "image/")
        allowed_types = {
            "application/octet-stream",
            "application/mp4",
            "application/ogg",
        }

        if (
            not content_type.startswith(allowed_prefixes)
            and content_type not in allowed_types
        ):
            raise DownloadFailure(
                "The link does not appear to point to a media file."
            )

        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_FILE_BYTES:
            raise DownloadFailure("The media exceeds the 45 MB limit.")

        parsed_path = urlparse(current_url).path
        filename = safe_filename(Path(parsed_path).name, "media")
        if "." not in filename:
            extension = mimetypes.guess_extension(content_type) or ".bin"
            filename += extension

        destination = folder / filename
        stem = destination.stem
        suffix = destination.suffix
        counter = 1

        while destination.exists():
            destination = folder / f"{stem}_{counter}{suffix}"
            counter += 1

        total = 0

        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if time.monotonic() >= deadline:
                    raise DownloadFailure("Direct download timed out.")

                if not chunk:
                    continue

                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise DownloadFailure("The media exceeds the 45 MB limit.")

                output.write(chunk)

        if total == 0:
            destination.unlink(missing_ok=True)
            raise DownloadFailure("The media server returned an empty file.")

        return destination

    except requests.RequestException:
        raise DownloadFailure(
            "The direct media server could not be reached."
        )
    finally:
        if response is not None:
            response.close()
        session.close()


# ============================================================
# MAIN DOWNLOAD ROUTER
# ============================================================

async def download_media(url: str, folder: Path) -> Path:
    validate_public_url(url)

    started = time.monotonic()
    deadline = started + MAX_PROCESS_SECONDS
    platform = platform_for_url(url)
    cookies = cookie_file_for(platform, folder)

    # First: use yt-dlp for supported social and media URLs.
    remaining = max(1, int(deadline - time.monotonic()))
    ytdlp_limit = min(YTDLP_TIMEOUT, remaining)

    media_path, ytdlp_reason = await download_with_ytdlp(
        url,
        folder,
        cookies,
        ytdlp_limit,
    )

    if media_path:
        logger.info(
            "Download succeeded | platform=%s | size=%s",
            platform or "direct",
            media_path.stat().st_size,
        )
        return media_path

    # Second: Instagram photo posts and carousels.
    if platform == "instagram" and time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        gallery_limit = min(GALLERY_TIMEOUT, remaining)

        gallery_path, _gallery_reason = await download_with_gallery_dl(
            url,
            folder,
            cookies,
            gallery_limit,
        )

        if gallery_path:
            return gallery_path

    # Third: try the URL as a direct media-file URL.
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
        direct_reason = "The processing deadline was reached."

    if ytdlp_reason == "INSTAGRAM_POST_HAS_NO_VIDEO":
        raise DownloadFailure(
            "Instagram did not provide a video for this post. "
            "It may be a photo post, or Instagram may be restricting access. "
            "The photo downloader also could not retrieve its media."
        )

    if "requires login" in ytdlp_reason.lower():
        raise DownloadFailure(
            "This media requires authentication or valid cookies. "
            "Check the platform's cookie variable in Railway."
        )

    if "45 mb" in ytdlp_reason.lower() or "45 MB" in direct_reason:
        raise DownloadFailure("The media exceeds the 45 MB limit.")

    if time.monotonic() - started >= MAX_PROCESS_SECONDS:
        raise DownloadFailure("Processing exceeded the time limit. Try again.")

    raise DownloadFailure(
        "Unable to download this link. It may be restricted, expired, "
        "unsupported, or not a direct media URL."
    )


# ============================================================
# TELEGRAM SEND HELPERS
# ============================================================

async def send_media_file(message, path: Path) -> None:
    size = path.stat().st_size

    if size <= 0:
        raise DownloadFailure("The downloaded file is empty.")

    if size > MAX_FILE_BYTES:
        raise DownloadFailure("The media exceeds the 45 MB limit.")

    mime_type, _ = mimetypes.guess_type(path.name)
    extension = path.suffix.lower()

    with path.open("rb") as media:
        if mime_type and mime_type.startswith("image/"):
            # Telegram photo previews work best for supported image types.
            if extension in (".jpg", ".jpeg", ".png", ".webp"):
                await message.reply_photo(photo=media)
            else:
                await message.reply_document(
                    document=media,
                    filename=path.name,
                )

        elif mime_type and mime_type.startswith("audio/"):
            await message.reply_audio(
                audio=media,
                filename=path.name,
            )

        elif extension in (".mp4", ".m4v", ".mov", ".webm"):
            await message.reply_video(
                video=media,
                filename=path.name,
                supports_streaming=(extension == ".mp4"),
            )

        else:
            await message.reply_document(
                document=media,
                filename=path.name,
            )


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "👋 Welcome!\n\n"
            "Send a supported public video, audio, image, or media link "
            "to download it.\n\n"
            "Use /help to see how to use this bot."
        )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "📥 Universal Media Downloader\n\n"
            "1. Send a supported public media link.\n"
            "2. Wait while the bot processes it.\n"
            "3. Save the returned file to your device.\n\n"
            "Supported links depend on the source site's access rules. "
            "Private, expired, login-protected, or blocked media may fail.\n\n"
            "Maximum file size: 45 MB.\n"
            "Preferred video quality: up to 720p."
        )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    urls = extract_urls(message.text)

    # Requirement: remain silent when ordinary text has no link.
    if not urls:
        return

    # One link per message to keep resource use predictable.
    url = urls[0]

    try:
        validate_public_url(url)
    except DownloadFailure:
        status = await message.reply_text(ACK_MESSAGE)
        await status.edit_text(
            "❌ Please send a valid public HTTP or HTTPS link."
        )
        return

    status = await message.reply_text(ACK_MESSAGE)

    async with DOWNLOAD_SEMAPHORE:
        try:
            await context.bot.send_chat_action(
                chat_id=message.chat_id,
                action=ChatAction.TYPING,
            )

            with tempfile.TemporaryDirectory(prefix="media_bot_") as temp:
                folder = Path(temp)

                media_path = await asyncio.wait_for(
                    download_media(url, folder),
                    timeout=MAX_PROCESS_SECONDS + 2,
                )

                await context.bot.send_chat_action(
                    chat_id=message.chat_id,
                    action=ChatAction.UPLOAD_VIDEO,
                )

                await send_media_file(message, media_path)

            # Remove the acknowledgment after a successful upload.
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
            # Log exception type only. Avoid logging URLs, tokens, or cookies.
            logger.error(
                "Request failed | error_type=%s",
                type(exc).__name__,
            )
            try:
                await status.edit_text(
                    "❌ Something went wrong while processing this link. "
                    "Please try again with another public link."
                )
            except Exception:
                pass


async def post_init(application: Application) -> None:
    logger.info("Universal media bot started.")


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

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    # Polling only: deploy a single active instance of this bot.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
