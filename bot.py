import os
import re
import json
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

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is missing."
    )

# Practical Telegram upload limit
MAX_FILE_SIZE = 45 * 1024 * 1024

# Maximum video height when conversion is necessary
MAX_VIDEO_HEIGHT = 720

# Maximum playlist items
MAX_PLAYLIST_ITEMS = 20

# FFmpeg timeout
FFMPEG_TIMEOUT = 90

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
# USER AGENT
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
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
            for extension in ALL_MEDIA_EXTENSIONS
        )

    except Exception:
        return False


# ============================================================
# COOKIE FILE
# ============================================================

def create_cookie_file(platform: str):

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

        text = decoded.decode(
            "utf-8"
        )

    except Exception as error:

        logger.error(
            "Invalid cookies for %s: %s",
            platform,
            error,
        )

        return None

    if not (
        text.startswith(
            "# Netscape HTTP Cookie File"
        )
        or text.startswith(
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
            text,
            encoding="utf-8",
            newline="\n",
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
# SAFE FILENAME
# ============================================================

def safe_filename(name: str) -> str:

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
# CONTENT TYPE
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
# HTTP SESSION
# ============================================================

def create_http_session():

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,"
                "image/webp,"
                "*/*;q=0.8"
            ),
            "Connection": "keep-alive",
        }
    )

    return session


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
):
    
    cookie_file = create_cookie_file(platform)

    if platform == "youtube":
        if cookie_file and Path(cookie_file).is_file():
            logger.info(
                "YouTube cookie file exists; size=%s bytes",
                Path(cookie_file).stat().st_size,
            )
        else:
            logger.error(
                "YouTube cookies are missing or could not be created."
            )

    options = {
        # Keep all your existing options here.
    }

            / "Downloaded_Media_%(autonumber)03d.%(ext)s"
        ),

        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlistend": MAX_PLAYLIST_ITEMS,
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "continuedl": True,
        "overwrites": True,
        "max_filesize": MAX_FILE_SIZE,

        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },

        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "restrictfilenames": True,
        "postprocessors": [],
    }

    # Use cookies configured in Railway Variables.
    if cookie_file:
        options["cookiefile"] = cookie_file
        logger.info("Cookies loaded for platform: %s", platform)
    elif platform == "youtube":
        logger.warning(
            "YouTube cookies are missing. "
            "Check the YOUTUBE_COOKIES_B64 Railway variable."
        )

    # Optional YouTube player client configuration.
    
    if platform == "youtube":
        options["extractor_args"] = {
            "youtube": {
                "player_client": ["default", "web_embedded"],
            }
        }

    return options



# ============================================================
# YT-DLP DOWNLOAD
# ============================================================

def run_ytdlp(
    url: str,
    platform: str,
    output_dir: Path,
):

    options = build_ydl_options(
        url=url,
        platform=platform,
        output_dir=output_dir,
    )

    logger.info(
        "yt-dlp downloading: %s",
        url,
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
            "No media file was created."
        )

    return files


# ============================================================
# FIND MEDIA
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

        if path.name.endswith(
            (
                ".part",
                ".ytdl",
                ".temp",
            )
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
        key=lambda p: p.stat().st_mtime,
    )


# ============================================================
# FFMPEG RUNNER
# ============================================================

def run_ffmpeg(
    command,
    timeout=FFMPEG_TIMEOUT,
):

    logger.info(
        "FFmpeg: %s",
        " ".join(
            map(str, command)
        ),
    )

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

    except subprocess.TimeoutExpired:

        logger.error(
            "FFmpeg timed out."
        )

        raise RuntimeError(
            "Video conversion took too long."
        )

    if result.returncode != 0:

        logger.error(
            "FFmpeg failed:\n%s",
            result.stderr[-3000:],
        )

        raise RuntimeError(
            "FFmpeg failed while preparing the video."
        )

    return result


# ============================================================
# VIDEO PROBE
# ============================================================

def ffprobe_value(
    path: Path,
    selector: str,
    entry: str,
):

    try:

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                selector,
                "-show_entries",
                f"stream={entry}",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            return None

        value = result.stdout.strip()

        return value or None

    except Exception:
        return None


def get_video_codec(
    path: Path,
):

    return ffprobe_value(
        path,
        "v:0",
        "codec_name",
    )


def get_audio_codec(
    path: Path,
):

    return ffprobe_value(
        path,
        "a:0",
        "codec_name",
    )


def get_video_height(
    path: Path,
):

    value = ffprobe_value(
        path,
        "v:0",
        "height",
    )

    try:
        return int(value)
    except Exception:
        return 0


