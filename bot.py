
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
from urllib.parse import urlparse, unquote

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
    """Extract the first HTTP/HTTPS URL from a Telegram message."""

    if not text:
        return None

    match = re.search(
        r"https?://[^\s<>\"']+",
        text.strip(),
        re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(0).rstrip(".,!?)\]}>'\"")


def validate_public_url(url):
    """
    Validate a URL before making a request.

    Reject local/private IP addresses and non-HTTP protocols.
    This is a basic SSRF safeguard, not a guarantee against DNS rebinding.
    """

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise RuntimeError("Only HTTP and HTTPS links are supported.")

    host = parsed.hostname

    if not host:
        raise RuntimeError("The URL does not contain a valid hostname.")

    if host.lower() in {"localhost"} or host.lower().endswith(".local"):
        raise RuntimeError("Local network URLs are not supported.")

    try:
        addresses = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise RuntimeError("The link hostname could not be resolved.") from exc

    for address in addresses:
        ip_text = address[4][0]

        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            raise RuntimeError("The URL has an invalid IP address.")

        if not ip.is_global:
            raise RuntimeError(
                "Links to private or local network addresses are not supported."
            )

    return parsed


def safe_stream_request(url):
    """Request a URL while checking each redirect destination."""

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
                raise RuntimeError("The server returned an invalid redirect.")

            from urllib.parse import urljoin
            current_url = urljoin(current_url, location)
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("The URL redirected too many times.")


# ============================================================
# FILE HELPERS
# ============================================================

def detect_extension(url, content_type=None, content_disposition=None):
    """Determine a filename extension from response metadata."""

    if content_disposition:
        match = re.search(
            r'filename\*?=(?:UTF-8\'\')?"?([^";]+)',
            content_disposition,
            re.IGNORECASE,
        )

        if match:
            name = unquote(match.group(1).strip().strip('"'))
            suffix = Path(name).suffix.lower()

            if suffix:
                return suffix

    suffix = Path(urlparse(url).path).suffix.lower()

    if suffix in MEDIA_EXTENSIONS:
        return suffix

    if content_type:
        content_type = content_type.split(";")[0].strip().lower()
        guessed = mimetypes.guess_extension(content_type)

        if guessed:
            return guessed.lower()

    return ".bin"


def download_direct_file(url, folder):
    """
    Download a direct file link.

    HTML pages fall through to yt-dlp. Download limits apply while streaming.
    """

    response = safe_stream_request(url)

    try:
        content_type = response.headers.get("Content-Type", "").lower()

        if (
            "text/html" in content_type
            or "application/xhtml+xml" in content_type
        ):
            return None

        length = response.headers.get("Content-Length")

        if length:
            try:
                if int(length) > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"File exceeds the {MAX_DOWNLOAD_MB} MB download limit."
                    )
            except ValueError:
                pass

        extension = detect_extension(
            url,
            content_type,
            response.headers.get("Content-Disposition"),
        )

        filename = Path(urlparse(url).path).name
        filename = unquote(filename) if filename else ""

        if filename:
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
            filename = filename[:150]
        else:
            filename = "download" + extension

        if not Path(filename).suffix:
            filename += extension

        destination = folder / filename

        total = 0

        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"File exceeds the {MAX_DOWNLOAD_MB} MB download limit."
                    )

                output.write(chunk)

        if not destination.exists() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise RuntimeError("The downloaded file is empty.")

        # Avoid accepting obvious HTML or JSON responses saved as binary files.
        with destination.open("rb") as file:
            header = file.read(512).lstrip().lower()

        if (
            header.startswith(b"<!doctype html")
            or header.startswith(b"<html")
            or header.startswith(b"{")
            and "json" in content_type
        ):
            destination.unlink(missing_ok=True)
            return None

        return destination

    finally:
        response.close()


# ============================================================
# COOKIES
# ============================================================

