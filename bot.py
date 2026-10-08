import os
import re
import base64
import asyncio
import logging
import shutil
import subprocess
import tempfile

from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import requests
import yt_dlp

from PIL import Image, ImageDraw, ImageFont

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

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Telegram practical upload limit
MAX_FILE_SIZE = 45 * 1024 * 1024

# Maximum video height when compression is required
MAX_VIDEO_HEIGHT = 720

# Maximum number of playlist items
MAX_PLAYLIST_ITEMS = 20

# Temporary working directory
BASE_DIR = (
    Path(tempfile.gettempdir())
    / "universal_media_downloader"
)

DOWNLOAD_DIR = BASE_DIR / "downloads"
COOKIE_DIR = BASE_DIR / "cookies"
LIGHTNING_GIF = BASE_DIR / "lightning.gif"

BASE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

DOWNLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

COOKIE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(
    "UniversalDownloader"
)


# ============================================================
# MEDIA EXTENSIONS
# ============================================================

VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".flv",
    ".3gp",
    ".ts",
    ".mpeg",
    ".mpg",
    ".m2ts",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".flac",
    ".weba",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}

OTHER_MEDIA_EXTENSIONS = {
    ".gif",
    ".svg",
}

ALL_MEDIA_EXTENSIONS = (
    VIDEO_EXTENSIONS
    | AUDIO_EXTENSIONS
    | IMAGE_EXTENSIONS
    | OTHER_MEDIA_EXTENSIONS
)


# ============================================================
# PLATFORM COOKIE VARIABLES
# ============================================================

COOKIE_ENV = {
    "instagram": "INSTAGRAM_COOKIES_B64",
    "facebook": "FACEBOOK_COOKIES_B64",
    "youtube": "YOUTUBE_COOKIES_B64",
    "twitter": "TWITTER_COOKIES_B64",
    "tiktok": "TIKTOK_COOKIES_B64",
    "reddit": "REDDIT_COOKIES_B64",
    "pinterest": "PINTEREST_COOKIES_B64",
    "snapchat": "SNAPCHAT_COOKIES_B64",
    "linkedin": "LINKEDIN_COOKIES_B64",
    "vimeo": "VIMEO_COOKIES_B64",
    "dailymotion": "DAILYMOTION_COOKIES_B64",
    "twitch": "TWITCH_COOKIES_B64",
}


# ============================================================
# USER AGENT
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)


# ============================================================
# PLATFORM DETECTION
# ============================================================

def detect_platform(url: str) -> str:

    try:
        host = urlparse(url).netloc.lower()

        if host.startswith("www."):
            host = host[4:]

        if "instagram.com" in host:
            return "instagram"

        if (
            "facebook.com" in host
            or "fb.watch" in host
        ):
            return "facebook"

        if (
            "youtube.com" in host
            or "youtu.be" in host
        ):
            return "youtube"

        if (
            "twitter.com" in host
            or host == "x.com"
            or host.endswith(".x.com")
        ):
            return "twitter"

        if "tiktok.com" in host:
            return "tiktok"

        if (
            "reddit.com" in host
            or "redd.it" in host
        ):
            return "reddit"

        if (
            "pinterest.com" in host
            or "pin.it" in host
        ):
            return "pinterest"

        if "snapchat.com" in host:
            return "snapchat"

        if "linkedin.com" in host:
            return "linkedin"

        if "vimeo.com" in host:
            return "vimeo"

        if "dailymotion.com" in host:
            return "dailymotion"

        if "twitch.tv" in host:
            return "twitch"

    except Exception:
        pass

    return "generic"


# ============================================================
# URL EXTRACTION
# ============================================================

URL_PATTERN = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)


def extract_url(text: str):

    if not text:
        return None

    match = URL_PATTERN.search(text)

    if not match:
        return None

    url = match.group(0)

    return url.rstrip(
        ".,!?;:)]}>\"'"
    )


# ============================================================
# URL NORMALIZATION
# ============================================================

