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
import uuid
import shutil

from pathlib import Path
from urllib.parse import urlparse, unquote, urljoin

import requests
import yt_dlp
import instaloader

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "800"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
MAX_REDIRECTS = 5
MAX_CAROUSEL_FILES = int(os.getenv("MAX_CAROUSEL_FILES", "10"))

MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024

# Persistent source files for post-download conversion buttons.
# Railway: mount a persistent volume at /data.
CONVERSION_DIR = Path(os.getenv("CONVERSION_DIR", "/data/pending_conversions"))
try:
    CONVERSION_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    CONVERSION_DIR = Path(tempfile.gettempdir()) / "media_downloader_conversions"
    CONVERSION_DIR.mkdir(parents=True, exist_ok=True)

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
    """
    Validate the URL and reject local/private network destinations.
    This is a basic SSRF safeguard, not a complete DNS-rebinding defense.
    """
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
    """Download direct files while validating every redirect."""
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


def resolve_pinterest_short_url(url):
    """Resolve pin.it links to a recognized Pinterest URL."""
    host = (urlparse(url).hostname or "").lower()

    if host != "pin.it" and not host.endswith(".pin.it"):
        return url

    response = safe_stream_request(url)

    try:
        resolved = response.url or url
    finally:
        response.close()

    validate_public_url(resolved)
    final_host = (urlparse(resolved).hostname or "").lower()

    allowed_domains = (
        "pinterest.com",
        "pinterest.co.uk",
        "pinterest.ca",
        "pinterest.de",
    )

    if not any(
        final_host == domain or final_host.endswith("." + domain)
        for domain in allowed_domains
    ):
        raise RuntimeError(
            "The pin.it link did not resolve to a recognized Pinterest URL."
        )

    return resolved


# ============================================================
# DIRECT FILE DOWNLOAD
# ============================================================

def detect_extension(url, content_type=None, disposition=None):
    """Find a filename extension from response headers or URL."""
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
    """Download a direct file URL; return None for HTML pages."""
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
            or (header.startswith(b"{") and "json" in content_type)
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
    """Find a valid Netscape cookie file without exposing its contents."""
    host = (host or "").lower()
    folder = Path(folder)

    is_instagram = (
        host == "instagram.com"
        or host.endswith(".instagram.com")
    )

    def inspect_cookie_file(path):
        path = Path(path)

        if not path.is_file() or path.stat().st_size == 0:
            return False, False

        try:
            with path.open(
                "r",
                encoding="utf-8-sig",
                errors="strict",
            ) as file:
                lines = file.read().splitlines()
        except (OSError, UnicodeError):
            return False, False

        valid_header = any(
            line.strip() in (
                "# Netscape HTTP Cookie File",
                "# HTTP Cookie File",
            )
            for line in lines[:10]
        )

        has_sessionid = False

        for line in lines:
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
                has_sessionid = True
                break

        return valid_header, has_sessionid

    candidates = []

    # Explicitly configured cookie path.
    configured_path = os.getenv(
        "INSTAGRAM_COOKIES_PATH", ""
    ).strip()

    if is_instagram and configured_path:
        candidates.append(Path(configured_path))

    # Persistent Railway Volume and common alternative locations.
    if is_instagram:
        candidates.extend([
            Path("/data/cookies.txt"),
            Path("/app/cookies.txt"),
            Path("/app/data/cookies.txt"),
        ])

    # Optional Base64 cookie file.
    variable = cookie_environment(host)
    encoded = os.getenv(variable, "").strip()

    if encoded:
        try:
            normalized = encoded.replace("-", "+").replace("_", "/")
            normalized += "=" * ((4 - len(normalized) % 4) % 4)

            decoded = base64.b64decode(
                normalized,
                validate=True,
            )

            generated_file = folder / "environment_cookies.txt"
            generated_file.write_bytes(decoded)
            candidates.append(generated_file)

        except Exception:
            log.warning(
                "%s could not be decoded. Use a cookie file instead.",
                variable,
            )

    candidates.append(Path("cookies.txt"))

    checked = set()

    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            pass

        if str(candidate) in checked:
            continue

        checked.add(str(candidate))

        valid_header, has_sessionid = inspect_cookie_file(candidate)

        if not valid_header:
            if candidate.exists():
                log.warning(
                    "Cookie file exists but is not valid Netscape format: %s",
                    candidate,
                )
            continue

        if is_instagram and not has_sessionid:
            log.warning(
                "Instagram cookie file has no non-empty sessionid cookie: %s",
                candidate,
            )
            continue

        log.info(
            "Cookie file selected: %s; format_ok=True; sessionid_present=%s",
            candidate,
            has_sessionid,
        )

        return candidate

    if is_instagram:
        log.error(
            "No usable Instagram cookie file found. "
            "Expected /data/cookies.txt in Netscape format."
        )

    return None




# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

class SafeExtensionYoutubeDL(yt_dlp.YoutubeDL):
    """Normalize unusual Snapchat extension metadata when possible."""

    def process_ie_result(self, ie_result, download=True, extra_info=None):
        if isinstance(ie_result, dict):
            source_url = (
                ie_result.get("webpage_url")
                or ie_result.get("original_url")
                or ie_result.get("url")
                or ""
            )
            source_host = (urlparse(source_url).hostname or "").lower()

            is_snapchat = (
                source_host == "snapchat.com"
                or source_host.endswith(".snapchat.com")
            )

            if is_snapchat:
                allowed_extensions = {
                    "mp4", "m4v", "mov", "webm", "mkv", "m3u8", "mpd"
                }

                formats = ie_result.get("formats")
                if isinstance(formats, list):
                    for fmt in formats:
                        if not isinstance(fmt, dict):
                            continue

                        ext = str(fmt.get("ext") or "").lower()
                        if ext and ext not in allowed_extensions:
                            log.warning(
                                "Snapchat returned unusual extension metadata; "
                                "normalizing metadata to mp4."
                            )
                            fmt["ext"] = "mp4"

                ext = str(ie_result.get("ext") or "").lower()
                if ext and ext not in allowed_extensions:
                    ie_result["ext"] = "mp4"

        return super().process_ie_result(
            ie_result,
            download=download,
            extra_info=extra_info,
        )


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
        "outtmpl": str(folder / "%(title).80B [%(id)s].%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
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
        log.info("yt-dlp will use cookie file at %s", cookie_file)

    selectors = [
        "bestvideo*+bestaudio/best",
        "best[height<=720]/best",
    ]

    errors = []

    for selector in selectors:
        attempt_options = dict(options)
        attempt_options["format"] = selector

        try:
            with SafeExtensionYoutubeDL(attempt_options) as ydl:
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
# GALLERY-DL HELPERS
# ============================================================

def run_gallery_dl(url, folder, output_name, platform_label):
    """Run gallery-dl and return files it successfully downloaded."""
    output_folder = Path(folder) / output_name
    output_folder.mkdir(parents=True, exist_ok=True)

    host = (urlparse(url).hostname or "").lower()
    cookie_file = create_cookie_file(host, folder)

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
        raise RuntimeError(
            f"{platform_label} download timed out."
        ) from exc

    files = [
        path
        for path in output_folder.rglob("*")
        if path.is_file()
        and path.suffix.lower() in MEDIA_EXTENSIONS
        and path.name != "cookies.txt"
        and path.stat().st_size > 0
    ]

    files.sort(key=lambda item: str(item))

    if files:
        return files[:MAX_CAROUSEL_FILES], f"{platform_label} media"

    details = (result.stderr or result.stdout or "").strip()

    raise RuntimeError(
        f"gallery-dl could not retrieve {platform_label} media. "
        + (
            details[-700:]
            if details
            else f"Exit code: {result.returncode}"
        )
    )



def instagram_gallery_download(url, folder):
    """Try gallery-dl for Instagram media accessible to the account."""
    output_folder = Path(folder) / "instagram_gallery"
    output_folder.mkdir(parents=True, exist_ok=True)

    cookie_file = create_cookie_file(
        "www.instagram.com",
        folder,
    )

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--directory",
        str(output_folder),
    ]

    if cookie_file and cookie_file.is_file():
        command.extend(["--cookies", str(cookie_file)])
        log.info("gallery-dl will use the configured cookie file.")
    else:
        log.error(
            "gallery-dl has no Instagram cookie file. "
            "Check /data/cookies.txt."
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
        raise RuntimeError(
            "Instagram gallery download timed out."
        ) from exc

    allowed_extensions = {
        ".jpg", ".jpeg", ".png", ".webp",
        ".gif", ".mp4", ".m4v", ".mov",
    }

    files = [
        path
        for path in output_folder.rglob("*")
        if path.is_file()
        and path.suffix.lower() in allowed_extensions
        and path.stat().st_size > 0
    ]

    files.sort(key=lambda path: str(path))

    if files:
        return files[:MAX_CAROUSEL_FILES], "Instagram media"

    details = (result.stderr or result.stdout or "").strip()

    raise RuntimeError(
        "gallery-dl could not retrieve Instagram media. "
        "Check whether the cookie session is valid and authorized "
        "to view this Story. "
        + (
            details[-700:]
            if details
            else f"Exit code: {result.returncode}"
        )
    )


def gallery_dl_download(url, folder, platform_label="this platform"):
    """Fallback for Pinterest and other gallery-dl-supported sites."""
    return run_gallery_dl(
        url,
        folder,
        "gallery_dl_output",
        platform_label,
    )


def instagram_story_download(url, folder):
    """Route Story URLs through yt-dlp."""
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
    """Try Instaloader for accessible Instagram posts and reels."""
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

    output_folder = Path(folder) / "instagram_instaloader"
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

    # Resolve Pinterest short links.
    if host == "pin.it" or host.endswith(".pin.it"):
        try:
            url = resolve_pinterest_short_url(url)
            host = (urlparse(url).hostname or "").lower()
            log.info("Resolved Pinterest short link.")
        except Exception as exc:
            log.warning(
                "Could not resolve Pinterest short URL: %s",
                str(exc)[:300],
            )

    # 1. Direct downloadable files.
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

    # 2. yt-dlp.
    try:
        return ytdlp_download(url, folder)

    except Exception as exc:
        ytdlp_error = str(exc)
        log.warning(
            "yt-dlp failed for %s: %s",
            host,
            ytdlp_error[:500],
        )

    # 3. Instagram-specific fallbacks.
    is_instagram = (
        host == "instagram.com"
        or host.endswith(".instagram.com")
    )

    if is_instagram:
        is_story_url = "/stories/" in urlparse(url).path.lower()

        try:
            if is_story_url:
                return instagram_story_download(url, folder)

            return instagram_gallery_download(url, folder)

        except Exception as exc:
            gallery_error = str(exc)
            log.warning(
                "Instagram gallery-dl failed: %s",
                gallery_error[:400],
            )

        if not is_story_url:
            try:
                return instagram_instaloader_download(url, folder)

            except Exception as exc:
                instaloader_error = str(exc)
                log.warning(
                    "Instaloader failed: %s",
                    instaloader_error[:400],
                )

        raise RuntimeError(
            "Instagram download failed.\n"
            f"yt-dlp: {ytdlp_error or 'Unknown error'}\n"
            f"gallery-dl: {gallery_error or 'Unknown error'}\n"
            + (
                f"Instaloader: {instaloader_error}\n"
                if instaloader_error
                else ""
            )
            + "For Stories and restricted media, configure a current "
            "authorized cookie file at /data/cookies.txt. "
            "This code cannot bypass Instagram login or privacy restrictions."
        )

    # Explain Facebook login redirects clearly.
    combined_errors = " ".join(
        str(item or "")
        for item in (direct_error, ytdlp_error, gallery_error)
    ).lower()

    is_facebook = (
        host == "facebook.com"
        or host.endswith(".facebook.com")
        or host == "fb.watch"
        or host.endswith(".fb.watch")
    )

    if is_facebook and "facebook.com/login.php" in combined_errors:
        raise RuntimeError(
            "Facebook redirected this link to its login page. "
            "If this is your content or you are authorized to view it, "
            "configure a current Netscape-format cookie file at "
            "/data/facebook_cookies.txt or set FACEBOOK_COOKIES_PATH. "
            "Private or restricted Stories may still be unavailable."
        )

    # 4. gallery-dl fallback for supported gallery sites.
    gallery_hosts = (
        "pinterest.com",
        "pinterest.co.uk",
        "pinterest.ca",
        "pinterest.de",
        "redd.it",
        "reddit.com",
        "pixiv.net",
        "imgur.com",
        "tumblr.com",
    )

    if any(
        host == domain or host.endswith("." + domain)
        for domain in gallery_hosts
    ):
        try:
            label = (
                "Pinterest"
                if "pinterest" in host
                else host
            )

            return gallery_dl_download(url, folder, label)

        except Exception as exc:
            gallery_error = str(exc)
            log.warning(
                "gallery-dl failed for %s: %s",
                host,
                gallery_error[:500],
            )

    details = [
        "Media download failed.",
        f"Direct download: {direct_error or 'No direct file found'}",
        f"yt-dlp: {ytdlp_error or 'No extractor error recorded'}",
    ]

    if gallery_error:
        details.append(f"gallery-dl: {gallery_error[:700]}")

    if instaloader_error:
        details.append(f"Instaloader: {instaloader_error[:400]}")

    if "pinterest" in host or host.endswith("pin.it"):
        details.append(
            "Pinterest tip: the pin may be an image instead of a video. "
            "The bot tries gallery-dl after yt-dlp. If it still fails, "
            "update the packages and check whether the pin is public."
        )

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
    with path.open("rb") as file:
        if extension == ".mp4":
            await message.reply_video(
                video=file,
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
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )

        else:
            await message.reply_document(
                document=file,
                filename=path.name,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )


# ============================================================
# POST-DOWNLOAD VIDEO CONVERSION
# ============================================================


def convert_video_file(source_path, mode):
    """Convert video to H.264/AAC, H.265/AAC, or extract AAC audio."""
    source = Path(source_path)

    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError(
            "Original video is missing or empty. Download it again."
        )

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg is missing. Install it in your Railway Dockerfile."
        )

    if mode == "h264":
        output = source.with_name(source.stem + "_H264.mp4")
        codec_args = [
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
        ]

    elif mode == "h265":
        output = source.with_name(source.stem + "_H265.mp4")
        codec_args = [
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx265",
            "-preset", "ultrafast",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-tag:v", "hvc1",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-x265-params", "log-level=error:pools=2",
        ]

    elif mode == "aac":
        output = source.with_name(source.stem + "_AAC.m4a")
        codec_args = [
            "-map", "0:a:0",
            "-vn",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
        ]

    else:
        raise RuntimeError(f"Unknown conversion mode: {mode}")

    output.unlink(missing_ok=True)

    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i", str(source),
        *codec_args,
        str(output),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=DOWNLOAD_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "FFmpeg timed out. Try a shorter video."
        ) from exc
    except OSError as exc:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"Could not start FFmpeg: {exc}"
        ) from exc

    stderr = result.stderr or ""
    stdout = result.stdout or ""

    log.info("FFmpeg exit code: %s", result.returncode)
    log.info("FFmpeg stderr: %s", stderr[-6000:])
    if stdout.strip():
        log.info("FFmpeg stdout: %s", stdout[-2000:])

    if result.returncode != 0:
        output.unlink(missing_ok=True)
        details = stderr.strip() or stdout.strip()
        if not details:
            details = f"No diagnostic output; exit code {result.returncode}"

        if mode == "aac" and (
            "matches no streams" in details.lower()
            or "does not contain any stream" in details.lower()
        ):
            raise RuntimeError(
                "This video does not contain an audio track."
            )

        raise RuntimeError(
            f"FFmpeg exited with code {result.returncode}: "
            f"{details[-1200:]}"
        )

    if not output.is_file():
        raise RuntimeError(
            f"FFmpeg exited with code 0 but created no output. "
            f"Check Railway logs and disk space. Expected: {output.name}"
        )

    if output.stat().st_size == 0:
        output.unlink(missing_ok=True)
        raise RuntimeError("FFmpeg created an empty output file.")

    if output.stat().st_size > MAX_UPLOAD_BYTES:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"Converted file exceeds the {MAX_UPLOAD_MB} MB upload limit."
        )

    log.info(
        "Conversion successful: %s (%s bytes)",
        output.name,
        output.stat().st_size,
    )

    return output




