
import os
import re
import sys
import base64
import socket
import ipaddress
import asyncio
import logging
import tempfile
import subprocess
import mimetypes

from pathlib import Path
from urllib.parse import urlparse, unquote, urljoin

import requests
import yt_dlp
import instaloader

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

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "45"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "200"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
MAX_REDIRECTS = 5
MAX_CAROUSEL_FILES = int(os.getenv("MAX_CAROUSEL_FILES", "10"))

MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("media_downloader")

# ============================================================
# FILE TYPES
# ============================================================

MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v",
    ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
    ".pdf", ".doc", ".docx", ".txt", ".rtf",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".apk", ".ipa", ".exe", ".dmg",
    ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
    ".svg", ".heic", ".heif",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".m4v",
}

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac",
}

# ============================================================
# URL HELPERS
# ============================================================

def extract_url(text):
    """Extract the first HTTP or HTTPS URL from a message."""
    if not text:
        return None

    match = re.search(
        r"https?://[^\s<>\"']+",
        text.strip(),
        re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(0).rstrip(".,!?)]}>'\"")


def validate_public_url(url):
    """Reject local/private network destinations."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise RuntimeError("Only HTTP and HTTPS links are supported.")

    host = parsed.hostname

    if not host:
        raise RuntimeError("The URL has no valid hostname.")

    if host.lower() == "localhost" or host.lower().endswith(".local"):
        raise RuntimeError("Local network URLs are not supported.")

    try:
        addresses = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise RuntimeError("Could not resolve the link hostname.") from exc

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError as exc:
            raise RuntimeError("Invalid IP address.") from exc

        if not ip.is_global:
            raise RuntimeError(
                "Links to private or local network addresses are blocked."
            )

    return parsed


def safe_stream_request(url):
    """Download direct files while validating redirects."""
    current_url = url

    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current_url)

        response = requests.get(
            current_url,
            stream=True,
            allow_redirects=False,
            timeout=(15, DOWNLOAD_TIMEOUT),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
                )
            },
        )

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()

            if not location:
                raise RuntimeError("Invalid redirect response.")

            current_url = urljoin(current_url, location)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("The URL redirected too many times.")


# ============================================================
# DIRECT FILE DOWNLOAD
# ============================================================

def detect_extension(url, content_type=None, disposition=None):
    """Find a filename extension from headers or URL."""
    if disposition:
        match = re.search(
            r'filename\*?=(?:UTF-8\'\')?"?([^";]+)',
            disposition,
            re.IGNORECASE,
        )

        if match:
            filename = unquote(match.group(1).strip().strip('"'))
            extension = Path(filename).suffix.lower()

            if extension:
                return extension

    extension = Path(urlparse(url).path).suffix.lower()

    if extension in MEDIA_EXTENSIONS:
        return extension

    if content_type:
        guessed = mimetypes.guess_extension(
            content_type.split(";")[0].strip().lower()
        )

        if guessed:
            return guessed.lower()

    return ".bin"


def download_direct_file(url, folder):
    """Download a direct file URL. Return None for HTML pages."""
    response = safe_stream_request(url)

    try:
        content_type = response.headers.get("Content-Type", "").lower()

        if "text/html" in content_type:
            return None

        content_length = response.headers.get("Content-Length")

        if content_length:
            try:
                if int(content_length) > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"File exceeds {MAX_DOWNLOAD_MB} MB."
                    )
            except ValueError:
                pass

        extension = detect_extension(
            url,
            content_type,
            response.headers.get("Content-Disposition"),
        )

        filename = unquote(Path(urlparse(url).path).name)

        filename = re.sub(
            r'[<>:"/\\|?*\x00-\x1f]',
            "_",
            filename,
        )[:150]

        if not filename:
            filename = "download" + extension
        elif not Path(filename).suffix:
            filename += extension

        destination = folder / filename
        total = 0

        with destination.open("wb") as output:
            for chunk in response.iter_content(128 * 1024):
                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"File exceeds {MAX_DOWNLOAD_MB} MB."
                    )

                output.write(chunk)

        if not destination.exists() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise RuntimeError("The downloaded file is empty.")

        with destination.open("rb") as file:
            header = file.read(512).lstrip().lower()

        if (
            header.startswith(b"<!doctype html")
            or header.startswith(b"<html")
            or (
                header.startswith(b"{")
                and "json" in content_type
            )
        ):
            destination.unlink(missing_ok=True)
            return None

        return destination

    finally:
        response.close()


# ============================================================
# COOKIE MANAGEMENT
# ============================================================

def cookie_environment(host):
    """Return the environment variable associated with a platform."""
    host = (host or "").lower()

    mappings = [
        ("youtube.com", "YOUTUBE_COOKIES_B64"),
        ("youtu.be", "YOUTUBE_COOKIES_B64"),
        ("instagram.com", "INSTAGRAM_COOKIES_B64"),
        ("facebook.com", "FACEBOOK_COOKIES_B64"),
        ("fb.watch", "FACEBOOK_COOKIES_B64"),
        ("tiktok.com", "TIKTOK_COOKIES_B64"),
        ("twitter.com", "X_COOKIES_B64"),
        ("x.com", "X_COOKIES_B64"),
        ("reddit.com", "REDDIT_COOKIES_B64"),
        ("linkedin.com", "LINKEDIN_COOKIES_B64"),
        ("snapchat.com", "SNAPCHAT_COOKIES_B64"),
    ]

    for domain, variable in mappings:
        if host == domain or host.endswith("." + domain):
            return variable

    return "COOKIES_B64"


def create_cookie_file(host, folder):
    """
    Cookie priority for Instagram:
    1. INSTAGRAM_COOKIES_PATH
    2. /data/cookies.txt on Railway Volume
    3. INSTAGRAM_COOKIES_B64
    4. Local cookies.txt
    """
    host = (host or "").lower()
    folder = Path(folder)

    is_instagram = (
        host == "instagram.com"
        or host.endswith(".instagram.com")
    )

    def valid_cookie_file(path):
        path = Path(path)

        if not path.is_file() or path.stat().st_size == 0:
            return False

        if not is_instagram:
            return True

        try:
            with path.open(
                "r",
                encoding="utf-8-sig",
                errors="strict",
            ) as handle:
                for line in handle:
                    if line.startswith("#HttpOnly_"):
                        line = line[len("#HttpOnly_"):]

                    if not line or line.startswith("#"):
                        continue

                    fields = line.rstrip("\r\n").split("\t")

                    if (
                        len(fields) >= 7
                        and fields[5] == "sessionid"
                        and fields[6].strip()
                    ):
                        return True

        except (OSError, UnicodeError):
            return False

        return False

    configured_path = os.getenv(
        "INSTAGRAM_COOKIES_PATH",
        "",
    ).strip()

    if is_instagram and configured_path:
        candidate = Path(configured_path)

        if valid_cookie_file(candidate):
            log.info("Loaded Instagram cookies from configured path.")
            return candidate.resolve()

        log.warning("INSTAGRAM_COOKIES_PATH is missing or invalid.")

    if is_instagram:
        volume_cookie = Path("/data/cookies.txt")

        if valid_cookie_file(volume_cookie):
            log.info("Loaded Instagram cookies from Railway Volume.")
            return volume_cookie

    variable = cookie_environment(host)
    encoded = os.getenv(variable, "").strip()

    if encoded:
        try:
            decoded = base64.b64decode(encoded, validate=True)

            if not decoded.strip():
                raise ValueError("Cookie file is empty.")

            if is_instagram:
                text = decoded.decode("utf-8-sig", errors="strict")
                has_session = False

                for line in text.splitlines():
                    if line.startswith("#HttpOnly_"):
                        line = line[len("#HttpOnly_"):]

                    if not line or line.startswith("#"):
                        continue

                    fields = line.split("\t")

                    if (
                        len(fields) >= 7
                        and fields[5] == "sessionid"
                        and fields[6].strip()
                    ):
                        has_session = True
                        break

                if not has_session:
                    raise ValueError(
                        "Instagram sessionid cookie is missing."
                    )

            cookie_path = folder / f"{variable.lower()}_cookies.txt"
            cookie_path.write_bytes(decoded)

            log.info("Loaded cookies from %s.", variable)
            return cookie_path

        except Exception as exc:
            log.warning(
                "Could not load %s: %s",
                variable,
                str(exc)[:200],
            )

    local_cookie = Path("cookies.txt")

    if valid_cookie_file(local_cookie):
        log.info("Using local cookies.txt.")
        return local_cookie.resolve()

    if is_instagram:
        log.warning("No valid Instagram cookies found. Check /data/cookies.txt.")

    return None


# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def ytdlp_download(url, folder):
    """Download media from sites supported by yt-dlp."""
    host = (urlparse(url).hostname or "").lower()

    existing_files = {
        str(path.resolve())
        for path in folder.rglob("*")
        if path.is_file()
    }

    cookie_file = create_cookie_file(host, folder)

    options = {
        "outtmpl": str(
            folder / "%(title).80B [%(id)s].%(ext)s"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": DOWNLOAD_TIMEOUT,
        "continuedl": True,
        "overwrites": True,
        "ignoreerrors": False,
        "max_filesize": MAX_DOWNLOAD_BYTES,
    }

    if cookie_file and Path(cookie_file).is_file():
        options["cookiefile"] = str(cookie_file)

    selectors = [
        "bestvideo*+bestaudio/best",
        "best[height<=720]/best",
    ]

    errors = []

    for selector in selectors:
        attempt_options = dict(options)
        attempt_options["format"] = selector

        try:
            with yt_dlp.YoutubeDL(attempt_options) as ydl:
                info = ydl.extract_info(url, download=True)

            candidates = []

            for path in folder.rglob("*"):
                if not path.is_file():
                    continue

                if str(path.resolve()) in existing_files:
                    continue

                if path.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue

                if path.name.endswith((".part", ".ytdl")):
                    continue

                if path.name == "cookies.txt":
                    continue

                if path.stat().st_size <= 0:
                    continue

                candidates.append(path)

            if candidates:
                candidates.sort(
                    key=lambda item: item.stat().st_size,
                    reverse=True,
                )

                title = (
                    info.get("title")
                    if isinstance(info, dict)
                    else None
                )

                return [candidates[0]], title or candidates[0].stem

            errors.append(
                f"Format {selector!r} produced no usable file."
            )

        except Exception as exc:
            errors.append(str(exc)[:500])
            log.warning(
                "yt-dlp failed for %s: %s",
                host,
                str(exc)[:500],
            )

    raise RuntimeError(
        "yt-dlp could not download this media. "
        + " | ".join(errors[-2:])
    )


# ============================================================
# INSTAGRAM GALLERY-DL FALLBACK
# ============================================================

def instagram_gallery_download(url, folder):
    """Download accessible Instagram media using gallery-dl and cookies."""
    output_folder = Path(folder) / "instagram_gallery"
    output_folder.mkdir(parents=True, exist_ok=True)

    cookie_file = create_cookie_file("www.instagram.com", folder)

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--directory",
        str(output_folder),
    ]

    if cookie_file and Path(cookie_file).is_file():
        command.extend(["--cookies", str(cookie_file)])

    command.append(url)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=DOWNLOAD_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Instagram gallery download timed out.") from exc

    allowed = {
        ".jpg", ".jpeg", ".png", ".webp", ".gif",
        ".mp4", ".m4v", ".mov",
    }

    files = [
        path for path in output_folder.rglob("*")
        if path.is_file()
        and path.suffix.lower() in allowed
        and path.stat().st_size > 0
    ]

    if files:
        files.sort(key=lambda item: str(item))
        return files[:MAX_CAROUSEL_FILES], "Instagram media"

    details = (result.stderr or result.stdout or "").strip()
    lower_details = details.lower()

    if any(
        word in lower_details
        for word in ("login", "cookies", "authentication", "no results")
    ):
        raise RuntimeError(
            "gallery-dl could not access this Story/post. Confirm "
            "/data/cookies.txt is a fresh Netscape-format cookie export "
            "and that the logged-in account is allowed to view this Story. "
            + details[-500:]
        )

    raise RuntimeError(
        "gallery-dl could not retrieve media. "
        + (
            details[-700:]
            if details
            else f"Exit code: {result.returncode}"
        )
    )


# ============================================================
# INSTAGRAM STORY HANDLER
# ============================================================

def instagram_story_download(url, folder):
    """Handle Instagram Story URLs separately using yt-dlp."""
    host = (urlparse(url).hostname or "").lower()

    if not (
        host == "instagram.com"
        or host.endswith(".instagram.com")
    ):
        raise RuntimeError("Not an Instagram URL.")

    return ytdlp_download(url, folder)


# ============================================================
# INSTAGRAM INSTALOADER FALLBACK
# ============================================================

def instagram_instaloader_download(url, folder):
    """Try Instaloader for accessible Instagram posts."""
    match = re.search(
        r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",
        url,
        re.IGNORECASE,
    )

    if not match:
        raise RuntimeError(
            "This is not a supported Instagram post URL. "
            "Story URLs are handled separately."
        )

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_comments=False,
        download_geotags=False,
        save_metadata=False,
        quiet=True,
    )

    post = instaloader.Post.from_shortcode(
        loader.context,
        match.group(1),
    )

    nodes = (
        list(post.get_sidecar_nodes())
        if post.typename == "GraphSidecar"
        else [post]
    )

    output_folder = folder / "instagram_instaloader"
    output_folder.mkdir(parents=True, exist_ok=True)

    files = []

    for index, node in enumerate(
        nodes[:MAX_CAROUSEL_FILES],
        start=1,
    ):
        is_video = getattr(node, "is_video", False)

        media_url = (
            getattr(node, "video_url", None)
            if is_video
            else getattr(node, "display_url", None)
        )

        if not media_url:
            continue

        extension = ".mp4" if is_video else ".jpg"
        destination = output_folder / f"instagram_{index:03d}{extension}"

        with requests.get(
            media_url,
            stream=True,
            timeout=(15, 60),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as response:
            response.raise_for_status()
            total = 0

            with destination.open("wb") as output:
                for chunk in response.iter_content(128 * 1024):
                    if not chunk:
                        continue

                    total += len(chunk)

                    if total > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError(
                            "Instagram media exceeds the download limit."
                        )

                    output.write(chunk)

        if destination.is_file() and destination.stat().st_size > 0:
            files.append(destination)

    if not files:
        raise RuntimeError(
            "Instaloader returned no accessible image or video files."
        )

    return files, post.caption or "Instagram media"


# ============================================================
# DOWNLOAD ROUTER
# ============================================================

def download_media(url, folder):
    host = (urlparse(url).hostname or "").lower()

    direct_error = None
    ytdlp_error = None
    gallery_error = None
    instaloader_error = None

    try:
        direct_file = download_direct_file(url, folder)

        if direct_file is not None:
            return [direct_file], direct_file.name

    except Exception as exc:
        direct_error = str(exc)
        log.info(
            "Direct download failed for %s: %s",
            host,
            direct_error[:300],
        )

    is_instagram = (
        host == "instagram.com"
        or host.endswith(".instagram.com")
    )
    is_story_url = (
        is_instagram
        and "/stories/" in urlparse(url).path.lower()
    )

    # Story links should not be sent to Instaloader's post parser.
    if is_story_url:
        try:
            return instagram_story_download(url, folder)
        except Exception as exc:
            ytdlp_error = str(exc)
            log.warning("Instagram Story download failed: %s", ytdlp_error[:500])

        try:
            return instagram_gallery_download(url, folder)
        except Exception as exc:
            gallery_error = str(exc)
            log.warning("Instagram Story gallery fallback failed: %s", gallery_error[:500])

        raise RuntimeError(
            "Instagram Story download failed.\n"
            f"yt-dlp: {ytdlp_error or 'No error recorded'}\n"
            f"gallery-dl: {gallery_error or 'No error recorded'}\n"
            "Verify that /data/cookies.txt contains fresh cookies from "
            "an account authorized to view this Story. The Story may also "
            "be expired, deleted, or otherwise unavailable."
        )

    try:
        return ytdlp_download(url, folder)

    except Exception as exc:
        ytdlp_error = str(exc)
        log.warning(
            "yt-dlp failed for %s: %s",
            host,
            ytdlp_error[:500],
        )

    if is_instagram:
        try:
            return instagram_gallery_download(url, folder)

        except Exception as exc:
            gallery_error = str(exc)
            log.warning(
                "gallery-dl failed: %s",
                gallery_error[:400],
            )

        try:
            return instagram_instaloader_download(url, folder)

        except Exception as exc:
            instaloader_error = str(exc)
            log.warning(
                "Instaloader failed: %s",
                instaloader_error[:400],
            )

    details = [
        "Media download failed.",
        f"Direct download: {direct_error or 'No direct file found'}",
        f"yt-dlp: {ytdlp_error or 'No extractor error recorded'}",
    ]

    if gallery_error:
        details.append(f"gallery-dl: {gallery_error[:400]}")

    if instaloader_error:
        details.append(f"Instaloader: {instaloader_error[:400]}")

    raise RuntimeError("\n".join(details))


# ============================================================
# TELEGRAM FILE DELIVERY
# ============================================================

async def send_downloaded_file(message, file_path):
    """Send a downloaded file using the appropriate Telegram method."""
    path = Path(file_path)

    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("Downloaded file is empty or missing.")

    size = path.stat().st_size

    if size > MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"File size is {size / (1024 * 1024):.1f} MB; "
            f"the configured upload limit is {MAX_UPLOAD_MB} MB."
        )

    extension = path.suffix.lower()
    caption = path.name[:900]

    with path.open("rb") as file:
        if extension == ".mp4":
            await message.reply_video(
                video=file,
                caption=caption,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )

        elif extension in AUDIO_EXTENSIONS:
            await message.reply_audio(
                audio=file,
                filename=path.name,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )

        else:
            await message.reply_document(
                document=file,
                filename=path.name,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        "👋 Welcome to Media Downloader!\n\n"
        "🔗 Send a supported media link.\n\n"
        "Supported platforms include:\n"
        "• YouTube videos and Shorts\n"
        "• Instagram posts, Reels, Stories, and carousels\n"
        "• Facebook and TikTok\n"
        "• X/Twitter, Reddit, and Pinterest\n"
        "• Other yt-dlp-supported sites\n"
        "• Direct downloadable files\n\n"
        f"📦 Download limit: {MAX_DOWNLOAD_MB} MB\n"
        f"📤 Telegram upload limit: {MAX_UPLOAD_MB} MB\n\n"
        "Use /help for instructions."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        "📖 How to use this bot\n\n"
        "1. Copy a supported media link.\n"
        "2. Send it to this bot.\n"
        "3. Wait for the download to finish.\n"
        "4. Receive the file in Telegram.\n\n"
        "Important:\n"
        "• Private, deleted, restricted, or login-protected content "
        "may not be available.\n"
        "• Some platforms may temporarily fail.\n"
        "• Instagram cookies expire and may need refreshing.\n"
        "• Only download content you are authorized to access."
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.message.text:
        return

    url = extract_url(update.message.text)

    if not url:
        return

    try:
        parsed = validate_public_url(url)
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Invalid or unsupported link.\n\n{str(exc)[:400]}"
        )
        return

    host = (parsed.hostname or "unknown").lower()

    status = await update.message.reply_text(
        "🔗 Thanks for providing the link!\n"
        "⏳ Processing your media..."
    )

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )

        with tempfile.TemporaryDirectory(
            prefix="media_downloader_"
        ) as temporary_directory:
            folder = Path(temporary_directory)

            try:
                files, title = await asyncio.wait_for(
                    asyncio.to_thread(
                        download_media,
                        url,
                        folder,
                    ),
                    timeout=DOWNLOAD_TIMEOUT + 60,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "Download timed out. Try again later or use "
                    "a smaller media file."
                ) from exc

            if not files:
                raise RuntimeError("No downloadable media was found.")

            deliverable = []

            for file_path in files:
                path = Path(file_path)

                if not path.is_file() or path.stat().st_size <= 0:
                    continue

                if path.stat().st_size > MAX_UPLOAD_BYTES:
                    log.warning("Skipping oversized file: %s", path.name)
                    continue

                deliverable.append(path)

            if not deliverable:
                raise RuntimeError(
                    "No downloaded file fits the Telegram upload limit "
                    f"of {MAX_UPLOAD_MB} MB."
                )

            await status.edit_text(
                "✅ Download completed!\n"
                "📤 Sending your file to Telegram..."
            )

            sent_count = 0

            for path in deliverable[:MAX_CAROUSEL_FILES]:
                await send_downloaded_file(update.message, path)
                sent_count += 1

            await status.edit_text(
                "✅ Download completed successfully!\n\n"
                f"📁 Files delivered: {sent_count}\n"
                "🙏 Thanks for using Media Downloader! ⚡"
            )

    except Exception as exc:
        error_text = str(exc).strip() or "Unknown download error"

        log.exception("Download failed for host %s", host)

        if len(error_text) > 2000:
            error_text = error_text[:2000] + "..."

        error_message = (
            "❌ Download failed.\n\n"
            f"Platform: {host}\n"
            f"Reason: {error_text}\n\n"
            "Check whether the link is accessible and supported."
        )

        try:
            await status.edit_text(error_message)
        except Exception:
            await update.message.reply_text(error_message)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    log.error(
        "Unhandled Telegram bot error",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it in your hosting environment."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
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
            handle_url,
        )
    )

    application.add_error_handler(error_handler)

    log.info("Media Downloader bot starting...")
    log.info("yt-dlp version: %s", yt_dlp.version.__version__)
    log.info("Maximum upload size: %s MB", MAX_UPLOAD_MB)
    log.info("Maximum download size: %s MB", MAX_DOWNLOAD_MB)

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