def normalize_url(url: str) -> str:

    try:

        parsed = urlparse(url)

        host = parsed.netloc.lower()

        # Facebook login redirect
        if (
            "facebook.com" in host
            and parsed.path.startswith("/login")
        ):

            query = parse_qs(
                parsed.query
            )

            next_values = query.get(
                "next"
            )

            if next_values:

                target = unquote(
                    next_values[0]
                )

                if target.startswith(
                    "http"
                ):
                    return target

        return url

    except Exception:

        return url


# ============================================================
# DIRECT MEDIA URL
# ============================================================

def is_direct_media_url(url: str) -> bool:

    try:

        path = urlparse(url).path.lower()

        return any(
            path.endswith(extension)
            for extension
            in ALL_MEDIA_EXTENSIONS
        )

    except Exception:

        return False


# ============================================================
# COOKIE FILE CREATION
# ============================================================

def create_cookie_file(
    platform: str,
):

    env_name = COOKIE_ENV.get(
        platform
    )

    if not env_name:
        return None

    encoded = os.getenv(
        env_name
    )

    if not encoded:
        logger.info(
            "No cookies configured for %s",
            platform,
        )
        return None

    encoded = encoded.strip()

    cookie_file = (
        COOKIE_DIR
        / f"{platform}.txt"
    )

    try:

        decoded = base64.b64decode(
            encoded,
            validate=True,
        )

        text = decoded.decode(
            "utf-8"
        )

    except Exception as error:

        logger.error(
            "Invalid Base64 cookies for %s: %s",
            platform,
            error,
        )

        return None

    valid_header = (
        text.startswith(
            "# Netscape HTTP Cookie File"
        )
        or text.startswith(
            "# HTTP Cookie File"
        )
    )

    if not valid_header:

        logger.error(
            "%s cookies are not Netscape format",
            platform,
        )

        return None

    try:

        cookie_file.write_text(
            text,
            encoding="utf-8",
            newline="\n",
        )

        return str(
            cookie_file
        )

    except Exception as error:

        logger.error(
            "Could not write cookie file: %s",
            error,
        )

        return None


# ============================================================
# HTTP SESSION
# ============================================================

def create_http_session():

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": (
                "en-US,en;q=0.9"
            ),
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,image/webp,"
                "*/*;q=0.8"
            ),
            "Connection": "keep-alive",
        }
    )

    return session


# ============================================================
# SAFE FILENAME
# ============================================================

def safe_filename(
    name: str,
) -> str:

    name = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "_",
        name,
    )

    name = name.strip(
        " ."
    )

    if not name:
        name = "download"

    return name[:180]


# ============================================================
# CONTENT TYPE -> EXTENSION
# ============================================================

def extension_from_content_type(
    content_type: str,
):

    content_type = (
        content_type
        .split(";")[0]
        .strip()
        .lower()
    )

    mapping = {

        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",

        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/flac": ".flac",
        "audio/webm": ".weba",

        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/svg+xml": ".svg",
    }

    return mapping.get(
        content_type,
        ".bin",
    )


# ============================================================
# DIRECT MEDIA DOWNLOAD
# ============================================================

