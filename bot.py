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

# Telegram upload safety limit
MAX_FILE_SIZE = 45 * 1024 * 1024

# Maximum video height after compression
MAX_VIDEO_HEIGHT = 720

# Maximum playlist items
MAX_PLAYLIST_ITEMS = 20


# ============================================================
# TEMP DIRECTORIES
# ============================================================

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
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
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
# COOKIE ENVIRONMENT VARIABLES
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

        # Handle Facebook login redirects
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

                if target.startswith("http"):
                    return target

        return url

    except Exception:

        return url


# ============================================================
# DIRECT MEDIA URL CHECK
# ============================================================

def is_direct_media_url(url: str) -> bool:

    try:

        path = urlparse(url).path.lower()

        return any(
            path.endswith(extension)
            for extension in ALL_MEDIA_EXTENSIONS
        )

    except Exception:

        return False


# ============================================================
# COOKIE FILE
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

        cookie_text = decoded.decode(
            "utf-8"
        )

    except Exception as error:

        logger.error(
            "Invalid Base64 cookies for %s: %s",
            platform,
            error,
        )

        return None

    if not (
        cookie_text.startswith(
            "# Netscape HTTP Cookie File"
        )
        or cookie_text.startswith(
            "# HTTP Cookie File"
        )
    ):

        logger.error(
            "Cookies for %s are not Netscape format.",
            platform,
        )

        return None

    try:

        cookie_file.write_text(
            cookie_text,
            encoding="utf-8",
        )

        return str(
            cookie_file
        )

    except Exception as error:

        logger.error(
            "Could not create cookie file: %s",
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
            "Accept-Language": "en-US,en;q=0.9",
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
        name = "Downloaded_Media"

    return name[:180]


# ============================================================
# CONTENT TYPE EXTENSION
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
# CLEANUP
# ============================================================

def cleanup_directory(
    path: Path,
):

    try:

        if path.exists():

            shutil.rmtree(
                path,
                ignore_errors=True,
            )

    except Exception as error:

        logger.warning(
            "Cleanup failed: %s",
            error,
        )


# ============================================================
# FIND DOWNLOADED MEDIA
# ============================================================

def find_media_files(
    directory: Path,
):

    files = []

    if not directory.exists():
        return files

    for path in directory.rglob("*"):

        if not path.is_file():
            continue

        if path.suffix.lower() in {
            ".part",
            ".ytdl",
            ".temp",
            ".json",
            ".description",
        }:
            continue

        try:

            if path.stat().st_size <= 0:
                continue

        except Exception:
            continue

        files.append(
            path
        )

    return sorted(
        files,
        key=lambda p: p.stat().st_mtime,
    )


# ============================================================
# FFMPEG
# ============================================================

def run_ffmpeg(
    command,
):

    logger.info(
        "FFmpeg: %s",
        " ".join(
            map(str, command)
        ),
    )

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:

        logger.error(
            result.stderr[-3000:]
        )

        raise RuntimeError(
            "FFmpeg failed."
        )

    return result


# ============================================================
# VIDEO INFORMATION
# ============================================================

def get_video_info(
    path: Path,
):

    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,codec_name",
        "-of",
        "default=noprint_wrappers=1",
        str(path),
    ]

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        values = {}

        for line in result.stdout.splitlines():

            if "=" in line:

                key, value = line.split(
                    "=",
                    1,
                )

                values[key.strip()] = (
                    value.strip()
                )

        width = int(
            values.get(
                "width",
                "0",
            )
        )

        height = int(
            values.get(
                "height",
                "0",
            )
        )

        codec = values.get(
            "codec_name"
        )

        return (
            width,
            height,
            codec,
        )

    except Exception:

        return (
            0,
            0,
            None,
        )


# ============================================================
# AUDIO CODEC
# ============================================================

def get_audio_codec(
    path: Path,
):

    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        codec = (
            result.stdout.strip()
        )

        return codec or None

    except Exception:

        return None


# ============================================================
# VIDEO CONVERSION
# ============================================================

def convert_video(
    input_path: Path,
    output_path: Path,
):

    width, height, _ = (
        get_video_info(
            input_path
        )
    )

    audio_codec = get_audio_codec(
        input_path
    )

    video_filter = []

    if (
        height
        and height > MAX_VIDEO_HEIGHT
    ):

        video_filter = [
            "-vf",
            f"scale=-2:{MAX_VIDEO_HEIGHT}",
        ]

    # Try to preserve original audio
    # when it is compatible with MP4.
    copy_audio_codecs = {
        "aac",
        "alac",
        "ac3",
        "eac3",
        "mp3",
    }

    if audio_codec in copy_audio_codecs:

        audio_options = [
            "-c:a",
            "copy",
        ]

    else:

        audio_options = [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),

        *video_filter,

        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",

        *audio_options,

        "-movflags",
        "+faststart",

        str(output_path),
    ]

    run_ffmpeg(
        command
    )

    return output_path


# ============================================================
# VIDEO SIZE PROCESSING
# ============================================================