def cookie_environment(host):
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
    Decode an optional Base64 Netscape-format cookie file.

    An optional cookies.txt at the project root is also supported.
    """

    root_cookie_file = Path("cookies.txt")

    if root_cookie_file.is_file() and root_cookie_file.stat().st_size > 0:
        return root_cookie_file.resolve()

    variable = cookie_environment(host)
    encoded = os.getenv(variable, "").strip()

    if not encoded:
        return None

    try:
        decoded = base64.b64decode(encoded, validate=True)

        if not decoded.strip():
            raise ValueError("Cookie file is empty.")

        cookie_path = folder / "cookies.txt"
        cookie_path.write_bytes(decoded)

        return cookie_path

    except Exception as exc:
        log.warning(
            "Unable to decode %s: %s",
            variable,
            str(exc)[:300],
        )
        return None


# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def ytdlp_download(url, folder):
    """
    Download supported social-media media with yt-dlp.

    Retries using an alternative format selector and rejects empty files.
    """

    host = (urlparse(url).hostname or "").lower()

    existing_files = {
        str(path.resolve())
        for path in folder.rglob("*")
        if path.is_file()
    }

    cookie_file = create_cookie_file(host, folder)

    common_options = {
        "outtmpl": str(folder / "%(title).80B [%(id)s].%(ext)s"),
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
    }

    if cookie_file and Path(cookie_file).is_file():
        common_options["cookiefile"] = str(cookie_file)

    format_selectors = [
        "bestvideo*+bestaudio/best",
        "best[height<=720]/best",
    ]

    errors = []
    media_extensions = MEDIA_EXTENSIONS

    for format_selector in format_selectors:
        options = dict(common_options)
        options["format"] = format_selector

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)

            candidates = []

            for path in folder.rglob("*"):
                if not path.is_file():
                    continue

                if str(path.resolve()) in existing_files:
                    continue

                if path.suffix.lower() not in media_extensions:
                    continue

                if path.name.endswith((".part", ".ytdl")):
                    continue

                if path.stat().st_size <= 0:
                    continue

                if path.name == "cookies.txt":
                    continue

                candidates.append(path)

            if candidates:
                candidates.sort(
                    key=lambda path: path.stat().st_size,
                    reverse=True,
                )

                title = (
                    info.get("title")
                    if isinstance(info, dict)
                    else None
                )

                return [candidates[0]], title or candidates[0].stem

            errors.append(
                f"Format {format_selector!r} produced no usable file."
            )

        except Exception as exc:
            message = str(exc)
            errors.append(message[:500])

            log.warning(
                "yt-dlp attempt failed for %s: %s",
                host,
                message[:500],
            )

    raise RuntimeError(
        "yt-dlp could not produce a non-empty media file. "
        + " | ".join(errors[-2:])
    )


# ============================================================
# INSTAGRAM GALLERY FALLBACK
# ============================================================


def instagram_gallery_download(url, folder):
    """Download accessible Instagram photos and carousels using cookies."""

    output_folder = folder / "instagram_gallery"
    output_folder.mkdir(parents=True, exist_ok=True)

    cookie_file = create_cookie_file("instagram.com", folder)

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "-D",
        str(output_folder),
    ]

    if cookie_file and Path(cookie_file).is_file():
        command.extend(["--cookies", str(cookie_file)])
    else:
        log.warning(
            "Instagram cookies not configured; login redirects may occur."
        )

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
        path
        for path in output_folder.rglob("*")
        if path.is_file()
        and path.suffix.lower() in allowed
        and path.stat().st_size > 0
    ]

    if not files:
        details = (
            result.stderr or result.stdout
            or f"gallery-dl exited with status {result.returncode}."
        )

        if "login" in details.lower() or "cookies" in details.lower():
            raise RuntimeError(
                "Instagram requires a valid login session. "
                "Check INSTAGRAM_COOKIES_B64 and export fresh cookies."
            )

        raise RuntimeError(details[-1200:])

    files.sort(key=lambda path: str(path))
    files = files[:MAX_CAROUSEL_FILES]

    return files, "Instagram media"



# ============================================================
# INSTAGRAM INSTALOADER FALLBACK
# ============================================================

def instagram_instaloader_download(url, folder):
    """Try Instaloader for publicly accessible Instagram posts."""

    match = re.search(
        r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",
        url,
        re.IGNORECASE,
    )

    if not match:
        raise RuntimeError("Could not identify the Instagram post ID.")

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
                            "Instagram media exceeds the download size limit."
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

    # 1. Direct downloadable file.
    try:
        direct_file = download_direct_file(url, folder)

        if direct_file is not None:
            return [direct_file], direct_file.name

    except Exception as exc:
        direct_error = str(exc)
        log.info(
            "Direct download failed for %s: %s",
            host,
            direct_error[:400],
        )

    # 2. yt-dlp for supported platforms.
    try:
        return ytdlp_download(url, folder)

    except Exception as exc:
        ytdlp_error = str(exc)
        log.warning(
            "yt-dlp failed for %s: %s",
            host,
            ytdlp_error[:700],
        )

    # 3. Additional Instagram extractors.
    if host == "instagram.com" or host.endswith(".instagram.com"):
        try:
            return instagram_gallery_download(url, folder)

        except Exception as exc:
            gallery_error = str(exc)
            log.warning(
                "gallery-dl failed for Instagram: %s",
                gallery_error[:500],
            )

        try:
            return instagram_instaloader_download(url, folder)

        except Exception as exc:
            instaloader_error = str(exc)
            log.warning(
                "Instaloader failed for Instagram: %s",
                instaloader_error[:500],
            )

    details = [
        "Media download failed.",
        f"Direct download: {direct_error or 'No direct file found'}",
        f"yt-dlp: {ytdlp_error or 'No extractor error recorded'}",
    ]

    if gallery_error:
        details.append(f"gallery-dl: {gallery_error[:500]}")

    if instaloader_error:
        details.append(f"Instaloader: {instaloader_error[:500]}")

    raise RuntimeError("\n".join(details))


# ============================================================
# TELEGRAM FILE DELIVERY
# ============================================================

async def send_downloaded_file(message, file_path):
    """Send a downloaded file using the appropriate Telegram method."""

    path = Path(file_path)

    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("The downloaded file is empty or missing.")

    size = path.stat().st_size

    if size > MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"The file is {size / (1024 * 1024):.1f} MB, "
            f"above the configured Telegram upload limit of "
            f"{MAX_UPLOAD_MB} MB."
        )

    extension = path.suffix.lower()
    caption = path.name[:900]

    with path.open("rb") as file:
        if extension in VIDEO_EXTENSIONS:
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
            # Sending images as documents preserves the original file.
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
        "🔗 Send a supported public media link to download it.\n\n"
        "Supported examples:\n"
        "• YouTube videos and Shorts\n"
        "• Instagram posts, reels, and supported carousels\n"
        "• Facebook, TikTok, X, Reddit, Pinterest, and other "
        "yt-dlp-supported sites\n"
        "• Direct links to accessible files\n\n"
        "📦 Maximum download size: "
        f"{MAX_DOWNLOAD_MB} MB\n"
        f"📤 Maximum upload size: {MAX_UPLOAD_MB} MB\n\n"
        "Use /help for more information."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    await update.message.reply_text(
        "📖 How to use Media Downloader\n\n"
        "1. Copy a supported public video, post, or file URL.\n"
        "2. Send the URL to this bot.\n"
        "3. Wait while the bot processes the link.\n"
        "4. Receive the downloaded file in Telegram.\n\n"
        "Important:\n"
        "• Private, deleted, restricted, or login-protected content "
        "may not be accessible.\n"
        "• Some platforms are not supported by yt-dlp or gallery-dl.\n"
        "• Direct file URLs must be accessible without unsupported "
        "authentication flows.\n"
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

    # Ignore ordinary text without a URL.
    if not url:
        return

    try:
        parsed = validate_public_url(url)
    except Exception as exc:
        await update.message.reply_text(
            f"❌ Invalid or unsupported link.\n\n{str(exc)[:500]}"
        )
        return

    host = (parsed.hostname or "unknown").lower()

    status = await update.message.reply_text(
        "🔗 Your link has been received!\n"
        "⏳ Your link is under process..."
    )

    work_dir = None

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )

        with tempfile.TemporaryDirectory(prefix="media_dl_") as temp:
            work_dir = Path(temp)

            try:
                files, title = await asyncio.wait_for(
                    asyncio.to_thread(
                        download_media,
                        url,
                        work_dir,
                    ),
                    timeout=DOWNLOAD_TIMEOUT + 60,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "The download timed out. Try again later or use "
                    "a smaller/shorter media file."
                ) from exc

            if not files:
                raise RuntimeError("No downloadable media was found.")

            # Keep delivery within the configured upload limit.
            deliverable = []

            for file_path in files:
                path = Path(file_path)

                if not path.is_file() or path.stat().st_size <= 0:
                    continue

                if path.stat().st_size > MAX_UPLOAD_BYTES:
                    log.warning(
                        "Skipping oversized file %s (%d bytes)",
                        path.name,
                        path.stat().st_size,
                    )
                    continue

                deliverable.append(path)

            if not deliverable:
                raise RuntimeError(
                    "No downloaded files fit the Telegram upload limit "
                    f"of {MAX_UPLOAD_MB} MB. Try a smaller video or "
                    "lower available quality."
                )

            await status.edit_text(
                "✅ Download completed!\n"
                "📤 Sending your file(s) to Telegram..."
            )

            sent_count = 0

            for path in deliverable[:MAX_CAROUSEL_FILES]:
                await send_downloaded_file(update.message, path)
                sent_count += 1

            await status.edit_text(
                "✅ Download completed successfully!\n\n"
                f"📁 Files delivered: {sent_count}\n"
                "🙏 Thanks for using Media Downloader. 🎉"
            )

    except Exception as exc:
        error_text = str(exc).strip() or "Unknown download error"

        log.exception(
            "Download failed for host %s",
            host,
        )

        # Keep the Telegram error readable.
        if len(error_text) > 2500:
            error_text = error_text[:2500] + "..."

        try:
            await status.edit_text(
                "❌ Download failed.\n\n"
                f"Platform: {host}\n"
                f"Technical reason: {error_text}\n\n"
                "Check whether the link is accessible and supported."
            )
        except Exception:
            await update.message.reply_text(
                "❌ Download failed.\n\n"
                f"Platform: {host}\n"
                f"Technical reason: {error_text}"
            )


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

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_url,
        )
    )

    application.add_error_handler(error_handler)

    log.info("Media Downloader bot starting...")
    log.info("yt-dlp version: %s", yt_dlp.version.__version__)
    log.info("FFmpeg availability is checked by yt-dlp when needed.")
    log.info("Maximum upload size: %s MB", MAX_UPLOAD_MB)
    log.info("Maximum download size: %s MB", MAX_DOWNLOAD_MB)

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
