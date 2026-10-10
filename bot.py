
import os
import re
import sys
import time
import socket
import ipaddress
import asyncio
import logging
import tempfile
import shutil
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("media_downloader")

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)

SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".txt", ".rtf",
    ".jpg", ".jpeg", ".png", ".gif", ".svg",
    ".heic", ".heif", ".webp",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".opus",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".zip", ".rar", ".apk", ".ipa", ".exe", ".dmg",
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".opus"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "application/zip": ".zip",
    "application/vnd.rar": ".rar",
    "application/x-rar-compressed": ".rar",
    "application/vnd.android.package-archive": ".apk",
    "application/octet-stream": "",
}


# ============================================================
# URL VALIDATION AND NETWORK SAFETY
# ============================================================

def extract_url(text):
    match = URL_PATTERN.search(text or "")
    if not match:
        return None

    url = match.group(0).rstrip(".,!?;:)]}")
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None

    return url


def validate_public_url(url):
    """Reject non-public destinations to reduce SSRF risk."""
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS links are supported.")

    host = parsed.hostname

    if not host or host.lower() in {"localhost"}:
        raise ValueError("This destination is not allowed.")

    try:
        addresses = socket.getaddrinfo(host, parsed.port or (
            443 if parsed.scheme == "https" else 80
        ))
    except socket.gaierror as exc:
        raise RuntimeError("Could not resolve the URL host.") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])

        if not ip.is_global:
            raise ValueError(
                "Private, local, and non-public network destinations "
                "are not allowed."
            )


