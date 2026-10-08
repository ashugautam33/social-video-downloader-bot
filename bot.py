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

# Keep below the practical Telegram bot upload limit.
# 45 MB gives us some safety margin.
MAX_FILE_SIZE = 45 * 1024 * 1024

# Normal output resolution.
MAX_VIDEO_HEIGHT = 720

# Maximum number of playlist entries.
MAX_PLAYLIST_ITEMS = 20

# Temporary directory.
BASE_DIR = (
    Path(tempfile.gettempdir())
    / "universal_media_downloader"
)

DOWNLOAD_DIR = BASE_DIR / "downloads"
COOKIE_DIR = BASE_DIR / "cookies"

LIGHTNING_GIF = (
    BASE_DIR / "lightning.gif"
)

# Create required directories.
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
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "UniversalMediaDownloader"
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
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)


# ============================================================
# PLATFORM DETECTION
# ============================================================

def detect_platform(url: str) -> str:

    try:

        host = (
            urlparse(url)
            .netloc
            .lower()
        )

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

    match = URL_PATTERN.search(
        text
    )

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

        host = (
            parsed.netloc
            .lower()
        )

        # Handle Facebook login redirect URLs.
        if (
            "facebook.com" in host
            and parsed.path.startswith("/login")
        ):

            query = parse_qs(
                parsed.query
            )

            next_values = (
                query.get("next")
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
# DIRECT MEDIA CHECK
# ============================================================

def is_direct_media_url(
    url: str,
) -> bool:

    try:

        path = (
            urlparse(url)
            .path
            .lower()
        )

        return any(
            path.endswith(ext)
            for ext in ALL_MEDIA_EXTENSIONS
        )

    except Exception:

        return False


# ============================================================
# COOKIE FILE CREATION
# ============================================================

def create_cookie_file(
    platform: str,
):

    environment_name = (
        COOKIE_ENV.get(platform)
    )

    if not environment_name:
        return None

    encoded = os.getenv(
        environment_name
    )

    if not encoded:
        return None

    try:

        decoded = base64.b64decode(
            encoded.strip(),
            validate=True,
        )

        cookie_text = (
            decoded.decode("utf-8")
        )

    except Exception as error:

        logger.error(
            "Cookie decode failed for %s: %s",
            platform,
            error,
        )

        return None

    # Verify Netscape cookie format.
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

    cookie_file = (
        COOKIE_DIR
        / f"{platform}.txt"
    )

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

        name = (
            "Downloaded_Media"
        )

    return name[:180]


# ============================================================
# DIRECTORY CLEANUP
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
# FIND MEDIA FILES
# ============================================================

def find_media_files(
    directory: Path,
):

    files = []

    if not directory.exists():
        return files

    ignored_extensions = {
        ".part",
        ".ytdl",
        ".temp",
        ".json",
        ".description",
    }

    for path in directory.rglob("*"):

        if not path.is_file():
            continue

        if (
            path.suffix.lower()
            in ignored_extensions
        ):
            continue

        try:

            if path.stat().st_size <= 0:
                continue

        except Exception:

            continue

        files.append(path)

    return sorted(
        files,
        key=lambda item:
            item.stat().st_mtime,
    )


# ============================================================
# FFMPEG RUNNER
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
            "FFmpeg error:\n%s",
            result.stderr[-4000:],
        )

        raise RuntimeError(
            "FFmpeg failed to process the media."
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

        for line in (
            result.stdout.splitlines()
        ):

            if "=" not in line:
                continue

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
                0,
            )
        )

        height = int(
            values.get(
                "height",
                0,
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
# AUDIO INFORMATION
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
# UNIVERSAL IPHONE VIDEO CONVERSION
# ============================================================

def convert_video_for_iphone(
    input_path: Path,
    output_path: Path,
    height: int,
    crf: int,
    audio_bitrate: str,
):

    """
    Create a highly compatible MP4.

    Video:
        H.264 / AVC

    Audio:
        AAC

    Pixel format:
        yuv420p

    Container:
        MP4

    Metadata:
        preserved where possible

    Fast start:
        enabled
    """

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(input_path),

        # ----------------------------------------------
        # VIDEO
        # ----------------------------------------------

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        str(crf),

        # iPhone-compatible pixel format.
        "-pix_fmt",
        "yuv420p",

        # Limit height while preserving aspect ratio.
        "-vf",
        f"scale=-2:{height}",

        # ----------------------------------------------
        # AUDIO
        # ----------------------------------------------

        "-c:a",
        "aac",

        "-b:a",
        audio_bitrate,

        # ----------------------------------------------
        # MP4
        # ----------------------------------------------

        "-movflags",
        "+faststart",

        # Preserve source metadata.
        "-map_metadata",
        "0",

        str(output_path),
    ]

    run_ffmpeg(
        command
    )

    if not output_path.exists():

        raise RuntimeError(
            "FFmpeg did not create the output video."
        )

    return output_path


# ============================================================
# PREPARE VIDEO
# ============================================================

def process_video_file(
    path: Path,
):

    """
    Every video is normalized.

    We deliberately do NOT simply pass through
    the original file because source websites can
    provide codecs that Telegram/iOS may not handle
    consistently.

    First:
        720p
        H.264
        AAC 192k
        CRF 23

    If that is too large:

        480p
        H.264
        AAC 128k
        CRF 30
    """

    first_output = (
        path.parent
        / "Downloaded_Media.mp4"
    )

    # --------------------------------------------------------
    # First conversion
    # --------------------------------------------------------

    try:

        convert_video_for_iphone(
            input_path=path,
            output_path=first_output,
            height=720,
            crf=23,
            audio_bitrate="192k",
        )

        size = (
            first_output.stat().st_size
        )

        logger.info(
            "First converted video size: %.2f MB",
            size / (1024 * 1024),
        )

        if size <= MAX_FILE_SIZE:

            return first_output

    except Exception as error:

        logger.warning(
            "First conversion failed: %s",
            error,
        )

    # --------------------------------------------------------
    # Second conversion
    # --------------------------------------------------------

    second_output = (
        path.parent
        / "Downloaded_Media_480p.mp4"
    )

    try:

        convert_video_for_iphone(
            input_path=path,
            output_path=second_output,
            height=480,
            crf=30,
            audio_bitrate="128k",
        )

        size = (
            second_output.stat().st_size
        )

        logger.info(
            "Second converted video size: %.2f MB",
            size / (1024 * 1024),
        )

        if size <= MAX_FILE_SIZE:

            return second_output

    except Exception as error:

        logger.warning(
            "Second conversion failed: %s",
            error,
        )

    # --------------------------------------------------------
    # Third conversion
    # --------------------------------------------------------

    third_output = (
        path.parent
        / "Downloaded_Media_low.mp4"
    )

    try:

        convert_video_for_iphone(
            input_path=path,
            output_path=third_output,
            height=360,
            crf=33,
            audio_bitrate="96k",
        )

        size = (
            third_output.stat().st_size
        )

        logger.info(
            "Third converted video size: %.2f MB",
            size / (1024 * 1024),
        )

        if size <= MAX_FILE_SIZE:

            return third_output

    except Exception as error:

        logger.warning(
            "Third conversion failed: %s",
            error,
        )

    raise RuntimeError(
        "The video could not be compressed below "
        "the Telegram upload limit."
    )


# ============================================================
# DIRECT MEDIA DOWNLOAD
# ============================================================

def direct_download(
    url: str,
    output_dir: Path,
):

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        }
    )

    response = session.get(
        url,
        stream=True,
        timeout=60,
        allow_redirects=True,
    )

    response.raise_for_status()

    content_type = (
        response.headers.get(
            "Content-Type",
            "",
        )
        .lower()
    )

    # Determine extension.
    extension = ".bin"

    if "video/mp4" in content_type:

        extension = ".mp4"

    elif "video/webm" in content_type:

        extension = ".webm"

    elif "video/quicktime" in content_type:

        extension = ".mov"

    elif "audio/mpeg" in content_type:

        extension = ".mp3"

    elif "audio/mp4" in content_type:

        extension = ".m4a"

    elif "audio/wav" in content_type:

        extension = ".wav"

    elif "image/jpeg" in content_type:

        extension = ".jpg"

    elif "image/png" in content_type:

        extension = ".png"

    elif "image/webp" in content_type:

        extension = ".webp"

    elif "image/gif" in content_type:

        extension = ".gif"

    output_path = (
        output_dir
        / f"Downloaded_Media{extension}"
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

                raise RuntimeError(
                    "The direct media file is larger "
                    "than the Telegram upload limit."
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

        # Clean internal filenames.
        #
        # No title.
        # No uploader.
        # No video ID.
        #
        "outtmpl": str(
            output_dir
            / "Downloaded_Media_%(autonumber)03d.%(ext)s"
        ),

        # Download best available video
        # and best available audio.
        "format":
            "bestvideo*+bestaudio/best",

        # Initial merge.
        "merge_output_format":
            "mp4",

        # Playlist support.
        "noplaylist": False,

        "playlistend":
            MAX_PLAYLIST_ITEMS,

        # Download reliability.
        "retries": 3,

        "fragment_retries": 3,

        "concurrent_fragment_downloads": 4,

        "socket_timeout": 30,

        # HTTP headers.
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language":
                "en-US,en;q=0.9",
        },

        # Do not generate extra files.
        "writethumbnail": False,

        "writeinfojson": False,

        "writedescription": False,

        "writesubtitles": False,

        "writeautomaticsub": False,

        # Cleaner logging.
        "quiet": True,

        "no_warnings": True,

        # Chrome impersonation when supported.
        "impersonate": "chrome",
    }

    cookie_file = create_cookie_file(
        platform
    )

    if cookie_file:

        options[
            "cookiefile"
        ] = cookie_file

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
        "Starting yt-dlp: %s",
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
            "First yt-dlp attempt failed: %s",
            first_error,
        )

        # Retry without Chrome impersonation.
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
# DOWNLOAD AND PROCESS
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

        # ----------------------------------------------------
        # DIRECT URL
        # ----------------------------------------------------

        if is_direct_media_url(
            url
        ):

            files = direct_download(
                url,
                job_dir,
            )

        # ----------------------------------------------------
        # WEBSITE URL
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # PROCESS FILES
        # ----------------------------------------------------

        for path in files:

            suffix = (
                path.suffix.lower()
            )

            # ================================================
            # VIDEO
            # ================================================

            if suffix in VIDEO_EXTENSIONS:

                logger.info(
                    "Converting video for iPhone: %s",
                    path.name,
                )

                processed = (
                    process_video_file(
                        path
                    )
                )

                processed_files.append(
                    processed
                )

            # ================================================
            # AUDIO
            # ================================================

            elif suffix in AUDIO_EXTENSIONS:

                size = (
                    path.stat().st_size
                )

                if size > MAX_FILE_SIZE:

                    raise RuntimeError(
                        "Audio file exceeds Telegram's limit."
                    )

                processed_files.append(
                    path
                )

            # ================================================
            # IMAGE
            # ================================================

            elif suffix in IMAGE_EXTENSIONS:

                size = (
                    path.stat().st_size
                )

                if size > MAX_FILE_SIZE:

                    raise RuntimeError(
                        "Image exceeds Telegram's limit."
                    )

                processed_files.append(
                    path
                )

            # ================================================
            # GIF
            # ================================================

            elif suffix == ".gif":

                size = (
                    path.stat().st_size
                )

                if size > MAX_FILE_SIZE:

                    raise RuntimeError(
                        "GIF exceeds Telegram's limit."
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
# SEND VIDEO/AUDIO/IMAGE
# ============================================================

async def send_file(
    update: Update,
    path: Path,
):

    suffix = (
        path.suffix.lower()
    )

    size = (
        path.stat().st_size
    )

    if size > MAX_FILE_SIZE:

        raise RuntimeError(
            "File is larger than the Telegram upload limit."
        )

    # ========================================================
    # VIDEO
    # ========================================================

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

                # No filename/title/creator caption.
                caption=None,

                supports_streaming=True,
            )

        return

    # ========================================================
    # OTHER VIDEO
    # ========================================================

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
        ) as audio:

            await update.message.reply_audio(
                audio=audio,

                caption=None,
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
        ) as image:

            await update.message.reply_photo(
                photo=image
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
        ) as gif:

            await update.message.reply_animation(
                animation=gif
            )

        return

    # ========================================================
    # FALLBACK DOCUMENT
    # ========================================================

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
# CREATE LIGHTNING GIF
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

        text = "⚡ PROCESSING..."

        try:

            box = draw.textbbox(
                (0, 0),
                text,
                font=font,
            )

            width = (
                box[2] - box[0]
            )

            height = (
                box[3] - box[1]
            )

            x = (
                500 - width
            ) // 2

            y = (
                180 - height
            ) // 2

            # Small movement.
            y += (
                frame_number % 5
            )

            draw.text(
                (x, y),
                text,
                fill="white",
                font=font,
            )

        except Exception:

            draw.text(
                (175, 80),
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
        "🔗 Send me a media link.\n\n"
        "⚡ I will download and prepare it "
        "for Telegram and iPhone.\n\n"
        "🎵 Available original audio is preserved "
        "whenever possible.\n\n"
        "📱 Videos are converted to a "
        "compatible H.264/AAC MP4 format."
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

        "Supported platforms:\n"
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

        "Direct video, audio and image URLs "
        "are also supported.\n\n"

        "📱 Video output:\n"
        "H.264 + AAC + MP4 + faststart"
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
            "❌ Please send a valid media link."
        )

        return

    url = normalize_url(
        url
    )

    platform = detect_platform(
        url
    )

    logger.info(
        "Received URL | platform=%s | %s",
        platform,
        url,
    )

    processing_message = None
    job_dir = None

    try:

        # ====================================================
        # PROCESSING ANIMATION
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
                            "Downloading and preparing your media..."
                        ),
                    )
                )

        # ====================================================
        # DOWNLOAD IN EXECUTOR
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
                "No media was downloaded."
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
        # SEND MEDIA
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

                logger.exception(
                    "Failed to send file"
                )

                await update.message.reply_text(
                    "❌ The media was downloaded, "
                    "but Telegram could not send it.\n\n"
                    f"Reason: {error}"
                )

        if sent_count == 0:

            raise RuntimeError(
                "No media could be uploaded to Telegram."
            )

    except Exception as error:

        logger.exception(
            "Download failed"
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
            "• The link has expired\n"
            "• The website blocked the request\n"
            "• The website changed its extractor\n"
            "• The media is larger than Telegram allows"
        )

    finally:

        # Delete all temporary downloaded files.
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
        "============================================"
    )

    logger.info(
        "Universal Media Downloader"
    )

    logger.info(
        "Starting..."
    )

    logger.info(
        "yt-dlp version: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "============================================"
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # URL MESSAGES
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    # --------------------------------------------------------
    # ERROR HANDLER
    # --------------------------------------------------------

    application.add_error_handler(
        telegram_error_handler
    )

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# PROGRAM ENTRY
# ============================================================

if __name__ == "__main__":

    main()