def direct_download(
    url: str,
    output_dir: Path,
):

    session = create_http_session()

    response = session.get(
        url,
        stream=True,
        timeout=(20, 120),
        allow_redirects=True,
    )

    response.raise_for_status()

    content_type = response.headers.get(
        "Content-Type",
        "",
    )

    filename = Path(
        urlparse(
            response.url
        ).path
    ).name

    filename = safe_filename(
        filename
    )

    extension = Path(
        filename
    ).suffix.lower()

    if not extension:

        extension = (
            extension_from_content_type(
                content_type
            )
        )

        filename += extension

    output_file = (
        output_dir
        / filename
    )

    total = 0

    with open(
        output_file,
        "wb",
    ) as file:

        for chunk in response.iter_content(
            chunk_size=256 * 1024
        ):

            if not chunk:
                continue

            total += len(chunk)

            if total > MAX_FILE_SIZE:

                file.close()

                try:
                    output_file.unlink()
                except Exception:
                    pass

                raise RuntimeError(
                    "The file is larger than the Telegram upload limit."
                )

            file.write(chunk)

    return output_file


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    url: str,
    platform: str,
    output_dir: Path,
    use_impersonation: bool = True,
):

    cookie_file = (
        create_cookie_file(
            platform
        )
    )

    options = {

        "outtmpl": str(
            output_dir
            / "%(title).120s_%(id)s.%(ext)s"
        ),

        # ====================================================
        # ORIGINAL VIDEO + ORIGINAL AVAILABLE AUDIO
        # ====================================================

        "format": (
            "bestvideo*+bestaudio/best"
        ),

        # MP4 container when merging is required.
        "merge_output_format": "mp4",

        "noplaylist": False,

        "playlistend": MAX_PLAYLIST_ITEMS,

        "quiet": True,

        "no_warnings": False,

        "retries": 3,

        "fragment_retries": 3,

        "file_access_retries": 3,

        "socket_timeout": 30,

        "continuedl": True,

        "overwrites": False,

        "max_filesize": MAX_FILE_SIZE,

        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": (
                "en-US,en;q=0.9"
            ),
        },

        "writethumbnail": False,

        "writeinfojson": False,

        "writesubtitles": False,

        "writeautomaticsub": False,

        "download_archive": None,

        "restrictfilenames": False,

        # Avoid unnecessary postprocessing.
        "postprocessors": [],

    }

    if cookie_file:

        options[
            "cookiefile"
        ] = cookie_file

    if use_impersonation:

        options[
            "impersonate"
        ] = "chrome"

    return options


# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def run_ytdlp(
    url: str,
    platform: str,
    output_dir: Path,
    use_impersonation: bool,
):

    options = build_ydl_options(
        url=url,
        platform=platform,
        output_dir=output_dir,
        use_impersonation=use_impersonation,
    )

    logger.info(
        "yt-dlp | platform=%s | impersonation=%s",
        platform,
        use_impersonation,
    )

    with yt_dlp.YoutubeDL(
        options
    ) as ydl:

        info = ydl.extract_info(
            url,
            download=True,
        )

        if not info:

            raise RuntimeError(
                "yt-dlp did not return media information."
            )

    files = find_media_files(
        output_dir
    )

    if not files:

        raise RuntimeError(
            "yt-dlp completed but no media file was created."
        )

    return files


# ============================================================
# FIND MEDIA FILES
# ============================================================

def find_media_files(
    directory: Path,
):

    if not directory.exists():
        return []

    result = []

    for path in directory.rglob("*"):

        if not path.is_file():
            continue

        ignored_suffixes = (
            ".part",
            ".ytdl",
            ".temp",
        )

        if path.name.endswith(
            ignored_suffixes
        ):
            continue

        if path.suffix.lower() in {
            ".json",
            ".description",
        }:
            continue

        try:

            if path.stat().st_size <= 0:
                continue

        except Exception:
            continue

        result.append(path)

    return sorted(
        result,
        key=lambda item: item.stat().st_mtime,
    )


# ============================================================
# COMMAND RUNNER
# ============================================================

def run_command(
    command,
    timeout=300,
):

    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


# ============================================================
# MEDIA PROBE
# ============================================================

def get_video_audio_info(
    path: Path,
):

    try:

        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        )

        if result.returncode != 0:
            return None

        import json

        return json.loads(
            result.stdout
        )

    except Exception as error:

        logger.warning(
            "ffprobe failed: %s",
            error,
        )

        return None


def has_audio_stream(
    path: Path,
) -> bool:

    data = get_video_audio_info(
        path
    )

    if not data:
        return False

    streams = data.get(
        "streams",
        []
    )

    return any(
        stream.get("codec_type")
        == "audio"
        for stream in streams
    )