def has_video(
    path: Path,
):

    return (
        get_video_codec(path)
        is not None
    )


# ============================================================
# VIDEO COMPATIBILITY
# ============================================================

def is_iphone_compatible_video(
    path: Path,
):

    if path.suffix.lower() != ".mp4":
        return False

    video_codec = get_video_codec(
        path
    )

    audio_codec = get_audio_codec(
        path
    )

    height = get_video_height(
        path
    )

    logger.info(
        "Video: codec=%s audio=%s height=%s",
        video_codec,
        audio_codec,
        height,
    )

    if video_codec != "h264":
        return False

    if audio_codec not in {
        "aac",
        None,
    }:
        return False

    if height > 1080:
        return False

    return True


# ============================================================
# FASTSTART REMUX
# ============================================================

def faststart_mp4(
    input_path: Path,
    output_path: Path,
):

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    run_ffmpeg(
        command,
        timeout=30,
    )

    return output_path


# ============================================================
# VIDEO CONVERSION
# ============================================================

def convert_video(
    input_path: Path,
    output_path: Path,
    height=720,
    crf=28,
    audio_bitrate="128k",
):

    source_height = get_video_height(
        input_path
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),

        "-map",
        "0:v:0",
    ]

    # Do not upscale small videos.
    if (
        source_height
        and source_height > height
    ):

        command.extend(
            [
                "-vf",
                f"scale=-2:{height}",
            ]
        )

    command.extend(
        [
            "-c:v",
            "libx264",

            "-preset",
            "ultrafast",

            "-crf",
            str(crf),

            "-pix_fmt",
            "yuv420p",

            "-c:a",
            "aac",

            "-b:a",
            audio_bitrate,

            "-movflags",
            "+faststart",

            str(output_path),
        ]
    )

    run_ffmpeg(
        command
    )

    return output_path


# ============================================================
# PROCESS VIDEO
# ============================================================

def process_video_file(
    path: Path,
):

    size = path.stat().st_size

    logger.info(
        "Processing video: %.2f MB",
        size / 1024 / 1024,
    )

    # --------------------------------------------------------
    # Already small + compatible
    # --------------------------------------------------------

    if (
        size <= MAX_FILE_SIZE
        and is_iphone_compatible_video(path)
    ):

        logger.info(
            "Video already compatible. No conversion required."
        )

        return path

    # --------------------------------------------------------
    # H264/AAC but not MP4 or faststart
    # --------------------------------------------------------

    if size <= MAX_FILE_SIZE:

        video_codec = get_video_codec(
            path
        )

        audio_codec = get_audio_codec(
            path
        )

        if (
            video_codec == "h264"
            and audio_codec in {
                "aac",
                None,
            }
        ):

            output = (
                path.parent
                / "Downloaded_Media.mp4"
            )

            try:

                faststart_mp4(
                    path,
                    output,
                )

                if (
                    output.exists()
                    and output.stat().st_size
                    <= MAX_FILE_SIZE
                ):

                    logger.info(
                        "Fast MP4 remux successful."
                    )

                    return output

            except Exception as error:

                logger.warning(
                    "Fast remux failed: %s",
                    error,
                )

    # --------------------------------------------------------
    # Full conversion
    # --------------------------------------------------------

    logger.info(
        "Video requires conversion."
    )

    output = (
        path.parent
        / "Downloaded_Media.mp4"
    )

    try:

        convert_video(
            input_path=path,
            output_path=output,
            height=720,
            crf=28,
            audio_bitrate="128k",
        )

        if (
            output.exists()
            and output.stat().st_size
            <= MAX_FILE_SIZE
        ):

            return output

    except Exception as error:

        logger.warning(
            "720p conversion failed: %s",
            error,
        )

    # --------------------------------------------------------
    # 480p fallback
    # --------------------------------------------------------

    output_480 = (
        path.parent
        / "Downloaded_Media_480p.mp4"
    )

    try:

        convert_video(
            input_path=path,
            output_path=output_480,
            height=480,
            crf=31,
            audio_bitrate="96k",
        )

        if (
            output_480.exists()
            and output_480.stat().st_size
            <= MAX_FILE_SIZE
        ):

            return output_480

    except Exception as error:

        logger.warning(
            "480p conversion failed: %s",
            error,
        )

    raise RuntimeError(
        "Video could not be prepared within the 45 MB Telegram limit."
    )


# ============================================================
# LIGHTNING GIF
# ============================================================