def safe_stream_request(url):
    """Follow redirects manually and validate each destination."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
        )
    })

    current_url = url

    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current_url)

        response = session.get(
            current_url,
            stream=True,
            allow_redirects=False,
            timeout=(15, 30),
        )

        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()

            if not location:
                session.close()
                raise RuntimeError("Redirect did not include a destination.")

            from urllib.parse import urljoin
            current_url = urljoin(current_url, location)
            continue

        if response.status_code >= 400:
            status = response.status_code
            response.close()
            session.close()
            raise RuntimeError(f"Direct link returned HTTP {status}.")

        return session, response

    session.close()
    raise RuntimeError("Too many redirects.")


# ============================================================
# MIME AND FILE TYPE DETECTION
# ============================================================

def detect_extension(path, content_type="", url=""):
    """Identify common file types using signatures, MIME, and URL."""
    try:
        with path.open("rb") as file_obj:
            head = file_obj.read(8192)
    except OSError:
        head = b""

    if head.startswith(b"%PDF-"):
        return ".pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head.startswith(b"PK\x03\x04"):
        # Could be ZIP, DOCX, or APK. Preserve a known URL extension.
        pass
    if head.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ".mkv"
    if head.startswith(b"ID3") or head.startswith(b"\xff\xfb"):
        return ".mp3"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return ".wav"
    if head.startswith(b"\x00\x00\x00") and b"ftyp" in head[:32]:
        return ".mp4"

    mime = (content_type or "").split(";")[0].strip().lower()
    extension = MIME_EXTENSIONS.get(mime)

    if extension:
        return extension

    url_extension = Path(urlparse(url).path).suffix.lower()
    if url_extension in SUPPORTED_EXTENSIONS:
        return url_extension

    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed:
        return guessed

    return ".bin"


def safe_filename(name):
    name = unquote(name or "download")
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", name)
    return name[:180] or "download"


# ============================================================
# DIRECT FILE DOWNLOADER
# ============================================================

def download_direct_file(url, folder):
    """
    Stream a direct file URL to disk.
    Return None when the response is an HTML page, allowing yt-dlp
    to attempt extraction instead.
    """
    session, response = safe_stream_request(url)

    try:
        content_type = response.headers.get("Content-Type", "")
        mime = content_type.split(";")[0].strip().lower()
        url_extension = Path(urlparse(url).path).suffix.lower()

        if mime in {"text/html", "application/xhtml+xml"}:
            return None

        if mime in {"application/json", "text/json"} and (
            url_extension not in SUPPORTED_EXTENSIONS
        ):
            return None

        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                f"File exceeds the {MAX_DOWNLOAD_MB} MB download limit."
            )

        name = safe_filename(Path(urlparse(url).path).name)
        initial_extension = Path(name).suffix.lower()

        if initial_extension not in SUPPORTED_EXTENSIONS:
            initial_extension = MIME_EXTENSIONS.get(mime, "")

        destination = folder / (
            Path(name).stem[:120] + (initial_extension or ".download")
        )

        total = 0
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue

                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"File exceeded the {MAX_DOWNLOAD_MB} MB limit."
                    )

                output.write(chunk)

        if total == 0:
            destination.unlink(missing_ok=True)
            raise RuntimeError("The server returned an empty file.")

        detected = detect_extension(destination, content_type, url)

        # Use the detected extension when the URL lacks a useful one.
        if Path(urlparse(url).path).suffix.lower() not in SUPPORTED_EXTENSIONS:
            corrected = destination.with_suffix(detected)
            if corrected != destination:
                destination.rename(corrected)
                destination = corrected

        return destination

    finally:
        response.close()
        session.close()


# ============================================================
# YT-DLP OPTIONS AND EXTRACTOR
# ============================================================

def cookie_environment(host):
    if host == "youtu.be" or host.endswith(".youtube.com") or host == "youtube.com":
        return "YOUTUBE_COOKIES_B64"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "INSTAGRAM_COOKIES_B64"
    if host == "facebook.com" or host.endswith(".facebook.com"):
        return "FACEBOOK_COOKIES_B64"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "TIKTOK_COOKIES_B64"
    if host == "x.com" or host.endswith(".x.com") or host.endswith(".twitter.com"):
        return "X_COOKIES_B64"
    return None


def create_cookie_file(host, folder):
    import base64

    variable = cookie_environment(host)
    encoded = os.getenv(variable, "").strip() if variable else ""

    if not encoded:
        return None

    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw:
            return None

        cookie_file = folder / "cookies.txt"
        cookie_file.write_bytes(raw)
        cookie_file.chmod(0o600)
        return str(cookie_file)

    except Exception:
        log.exception("Could not decode %s", variable)
        return None


def ytdlp_download(url, folder):
    host = (urlparse(url).hostname or "").lower()

    options = {
        "outtmpl": str(folder / "%(title).80B_%(id)s.%(ext)s"),
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "max_filesize": MAX_DOWNLOAD_BYTES,
    }

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg

    cookie_file = create_cookie_file(host, folder)
    if cookie_file:
        options["cookiefile"] = cookie_file

    if host == "instagram.com" or host.endswith(".instagram.com"):
        options["noplaylist"] = False

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    if not info:
        raise RuntimeError("yt-dlp returned no media information.")

    title = info.get("title") or "Downloaded media"
    files = []

    for path in folder.iterdir():
        if (
            path.is_file()
            and path.name != "cookies.txt"
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
            and path.stat().st_size > 0
        ):
            files.append(path)

    if not files:
        raise RuntimeError(
            "Extractor returned no supported files. "
            "The post may be restricted or unsupported."
        )

    # YouTube normally produces one merged file. Instagram carousels
    # may produce multiple files.
    if host == "youtu.be" or "youtube.com" in host:
        files = [max(files, key=lambda item: item.stat().st_size)]

    return files, title


# ============================================================
# DOWNLOAD ROUTER
# ============================================================

def download_media(url, folder):
    host = (urlparse(url).hostname or "").lower()

    # Initialize errors so they are available on every path.
    direct_error = None
    ytdlp_error = None

    # 1. Try direct file download.
    try:
        direct_file = download_direct_file(url, folder)

        if direct_file is not None:
            return [direct_file], direct_file.name

    except Exception as exc:
        direct_error = str(exc)
        log.warning(
            "Direct download failed for %s: %s",
            host,
            direct_error[:500],
        )

    # 2. Try yt-dlp.
    try:
        return ytdlp_download(url, folder)

    except Exception as exc:
        ytdlp_error = str(exc)
        log.warning(
            "yt-dlp failed for %s: %s",
            host,
            ytdlp_error[:700],
        )

    # 3. Instagram fallback for publicly accessible posts.
    if host == "instagram.com" or host.endswith(".instagram.com"):
        try:
            shortcode_match = re.search(
                r"instagram\.com/(?:p|reel|reels|tv)/"
                r"([A-Za-z0-9_-]+)",
                url,
                re.IGNORECASE,
            )

            if not shortcode_match:
                raise RuntimeError(
                    "Could not identify the Instagram post ID."
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
                shortcode_match.group(1),
            )

            nodes = (
                list(post.get_sidecar_nodes())
                if post.typename == "GraphSidecar"
                else [post]
            )

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

                suffix = ".mp4" if is_video else ".jpg"
                destination = folder / (
                    f"instagram_{index:03d}{suffix}"
                )

                total = 0

                with requests.get(
                    media_url,
                    stream=True,
                    timeout=(15, 60),
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as response:
                    response.raise_for_status()

                    with destination.open("wb") as output:
                        for chunk in response.iter_content(
                            128 * 1024
                        ):
                            if not chunk:
                                continue

                            total += len(chunk)

                            if total > MAX_DOWNLOAD_BYTES:
                                raise RuntimeError(
                                    "Instagram file exceeds the "
                                    "configured download size limit."
                                )

                            output.write(chunk)

                if (
                    destination.is_file()
                    and destination.stat().st_size > 0
                ):
                    files.append(destination)

            if files:
                return files, post.caption or "Instagram media"

            raise RuntimeError(
                "Instagram returned no accessible media files."
            )

        except Exception as exc:
            instagram_error = str(exc)

            raise RuntimeError(
                "Media extraction failed.\n"
                f"Direct download: "
                f"{direct_error or 'No direct file found'}\n"
                f"yt-dlp: "
                f"{ytdlp_error or 'No extractor error recorded'}\n"
                f"Instagram fallback: {instagram_error}"
            ) from exc

    # 4. Report the actual failure without an unbound-variable error.
    raise RuntimeError(
        "Media download failed.\n"
        f"Direct download: "
        f"{direct_error or 'No direct file found'}\n"
        f"yt-dlp: "
        f"{ytdlp_error or 'No extractor error recorded'}"
    )
# ============================================================
# TELEGRAM MESSAGES AND UPLOAD
# ============================================================

async def edit_status(message, text):
    try:
        await message.edit_text(text[:4000])
    except Exception:
        log.exception("Status message update failed")


async def send_file(message, path, title):
    size = path.stat().st_size

    if size > MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"{path.name} is {size / (1024 * 1024):.1f} MB, "
            f"above the configured upload limit of {MAX_UPLOAD_MB} MB."
        )

    suffix = path.suffix.lower()
    caption = (title or path.name)[:900]

    with path.open("rb") as media:
        if suffix in IMAGE_EXTENSIONS:
            await message.reply_photo(photo=media, caption=caption)
        elif suffix in VIDEO_EXTENSIONS:
            await message.reply_video(
                video=media,
                caption=caption,
                supports_streaming=True,
            )
        elif suffix in AUDIO_EXTENSIONS:
            await message.reply_audio(
                audio=media,
                title=caption[:250],
            )
        else:
            await message.reply_document(
                document=media,
                filename=path.name[:250],
                caption=caption,
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 Welcome to Media Downloader!\n\n"
        "🔗 Send a supported media or direct-file link.\n\n"
        "⏳ Your link is under process…\n"
        "✅ When the download and upload finish, I'll thank you!\n\n"
        "/help - Instructions\n"
        "/status - Configuration"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Send a public media link or a direct link to a file.\n\n"
        "Documents, images, audio, video, archives, and application "
        "files can be downloaded when the server makes them accessible.\n\n"
        "Social-media posts depend on supported extractors and platform "
        "permissions. Private or DRM-protected content is not supported."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🛠 Bot status\n\n"
        f"Token configured: {'Yes' if BOT_TOKEN else 'No'}\n"
        f"yt-dlp version: {yt_dlp.version.__version__}\n"
        f"FFmpeg available: {'Yes' if shutil.which('ffmpeg') else 'No'}\n"
        f"Download limit: {MAX_DOWNLOAD_MB} MB\n"
        f"Upload limit: {MAX_UPLOAD_MB} MB"
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    url = extract_url(message.text)
    if not url:
        await message.reply_text("❌ Please send a valid HTTP or HTTPS URL.")
        return

    host = (urlparse(url).hostname or "unknown").lower()

    status = await message.reply_text(
        "🔗 Your link has been received!\n\n"
        "⏳ Your link is under process…\n"
        "Please wait while I identify and download the file."
    )

    folder = Path(tempfile.mkdtemp(prefix="telegram_downloader_"))

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.TYPING,
        )

        await edit_status(
            status,
            f"🔄 Your link is under process…\n\n"
            f"🌐 Platform: {host}\n"
            "⬇️ Downloading your file…"
        )

        files, title = await asyncio.wait_for(
            asyncio.to_thread(download_media, url, folder),
            timeout=DOWNLOAD_TIMEOUT,
        )

        for index, path in enumerate(files, start=1):
            await edit_status(
                status,
                f"📤 Download complete!\n"
                f"Sending file {index}/{len(files)}…"
            )
            await send_file(
                message,
                path,
                title if len(files) == 1 else f"{title} ({index})",
            )

        await edit_status(
            status,
            "✅ Download completed successfully!\n\n"
            "🙏 Thanks for using Media Downloader.\n"
            "🎉 Your file has been delivered!"
        )

    except asyncio.TimeoutError:
        await edit_status(
            status,
            f"⏱️ Processing exceeded {DOWNLOAD_TIMEOUT} seconds. "
            "Please try again later."
        )

    except Exception as error:
        reason = str(error).replace(BOT_TOKEN, "[hidden]")[:1400]
        log.exception("Download failed for host %s", host)

        await edit_status(
            status,
            "❌ Download failed.\n\n"
            f"Platform: {host}\n"
            f"Technical reason: {reason}\n\n"
            "Check whether the link is accessible and supported."
        )

    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📄 File received! This bot downloads files from links; "
        "it does not yet convert arbitrary files you upload."
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add it to your hosting environment."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url)
    )
    app.add_handler(
        MessageHandler(
            filters.Document.ALL
            | filters.PHOTO
            | filters.VIDEO
            | filters.AUDIO
            | filters.VOICE,
            handle_file,
        )
    )

    log.info("Telegram downloader started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