def get_audio_codec(
    path: Path,
):

    data = get_video_audio_info(
        path
    )

    if not data:
        return None

    for stream in data.get(
        "streams",
        []
    ):

        if (
            stream.get(
                "codec_type"
            )
            == "audio"
        ):

            return (
                stream.get(
                    "codec_name"
                )
            )

    return None


# ============================================================
# ORIGINAL AUDIO COMPATIBILITY
# ============================================================

def audio_codec_is_mp4_compatible(
    codec: str | None,
) -> bool:

    if not codec:
        return False

    # Common codecs that can safely be used
    # in MP4 without audio re-encoding.
    compatible = {
        "aac",
        "alac",
        "ac3",
        "eac3",
        "mp3",
    }

    return codec.lower() in compatible


# ============================================================
# VIDEO CONVERSION
# ============================================================

def convert_video(
    path: Path,
):

    output = path.with_name(
        f"{path.stem}_converted.mp4"
    )

    audio_exists = has_audio_stream(
        path
    )

    audio_codec = get_audio_codec(
        path
    )

    logger.info(
        "Video conversion | audio=%s | codec=%s",
        audio_exists,
        audio_codec,
    )

    # ========================================================
    # FIRST ATTEMPT
    #
    # Video is converted to H264.
    # Original audio is COPIED whenever possible.
    # ========================================================

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(path),

        "-map",
        "0:v:0",

        "-vf",
        (
            f"scale="
            f"'min(1280,iw)':"
            f"'min({MAX_VIDEO_HEIGHT},ih)':"
            f"force_original_aspect_ratio=decrease"
        ),

        "-r",
        "30",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "28",

        "-pix_fmt",
        "yuv420p",
    ]

    if (
        audio_exists
        and audio_codec_is_mp4_compatible(
            audio_codec
        )
    ):

        # IMPORTANT:
        # Keep original audio stream.
        command.extend(
            [
                "-map",
                "0:a:0?",
                "-c:a",
                "copy",
            ]
        )

    elif audio_exists:

        # ====================================================
        # Original codec cannot safely be placed in MP4.
        # Fallback only when necessary.
        # ====================================================

        command.extend(
            [
                "-map",
                "0:a:0?",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ]
        )

    else:

        command.extend(
            [
                "-an",
            ]
        )

    command.extend(
        [
            "-movflags",
            "+faststart",

            str(output),
        ]
    )

    logger.info(
        "FFmpeg conversion started: %s",
        path.name,
    )

    result = run_command(
        command,
        timeout=600,
    )

    if result.returncode != 0:

        logger.warning(
            "First FFmpeg conversion failed."
        )

        logger.warning(
            "%s",
            result.stderr[-3000:],
        )

        # ====================================================
        # FALLBACK
        #
        # Always use AAC if copying original audio failed.
        # ====================================================

        if audio_exists:

            fallback_command = [
                "ffmpeg",
                "-y",

                "-i",
                str(path),

                "-map",
                "0:v:0",

                "-vf",
                (
                    f"scale="
                    f"'min(1280,iw)':"
                    f"'min({MAX_VIDEO_HEIGHT},ih)':"
                    f"force_original_aspect_ratio=decrease"
                ),

                "-r",
                "30",

                "-c:v",
                "libx264",

                "-preset",
                "veryfast",

                "-crf",
                "28",

                "-pix_fmt",
                "yuv420p",

                "-map",
                "0:a:0?",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-movflags",
                "+faststart",

                str(output),
            ]

            result = run_command(
                fallback_command,
                timeout=600,
            )

    if result.returncode != 0:

        logger.error(
            "FFmpeg failed:\n%s",
            result.stderr[-5000:],
        )

        raise RuntimeError(
            "FFmpeg could not convert the video."
        )

    if not output.exists():

        raise RuntimeError(
            "FFmpeg did not create the converted video."
        )

    try:
        path.unlink()
    except Exception:
        pass

    return output