def process_video_file(
    path: Path,
):

    try:

        size = path.stat().st_size

    except Exception:

        return path

    # Keep original video/audio untouched
    # when it is already small enough.
    if size <= MAX_FILE_SIZE:

        return path

    logger.info(
        "Compressing %.2f MB video",
        size / (1024 * 1024),
    )

    output_path = (
        path.parent
        / f"compressed_{path.stem}.mp4"
    )

    try:

        convert_video(
            path,
            output_path,
        )

        if (
            output_path.exists()
            and output_path.stat().st_size
            <= MAX_FILE_SIZE
        ):

            try:
                path.unlink()
            except Exception:
                pass

            return output_path

        # Second, stronger compression
        aggressive_output = (
            path.parent
            / f"compressed2_{path.stem}.mp4"
        )

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(path),

            "-vf",
            "scale=-2:480",

            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-crf",
            "32",

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-movflags",
            "+faststart",

            str(aggressive_output),
        ]

        run_ffmpeg(
            command
        )

        if (
            aggressive_output.exists()
            and aggressive_output.stat().st_size
            <= MAX_FILE_SIZE
        ):

            try:
                path.unlink()
            except Exception:
                pass

            try:

                if output_path.exists():
                    output_path.unlink()

            except Exception:
                pass

            return aggressive_output

        raise RuntimeError(
            "Unable to compress video below Telegram's upload limit."
        )

    except Exception:

        try:

            if output_path.exists():
                output_path.unlink()

        except Exception:
            pass

        raise


# ============================================================
# DIRECT MEDIA DOWNLOAD
# ============================================================

def direct_download(
    url: str,
    output_dir: Path,
):

    session = create_http_session()

    logger.info(
        "Direct download: %s",
        url,
    )

    response = session.get(
        url,
        stream=True,
        timeout=60,
        allow_redirects=True,
    )

    response.raise_for_status()

    content_type = response.headers.get(
        "Content-Type",
        "",
    )

    content_length = response.headers.get(
        "Content-Length"
    )

    if content_length:

        try:

            if (
                int(content_length)
                > MAX_FILE_SIZE
            ):

                raise RuntimeError(
                    "The media is larger than Telegram's upload limit."
                )

        except ValueError:
            pass

    extension = (
        extension_from_content_type(
            content_type
        )
    )

    filename = (
        "Downloaded_Media"
        + extension
    )

    content_disposition = (
        response.headers.get(
            "Content-Disposition",
            "",
        )
    )

    match = re.search(
        r'filename="?([^"]+)"?',
        content_disposition,
        re.IGNORECASE,
    )

    if match:

        supplied_name = safe_filename(
            match.group(1)
        )

        if Path(
            supplied_name
        ).suffix:

            filename = supplied_name

    output_path = (
        output_dir
        / filename
    )

    total = 0

    with open(
        output_path,
        "wb",
    ) as file:

        for chunk in response.iter_content(
            chunk_size=1024 * 1024
        ):

            if not chunk:
                continue

            total += len(chunk)

            if total > MAX_FILE_SIZE:

                try:
                    file.close()
                except Exception:
                    pass

                try:
                    output_path.unlink()
                except Exception:
                    pass

                raise RuntimeError(
                    "The media is larger than Telegram's upload limit."
                )

            file.write(chunk)

    return [
        output_path
    ]


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    output_dir: Path,
    platform: str,
):

    options = {

        # ====================================================
        # IMPORTANT:
        # Clean filename.
        #
        # No:
        # %(title)s
        # %(uploader)s
        # %(id)s
        #
        # ====================================================

        "outtmpl": str(
            output_dir
            / "Downloaded_Media.%(ext)s"
        ),

        # Best video + best available audio
        "format": (
            "bestvideo*+bestaudio/best"
        ),

        # Merge video/audio into MP4
        "merge_output_format": "mp4",

        # Playlist support
        "noplaylist": False,

        "playlistend": MAX_PLAYLIST_ITEMS,

        # Performance
        "concurrent_fragment_downloads": 4,

        "retries": 3,

        "fragment_retries": 3,

        "socket_timeout": 30,

        # Logging
        "quiet": True,

        "no_warnings": True,

        # Don't generate extra files
        "writethumbnail": False,

        "writeinfojson": False,

        "writedescription": False,

        "writeautomaticsub": False,

        "writesubtitles": False,

        # HTTP
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": (
                "en-US,en;q=0.9"
            ),
        },
    }

    # ========================================================
    # PLATFORM COOKIES
    # ========================================================

    cookie_file = create_cookie_file(
        platform
    )

    if cookie_file:

        options[
            "cookiefile"
        ] = cookie_file

    # ========================================================
    # CHROME IMPERSONATION
    # ========================================================

    options[
        "impersonate"
    ] = "chrome"

    return options


# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def yt_dlp_download(
    url: str,
    output_dir: Path,
    platform: str,
):

    options = build_ydl_options(
        output_dir,
        platform,
    )

    logger.info(
        "yt-dlp downloading: %s",
        url,
    )

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            ydl.download(
                [url]
            )

    except Exception as first_error:

        logger.warning(
            "yt-dlp Chrome impersonation failed: %s",
            first_error,
        )

        # Retry without impersonation
        options.pop(
            "impersonate",
            None,
        )

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            ydl.download(
                [url]
            )


# ============================================================
# DOWNLOAD MEDIA
# ============================================================

def download_media(
    url: str,
):

    job_id = next(
        tempfile._get_candidate_names()
    )

    job_dir = (
        DOWNLOAD_DIR
        / job_id
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    platform = detect_platform(
        url
    )

    try:

        # ====================================================
        # DIRECT MEDIA
        # ====================================================

        if is_direct_media_url(
            url
        ):

            files = direct_download(
                url,
                job_dir,
            )

        # ====================================================
        # YT-DLP
        # ====================================================

        else:

            yt_dlp_download(
                url,
                job_dir,
                platform,
            )

            files = find_media_files(
                job_dir
            )

        if not files:

            raise RuntimeError(
                "No media files were found."
            )

        processed_files = []

        for path in files:

            suffix = (
                path.suffix.lower()
            )

            # =================================================
            # VIDEO
            # =================================================

            if suffix in VIDEO_EXTENSIONS:

                processed = (
                    process_video_file(
                        path
                    )
                )

                processed_files.append(
                    processed
                )

            # =================================================
            # AUDIO / IMAGE / GIF
            # =================================================

            elif suffix in (
                AUDIO_EXTENSIONS
                | IMAGE_EXTENSIONS
                | OTHER_MEDIA_EXTENSIONS
            ):

                try:

                    size = path.stat().st_size

                except FileNotFoundError:

                    continue

                if size > MAX_FILE_SIZE:

                    raise RuntimeError(
                        f"{path.name} is larger than Telegram's upload limit."
                    )

                processed_files.append(
                    path
                )

        if not processed_files:

            raise RuntimeError(
                "No supported media files were found."
            )

        return (
            job_dir,
            processed_files,
        )

    except Exception:

        cleanup_directory(
            job_dir
        )

        raise


# ============================================================
# SEND FILE
# ============================================================

async def send_file(
    update: Update,
    path: Path,
):

    suffix = (
        path.suffix.lower()
    )

    size = path.stat().st_size

    if size > MAX_FILE_SIZE:

        raise RuntimeError(
            "File is larger than Telegram's upload limit."
        )

    # ========================================================
    # VIDEO
    # ========================================================

    if suffix in VIDEO_EXTENSIONS:

        await update.message.chat.send_action(
            ChatAction.UPLOAD_VIDEO
        )

        with open(
            path,
            "rb",
        ) as file:

            await update.message.reply_video(
                video=file,

                # Clean caption
                caption="Downloaded Media",

                supports_streaming=True,
            )

        return

    # ========================================================
    # AUDIO
    # ========================================================

    if suffix in AUDIO_EXTENSIONS:

        await update.message.chat.send_action(
            ChatAction.UPLOAD_AUDIO
        )

        with open(
            path,
            "rb",
        ) as file:

            await update.message.reply_audio(
                audio=file,

                # Clean caption
                caption="Downloaded Media",
            )

        return

    # ========================================================
    # IMAGE
    # ========================================================

    if suffix in IMAGE_EXTENSIONS:

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

    if suffix == ".gif":

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

            # Clean caption
            caption="Downloaded Media",
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
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "👋 Welcome to Universal Media Downloader!\n\n"

        "📥 Send me any media link.\n\n"

        "⚡ I will download the available media "
        "and audio.\n\n"

        "🎵 Original audio is preserved whenever "
        "the source/container allows it."
    )


# ============================================================
# /HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "📥 Universal Media Downloader\n\n"

        "Send a media URL and I will process it.\n\n"

        "Supported platforms include:\n\n"

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

        "Direct media URLs are also supported.\n\n"

        "🎵 Original available audio is preserved "
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
        "URL received | %s | %s",
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
                            "Downloading media..."
                        ),
                    )
                )

        # ====================================================
        # DOWNLOAD
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
        # DELETE PROCESSING MESSAGE
        # ====================================================

        if processing_message:

            try:

                await processing_message.delete()

            except Exception:
                pass

            processing_message = None

        # ====================================================
        # SEND DOWNLOADED MEDIA
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
                    "Upload error: %s",
                    error,
                )

                await update.message.reply_text(
                    "❌ Media was downloaded, "
                    "but Telegram could not upload it.\n\n"
                    f"Reason: {error}"
                )

        if sent_count == 0:

            await update.message.reply_text(
                "❌ No media could be sent."
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

            "Technical reason:\n"
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

    # /start
    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    # /help
    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    # URLs sent as normal messages
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    # Error handler
    application.add_error_handler(
        telegram_error_handler
    )

    # Start bot
    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# START PROGRAM
# ============================================================

if __name__ == "__main__":
    main()