async def convert_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    try:
        _, token, mode = query.data.split(":", 2)

        pending = context.application.bot_data.get(
            "pending_conversions", {}
        )
        item = pending.get(token)

        if not item:
            await query.message.reply_text(
                "⌛ This conversion request has expired. "
                "Please download the video again."
            )
            return

        if item["user_id"] != query.from_user.id:
            await query.message.reply_text(
                "❌ This conversion menu belongs to another user."
            )
            return

        labels = {
            "h264": "H.264 + AAC (MP4)",
            "h265": "H.265 + AAC (MP4)",
            "aac": "AAC Audio (M4A)",
        }

        label = labels.get(mode, mode)

        await query.edit_message_text(
            f"⚙️ Converting to {label}... Please wait."
        )

        converted = await asyncio.to_thread(
            convert_video_file,
            item["path"],
            mode,
        )

        with converted.open("rb") as media:
            if mode == "aac":
                await query.message.reply_audio(
                    audio=media,
                    filename=converted.name,
                    caption="Hi there, I've created a new file",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )
            else:
                await query.message.reply_video(
                    video=media,
                    filename=converted.name,
                    supports_streaming=True,
                    caption="Hi there, I've created a new file",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )

        await query.edit_message_text(
            f"✅ Your converted file is ready!\n"
            f"📁 File: {converted.name}\n"
            "🙏 Thanks for using our Service ⚡"
        )

    except Exception as exc:
        log.exception("Conversion failed")

        try:
            await query.edit_message_text(
                f"❌ Conversion failed.\n{str(exc)[:900]}"
            )
        except Exception:
            await query.message.reply_text(
                f"❌ Conversion failed.\n{str(exc)[:900]}"
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
        "🔗 Send a supported public media link.\n\n"
        "Supported platforms include:\n"
        "• YouTube videos and Shorts\n"
        "• Instagram posts, Reels, Stories and carousels\n"
        "• Facebook and TikTok\n"
        "• X/Twitter, Reddit and Pinterest\n"
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
        "• Private, deleted, restricted or login-protected content "
        "may not be available.\n"
        "• Platforms may change their systems and temporarily break downloads.\n"
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
                    log.warning(
                        "Skipping oversized file: %s",
                        path.name,
                    )
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
            first_video_token = None
            pending = context.application.bot_data.setdefault(
                "pending_conversions", {}
            )

            for path in deliverable[:MAX_CAROUSEL_FILES]:
                await send_downloaded_file(update.message, path)
                sent_count += 1

                if first_video_token is None and path.suffix.lower() in VIDEO_EXTENSIONS:
                    token = uuid.uuid4().hex[:12]
                    persistent_source = CONVERSION_DIR / f"{token}_{path.name}"
                    await asyncio.to_thread(shutil.copy2, path, persistent_source)
                    pending[token] = {
                        "path": str(persistent_source),
                        "user_id": update.effective_user.id,
                    }
                    first_video_token = token

            keyboard = None
            if first_video_token:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🚀 H.265 + AAC (smaller file)",
                        callback_data=f"convert:{first_video_token}:h265",
                    )],
                    [InlineKeyboardButton(
                        "🎵 Extract AAC audio (M4A)",
                        callback_data=f"convert:{first_video_token}:aac",
                    )],
                ])

            await status.edit_text(
                "✅ Download completed successfully!\n\n"
                f"📁 Files delivered: {sent_count}\n"
                "🙏 Thanks for using our Service ⚡",
                reply_markup=keyboard,
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
    error = context.error

    if isinstance(error, Conflict):
        log.error(
            "Telegram polling conflict: another process is using "
            "getUpdates with this BOT_TOKEN. Stop duplicate deployments "
            "or processes and keep exactly one polling instance running."
        )
        return

    log.error("Unhandled Telegram bot error", exc_info=error)


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
        CallbackQueryHandler(convert_callback, pattern=r"^convert:")
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
    log.info(
        "Polling mode requires exactly one running instance "
        "for this BOT_TOKEN."
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