# ============================================================
# VIDEO PROCESSING
# ============================================================

def process_video_file(
    path: Path,
):

    if not path.exists():
        return path

    size = path.stat().st_size

    # ========================================================
    # IMPORTANT:
    #
    # If the downloaded file is already below Telegram limit,
    # DO NOT TOUCH IT.
    #
    # This means original video AND original audio remain
    # exactly as downloaded.
    # ========================================================

    if size <= MAX_FILE_SIZE:

        logger.info(
            "Video is within size limit. "
            "Keeping original file unchanged: %s",
            path.name,
        )

        return path

    logger.info(
        "Video exceeds Telegram limit. "
        "Converting: %s",
        path.name,
    )

    return convert_video(
        path
    )


# ============================================================
# CLEAN DIRECTORY
# ============================================================

def cleanup_directory(
    directory: Path,
):

    if not directory.exists():
        return

    try:

        shutil.rmtree(
            directory,
            ignore_errors=True,
        )

    except Exception as error:

        logger.warning(
            "Cleanup failed: %s",
            error,
        )


# ============================================================
# UNIVERSAL DOWNLOAD
# ============================================================

def download_media(
    url: str,
):

    url = normalize_url(
        url
    )

    platform = detect_platform(
        url
    )

    job_name = next(
        tempfile._get_candidate_names()
    )

    job_dir = (
        DOWNLOAD_DIR
        / job_name
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.info(
        "New job | %s | %s",
        platform,
        url,
    )

    try:

        # ====================================================
        # DIRECT MEDIA
        # ====================================================

        if is_direct_media_url(
            url
        ):

            logger.info(
                "Direct media URL."
            )

            file_path = direct_download(
                url,
                job_dir,
            )

            return (
                job_dir,
                [file_path],
            )

        # ====================================================
        # YT-DLP ATTEMPT
        # ====================================================

        first_error = None

        try:

            files = run_ytdlp(
                url=url,
                platform=platform,
                output_dir=job_dir,
                use_impersonation=True,
            )

        except Exception as error:

            first_error = error

            logger.warning(
                "First attempt failed: %s",
                error,
            )

            # Remove partial files.
            for item in job_dir.iterdir():

                try:

                    if item.is_file():
                        item.unlink()

                    elif item.is_dir():
                        shutil.rmtree(
                            item,
                            ignore_errors=True,
                        )

                except Exception:
                    pass

            # =================================================
            # SECOND ATTEMPT
            # =================================================

            files = run_ytdlp(
                url=url,
                platform=platform,
                output_dir=job_dir,
                use_impersonation=False,
            )

        # ====================================================
        # PROCESS FILES
        # ====================================================

        final_files = []

        for file_path in files:

            if not file_path.exists():
                continue

            extension = (
                file_path.suffix.lower()
            )

            # ------------------------------------------------
            # VIDEO
            # ------------------------------------------------

            if extension in VIDEO_EXTENSIONS:

                file_path = (
                    process_video_file(
                        file_path
                    )
                )

            # ------------------------------------------------
            # SIZE CHECK
            # ------------------------------------------------

            if not file_path.exists():
                continue

            file_size = (
                file_path.stat().st_size
            )

            if file_size <= 0:
                continue

            if file_size > MAX_FILE_SIZE:

                logger.warning(
                    "Skipping oversized file: %s",
                    file_path.name,
                )

                continue

            final_files.append(
                file_path
            )

        if not final_files:

            raise RuntimeError(
                "No usable media files were produced."
            )

        return (
            job_dir,
            final_files,
        )

    except Exception:

        cleanup_directory(
            job_dir
        )

        raise


# ============================================================
# FILE TYPE
# ============================================================

def get_file_type(
    path: Path,
):

    extension = (
        path.suffix.lower()
    )

    if extension in VIDEO_EXTENSIONS:
        return "video"

    if extension in AUDIO_EXTENSIONS:
        return "audio"

    if extension in IMAGE_EXTENSIONS:
        return "image"

    if extension == ".gif":
        return "gif"

    if extension == ".svg":
        return "svg"

    return "document"


# ============================================================
# TELEGRAM SEND
# ============================================================

async def send_file(
    update: Update,
    path: Path,
):

    if not path.exists():
        return

    if path.stat().st_size <= 0:
        return

    if path.stat().st_size > MAX_FILE_SIZE:

        raise RuntimeError(
            f"{path.name} is too large for Telegram."
        )

    file_type = get_file_type(
        path
    )

    logger.info(
        "Uploading | %s | %s",
        path.name,
        file_type,
    )

    # ========================================================
    # VIDEO
    # ========================================================

    if file_type == "video":

        await update.message.chat.send_action(
            ChatAction.UPLOAD_VIDEO
        )

        with open(
            path,
            "rb",
        ) as file:

            await update.message.reply_video(
                video=file,
                supports_streaming=True,
                caption=path.name[:100],
            )

        return

    # ========================================================
    # AUDIO
    # ========================================================

    if file_type == "audio":

        await update.message.chat.send_action(
            ChatAction.UPLOAD_AUDIO
        )

        with open(
            path,
            "rb",
        ) as file:

            await update.message.reply_audio(
                audio=file,
                caption=path.name[:100],
            )

        return

    # ========================================================
    # IMAGE
    # ========================================================

    if file_type == "image":

        await update.message.chat.send_action(
            ChatAction.UPLOAD_PHOTO
        )

        with open(
            path,
            "rb",
        ) as file:

            await update.message.reply_photo(
                photo=file
            )

        return

    # ========================================================
    # GIF
    # ========================================================

    if file_type == "gif":

        await update.message.chat.send_action(
            ChatAction.UPLOAD_DOCUMENT
        )

        with open(
            path,
            "rb",
        ) as file:

            await update.message.reply_animation(
                animation=file
            )

        return

    # ========================================================
    # DOCUMENT
    # ========================================================

    await update.message.chat.send_action(
        ChatAction.UPLOAD_DOCUMENT
    )

    with open(
        path,
        "rb",
    ) as file:

        await update.message.reply_document(
            document=file,
            caption=path.name[:100],
        )


# ============================================================
# LIGHTNING GIF
# ============================================================

def create_lightning_gif():

    if LIGHTNING_GIF.exists():
        return

    frames = []

    try:

        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans-Bold.ttf",
            42,
        )

    except Exception:

        font = ImageFont.load_default()

    for frame_number in range(8):

        image = Image.new(
            "RGB",
            (500, 180),
            "black",
        )

        draw = ImageDraw.Draw(
            image
        )

        text = "⚡ Processing..."

        try:

            box = draw.textbbox(
                (0, 0),
                text,
                font=font,
            )

            text_width = (
                box[2] - box[0]
            )

            text_height = (
                box[3] - box[1]
            )

            x = (
                500
                - text_width
            ) // 2

            y = (
                180
                - text_height
            ) // 2

            y += (
                frame_number % 4
            )

            draw.text(
                (x, y),
                text,
                fill="white",
                font=font,
            )

        except Exception:

            draw.text(
                (170, 80),
                "PROCESSING...",
                fill="white",
                font=font,
            )

        frames.append(
            image
        )

    frames[0].save(
        LIGHTNING_GIF,
        save_all=True,
        append_images=frames[1:],
        duration=140,
        loop=0,
    )