def create_lightning_gif():

    if LIGHTNING_GIF.exists():
        return

    width = 500
    height = 220

    frames = []

    try:

        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans-Bold.ttf",
            32,
        )

    except Exception:

        font = ImageFont.load_default()

    for frame_number in range(12):

        image = Image.new(
            "RGB",
            (width, height),
            (15, 15, 25),
        )

        draw = ImageDraw.Draw(
            image
        )

        # Lightning
        lightning = [
            (235, 20),
            (185, 105),
            (225, 105),
            (175, 200),
            (315, 90),
            (270, 90),
            (320, 20),
        ]

        offset = (
            frame_number % 3
        )

        points = [
            (
                x + offset,
                y,
            )
            for x, y in lightning
        ]

        draw.polygon(
            points,
            fill="white",
        )

        text = "PROCESSING..."

        bbox = draw.textbbox(
            (0, 0),
            text,
            font=font,
        )

        text_width = (
            bbox[2] - bbox[0]
        )

        draw.text(
            (
                (width - text_width) / 2,
                185,
            ),
            text,
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
        duration=120,
        loop=0,
    )


# ============================================================
# SEND FILE
# ============================================================

async def send_file(
    update: Update,
    path: Path,
):

    if not path.exists():
        raise RuntimeError(
            "Downloaded file no longer exists."
        )

    size = path.stat().st_size

    if size > MAX_FILE_SIZE:

        raise RuntimeError(
            "File is larger than the 45 MB upload limit."
        )

    suffix = path.suffix.lower()

    # --------------------------------------------------------
    # MP4 / video
    # --------------------------------------------------------

    if suffix == ".mp4":

        await update.message.chat.send_action(
            ChatAction.UPLOAD_VIDEO
        )

        with open(
            path,
            "rb",
        ) as video:

            await update.message.reply_video(
                video=video,
                caption=None,
                supports_streaming=True,
            )

        return

    # --------------------------------------------------------
    # Other videos
    # --------------------------------------------------------

    if suffix in VIDEO_EXTENSIONS:

        await update.message.chat.send_action(
            ChatAction.UPLOAD_VIDEO
        )

        with open(
            path,
            "rb",
        ) as video:

            await update.message.reply_video(
                video=video,
                caption=None,
                supports_streaming=True,
            )

        return

    # --------------------------------------------------------
    # Audio
    # --------------------------------------------------------

    if suffix in AUDIO_EXTENSIONS:

        await update.message.chat.send_action(
            ChatAction.UPLOAD_AUDIO
        )

        with open(
            path,
            "rb",
        ) as audio:

            await update.message.reply_audio(
                audio=audio,
                caption=None,
            )

        return

    # --------------------------------------------------------
    # Images
    # --------------------------------------------------------

    if suffix in IMAGE_EXTENSIONS:

        await update.message.chat.send_action(
            ChatAction.UPLOAD_PHOTO
        )

        with open(
            path,
            "rb",
        ) as image:

            await update.message.reply_photo(
                photo=image
            )

        return

    # --------------------------------------------------------
    # GIF
    # --------------------------------------------------------

    if suffix == ".gif":

        await update.message.chat.send_action(
            ChatAction.UPLOAD_DOCUMENT
        )

        with open(
            path,
            "rb",
        ) as gif:

            await update.message.reply_animation(
                animation=gif
            )

        return

    # --------------------------------------------------------
    # Everything else
    # --------------------------------------------------------

    await update.message.chat.send_action(
        ChatAction.UPLOAD_DOCUMENT
    )

    with open(
        path,
        "rb",
    ) as document:

        await update.message.reply_document(
            document=document,
            caption=None,
        )


# ============================================================
# DOWNLOAD JOB
# ============================================================

def download_job(
    url: str,
    platform: str,
    job_dir: Path,
):

    logger.info(
        "Starting download: %s",
        url,
    )

    # --------------------------------------------------------
    # Direct media URL
    # --------------------------------------------------------

    if is_direct_media_url(url):

        file_path = direct_download(
            url,
            job_dir,
        )

        return [
            file_path
        ]

    # --------------------------------------------------------
    # yt-dlp
    # --------------------------------------------------------

    files = run_ytdlp(
        url=url,
        platform=platform,
        output_dir=job_dir,
    )

    return files


# ============================================================
# START COMMAND
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "👋 Welcome to Universal Media Downloader!\n\n"
        "📥 Send me a media link.\n\n"
        "⚡ I will download the available media.\n"
        "🎵 Available original audio is preserved whenever possible.\n\n"
        "Use /help for supported platforms."
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

        "Send a media URL and I will download it.\n\n"

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

        "Direct media:\n"
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

        "🎵 Original available audio is preserved whenever possible."
    )


# ============================================================
# PROCESSING MESSAGE UPDATE
# ============================================================