# ============================================================
# START COMMAND
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "👋 Welcome to Universal Media Downloader!\n\n"

        "📥 Send me any supported media link.\n\n"

        "⚡ I will download the available media "
        "with its available audio.\n\n"

        "🎵 Original audio is preserved whenever "
        "the source/container allows it."
    )


# ============================================================
# HELP COMMAND
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "📥 Universal Media Downloader\n\n"

        "Send a media URL.\n\n"

        "Supported platforms include:\n"
        "• Instagram\n"
        "• Facebook\n"
        "• YouTube\n"
        "• TikTok\n"
        "• X / Twitter\n"
        "• Reddit\n"
        "• Pinterest\n"
        "• Snapchat\n"
        "• LinkedIn\n"
        "• Vimeo\n"
        "• Dailymotion\n"
        "• Twitch\n\n"

        "Also supports direct:\n"
        "• MP4\n"
        "• MOV\n"
        "• WebM\n"
        "• MP3\n"
        "• M4A\n"
        "• WAV\n"
        "• JPG\n"
        "• PNG\n"
        "• GIF\n"
        "• WebP\n\n"

        "🎵 Original available audio is kept "
        "whenever possible."
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = (
        update.message.text
        or ""
    )

    url = extract_url(
        text
    )

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid media URL."
        )

        return

    url = normalize_url(
        url
    )

    platform = detect_platform(
        url
    )

    logger.info(
        "Incoming URL | platform=%s | %s",
        platform,
        url,
    )

    processing_message = None
    job_dir = None

    try:

        # ====================================================
        # PROCESSING GIF
        # ====================================================

        create_lightning_gif()

        if LIGHTNING_GIF.exists():

            with open(
                LIGHTNING_GIF,
                "rb",
            ) as gif:

                processing_message = (
                    await update.message.reply_animation(
                        animation=gif,
                        caption=(
                            "⚡ Thanks for providing the link!\n\n"
                            f"Platform: {platform.title()}\n"
                            "Downloading media + available audio..."
                        ),
                    )
                )

        # ====================================================
        # DOWNLOAD IN WORKER
        # ====================================================

        loop = (
            asyncio.get_running_loop()
        )

        job_dir, files = (
            await loop.run_in_executor(
                None,
                lambda: download_media(
                    url
                ),
            )
        )

        if not files:

            raise RuntimeError(
                "No media was found."
            )

        # ====================================================
        # REMOVE PROCESSING MESSAGE
        # ====================================================

        if processing_message:

            try:

                await processing_message.delete()

            except Exception:
                pass

            processing_message = None

        # ====================================================
        # SEND FILES
        # ====================================================

        sent_count = 0

        for file_path in files:

            try:

                await send_file(
                    update,
                    file_path,
                )

                sent_count += 1

            except Exception as error:

                logger.error(
                    "Upload failed: %s",
                    error,
                )

                await update.message.reply_text(
                    "❌ The media was downloaded, "
                    "but Telegram could not upload it.\n\n"
                    f"Reason: {error}"
                )

        if sent_count == 0:

            await update.message.reply_text(
                "❌ No media could be sent to Telegram."
            )

    except Exception as error:

        logger.exception(
            "Download error"
        )

        if processing_message:

            try:

                await processing_message.delete()

            except Exception:
                pass

        error_text = str(
            error
        )

        if len(error_text) > 1800:

            error_text = (
                error_text[-1800:]
            )

        await update.message.reply_text(
            "❌ I couldn't download this media.\n\n"

            f"🌐 Platform: {platform.title()}\n\n"

            f"Technical reason:\n"
            f"{error_text}\n\n"

            "Possible reasons:\n"
            "• The media is private\n"
            "• Login is required\n"
            "• Cookies are missing or expired\n"
            "• The URL has expired\n"
            "• The website blocked the request\n"
            "• The website changed its extraction method\n"
            "• The media is larger than Telegram allows"
        )

    finally:

        if job_dir:

            cleanup_directory(
                job_dir
            )


# ============================================================
# TELEGRAM ERROR HANDLER
# ============================================================

async def telegram_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Telegram error: %s",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    create_lightning_gif()

    logger.info(
        "=========================================="
    )

    logger.info(
        "Universal Media Downloader starting"
    )

    logger.info(
        "yt-dlp version: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "=========================================="
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    application.add_error_handler(
        telegram_error_handler
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