async def update_processing_message(
    message,
    text: str,
):

    if not message:
        return

    try:

        await message.edit_caption(
            caption=text
        )

    except Exception as error:

        logger.debug(
            "Could not update processing message: %s",
            error,
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
    ).strip()

    # ========================================================
    # IMPORTANT:
    # DO NOTHING IF THERE IS NO URL.
    #
    # Therefore the processing animation appears ONLY when
    # a real URL is sent.
    # ========================================================

    url = extract_url(
        text
    )

    if not url:

        # Do not send processing message.
        # Do not reply to ordinary text.
        return

    url = normalize_url(
        url
    )

    platform = detect_platform(
        url
    )

    logger.info(
        "URL received | platform=%s | %s",
        platform,
        url,
    )

    processing_message = None
    job_dir = None

    try:

        # ====================================================
        # CREATE JOB DIRECTORY
        # ====================================================

        job_dir = (
            DOWNLOAD_DIR
            / next(
                tempfile._get_candidate_names()
            )
        )

        job_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        # ====================================================
        # ONLY NOW SHOW PROCESSING
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
                            "⚡ Processing your link...\n\n"
                            f"Platform: {platform.title()}"
                        ),
                    )
                )

        # ====================================================
        # DOWNLOAD
        # ====================================================

        if processing_message:

            await update_processing_message(
                processing_message,
                (
                    "⚡ Downloading media...\n\n"
                    f"Platform: {platform.title()}"
                ),
            )

        loop = (
            asyncio.get_running_loop()
        )

        files = await loop.run_in_executor(
            None,
            download_job,
            url,
            platform,
            job_dir,
        )

        if not files:

            raise RuntimeError(
                "No media was downloaded."
            )

        # ====================================================
        # SEND FILES
        # ====================================================

        total_files = len(files)

        for index, file_path in enumerate(
            files,
            start=1,
        ):

            if processing_message:

                await update_processing_message(
                    processing_message,
                    (
                        "⚡ Preparing media...\n\n"
                        f"File {index}/{total_files}"
                    ),
                )

            # ----------------------------------------------
            # Video
            # ----------------------------------------------

            if (
                file_path.suffix.lower()
                in VIDEO_EXTENSIONS
            ):

                file_path = await loop.run_in_executor(
                    None,
                    process_video_file,
                    file_path,
                )

            # ----------------------------------------------
            # Check final size
            # ----------------------------------------------

            if (
                not file_path.exists()
            ):

                raise RuntimeError(
                    "Downloaded file disappeared before upload."
                )

            if (
                file_path.stat().st_size
                > MAX_FILE_SIZE
            ):

                raise RuntimeError(
                    "Downloaded media is larger than the 45 MB Telegram limit."
                )

            # ----------------------------------------------
            # Send
            # ----------------------------------------------

            await send_file(
                update,
                file_path,
            )

        # ====================================================
        # REMOVE PROCESSING MESSAGE
        # ====================================================

        if processing_message:

            try:

                await processing_message.delete()

            except Exception:

                pass

    except yt_dlp.utils.DownloadError as error:

        logger.exception(
            "yt-dlp download failed."
        )

        error_text = str(
            error
        )

        if len(error_text) > 1200:
            error_text = (
                error_text[:1200]
                + "..."
            )

        if processing_message:

            await update_processing_message(
                processing_message,
                (
                    "❌ Download failed.\n\n"
                    f"{error_text}"
                ),
            )

        else:

            await update.message.reply_text(
                "❌ Download failed."
            )

    except Exception as error:

        logger.exception(
            "Media processing failed."
        )

        error_text = str(
            error
        )

        if not error_text:
            error_text = (
                "Unknown error."
            )

        if len(error_text) > 1200:
            error_text = (
                error_text[:1200]
                + "..."
            )

        if processing_message:

            await update_processing_message(
                processing_message,
                (
                    "❌ Media processing failed.\n\n"
                    f"{error_text}"
                ),
            )

        else:

            await update.message.reply_text(
                (
                    "❌ Media processing failed.\n\n"
                    f"{error_text}"
                )
            )

    finally:

        # ====================================================
        # CLEAN TEMP FILES
        # ====================================================

        if job_dir:

            try:

                shutil.rmtree(
                    job_dir,
                    ignore_errors=True,
                )

            except Exception:

                pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Telegram error:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "Starting Universal Media Downloader..."
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
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

    # IMPORTANT:
    # Only normal TEXT messages are handled.
    #
    # handle_message() first checks for a URL.
    # If there is no URL, it returns without showing
    # any processing message.
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot is running."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
