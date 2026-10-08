import os
import re
import json
import base64
import shutil
import asyncio
import logging
import tempfile
import subprocess

from pathlib import Path
from urllib.parse import (
    urlsplit,
    urlunsplit,
    parse_qs,
    unquote,
)

import yt_dlp
from PIL import Image, ImageDraw

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

# Optional Instagram/Facebook Netscape cookies.txt
INSTAGRAM_COOKIES_B64 = os.getenv(
    "INSTAGRAM_COOKIES_B64"
)

# Telegram safety limit
MAX_FILE_SIZE = 45 * 1024 * 1024

# Maximum output video height
MAX_VIDEO_HEIGHT = 720

# Temporary working directory
BASE_DIR = (
    Path(tempfile.gettempdir())
    / "universal_media_downloader"
)

BASE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

LIGHTNING_GIF = (
    BASE_DIR / "lightning.gif"
)


# ============================================================
# FILE EXTENSIONS
# ============================================================

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".avi",
    ".flv",
    ".3gp",
    ".ts",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".flac",
    ".wma",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tiff",
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format=(
        "%(asctime)s - "
        "%(levelname)s - "
        "%(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(
    "UniversalMediaBot"
)


# ============================================================
# LIGHTNING GIF
# ============================================================

def create_lightning_gif():

    if LIGHTNING_GIF.exists():
        return

    frames = []

    for i in range(12):

        image = Image.new(
            "RGBA",
            (400, 400),
            (0, 0, 0, 0),
        )

        draw = ImageDraw.Draw(
            image
        )

        pulse = (
            1.0
            + 0.08 * (i % 6) / 5
        )

        cx = 200
        cy = 200

        points = [
            (
                cx + int(35 * pulse),
                cy - int(145 * pulse),
            ),
            (
                cx - int(70 * pulse),
                cy + int(5 * pulse),
            ),
            (
                cx - int(5 * pulse),
                cy + int(5 * pulse),
            ),
            (
                cx - int(45 * pulse),
                cy + int(145 * pulse),
            ),
            (
                cx + int(85 * pulse),
                cy - int(20 * pulse),
            ),
            (
                cx + int(20 * pulse),
                cy - int(20 * pulse),
            ),
        ]

        # Glow
        for width, alpha in (
            (30, 25),
            (20, 45),
            (12, 75),
        ):

            draw.line(
                points + [points[0]],
                fill=(
                    255,
                    220,
                    0,
                    alpha,
                ),
                width=width,
                joint="curve",
            )

        # Lightning body
        draw.polygon(
            points,
            fill=(
                255,
                215,
                0,
                255,
            ),
        )

        # White outline
        draw.line(
            points + [points[0]],
            fill=(
                255,
                255,
                255,
                255,
            ),
            width=5,
            joint="curve",
        )

        frames.append(image)

    frames[0].save(
        LIGHTNING_GIF,
        save_all=True,
        append_images=frames[1:],
        duration=90,
        loop=0,
        disposal=2,
    )


# ============================================================
# URL EXTRACTION
# ============================================================

def extract_url(text):

    if not text:
        return None

    match = re.search(
        r"https?://[^\s]+",
        text,
    )

    if not match:
        return None

    url = match.group(0).rstrip(
        ".,!?)]}"
    )

    return normalize_social_url(
        url
    )


# ============================================================
# URL NORMALIZATION
# ============================================================

def normalize_social_url(url):

    if not url:
        return url

    try:

        parts = urlsplit(url)

        host = (
            parts.netloc
            .lower()
        )

        path = parts.path.lower()

        # ====================================================
        # FACEBOOK LOGIN REDIRECT
        # ====================================================

        if (
            "facebook.com" in host
            and path.startswith("/login")
        ):

            query = parse_qs(
                parts.query
            )

            next_values = query.get(
                "next"
            )

            if next_values:

                next_url = next_values[0]

                next_url = unquote(
                    next_url
                )

                logger.info(
                    "Facebook login redirect detected."
                )

                logger.info(
                    "Recovered Facebook URL: %s",
                    next_url,
                )

                return normalize_social_url(
                    next_url
                )

        # ====================================================
        # FACEBOOK TRACKING PARAMETERS
        # ====================================================

        if "facebook.com" in host:

            return urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    parts.query,
                    "",
                )
            )

        # ====================================================
        # OTHER SOCIAL SITES
        # ====================================================

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                "",
                "",
            )
        )

    except Exception as e:

        logger.warning(
            "URL normalization error: %s",
            e,
        )

        return url


# ============================================================
# URL TYPES
# ============================================================

def is_instagram(url):

    return (
        "instagram.com"
        in url.lower()
    )


def is_facebook(url):

    value = url.lower()

    return (
        "facebook.com" in value
        or "fb.watch" in value
    )


def is_direct_media_url(url):

    path = urlsplit(url).path.lower()

    extensions = (
        VIDEO_EXTENSIONS
        | AUDIO_EXTENSIONS
        | IMAGE_EXTENSIONS
    )

    return any(
        path.endswith(ext)
        for ext in extensions
    )


# ============================================================
# COOKIE FILE
# ============================================================

def create_cookie_file():

    if not INSTAGRAM_COOKIES_B64:

        logger.info(
            "No Instagram/Facebook cookies configured."
        )

        return None

    cookie_file = (
        BASE_DIR
        / "social_cookies.txt"
    )

    try:

        value = (
            INSTAGRAM_COOKIES_B64
            .strip()
        )

        # ====================================================
        # TRY BASE64
        # ====================================================

        try:

            decoded = base64.b64decode(
                value,
                validate=True,
            )

        except Exception:

            decoded = None

        if decoded:

            # Remove UTF-8 BOM
            if decoded.startswith(
                b"\xef\xbb\xbf"
            ):

                decoded = decoded[3:]

            try:

                text = decoded.decode(
                    "utf-8"
                )

            except UnicodeDecodeError:

                logger.error(
                    "Cookie data is not UTF-8 text."
                )

                return None

        else:

            # =================================================
            # Plain cookies.txt
            # =================================================

            text = value

        # ====================================================
        # VALIDATE NETSCAPE FORMAT
        # ====================================================

        if (
            "# Netscape HTTP Cookie File"
            not in text
            and "\t" not in text
        ):

            logger.error(
                "Invalid Netscape cookies.txt."
            )

            return None

        cookie_file.write_text(
            text,
            encoding="utf-8",
        )

        logger.info(
            "Social cookies loaded successfully."
        )

        return str(cookie_file)

    except Exception as e:

        logger.exception(
            "Cookie processing failed."
        )

        return None


# ============================================================
# FFMPEG CHECK
# ============================================================

def ffmpeg_available():

    try:

        result = subprocess.run(
            [
                "ffmpeg",
                "-version",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

        return (
            result.returncode == 0
        )

    except Exception:

        return False


# ============================================================
# FFPROBE
# ============================================================

def probe(file_path):

    try:

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(file_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:

            return None

        return json.loads(
            result.stdout
        )

    except Exception:

        return None


# ============================================================
# STREAM DETECTION
# ============================================================

def get_streams(file_path):

    data = probe(
        file_path
    )

    if not data:

        return None, None

    streams = data.get(
        "streams",
        [],
    )

    video = next(
        (
            stream
            for stream in streams
            if stream.get(
                "codec_type"
            ) == "video"
        ),
        None,
    )

    audio = next(
        (
            stream
            for stream in streams
            if stream.get(
                "codec_type"
            ) == "audio"
        ),
        None,
    )

    return video, audio


# ============================================================
# MEDIA TYPE
# ============================================================

def detect_media_type(file_path):

    extension = (
        Path(file_path)
        .suffix
        .lower()
    )

    if extension in VIDEO_EXTENSIONS:

        return "video"

    if extension in AUDIO_EXTENSIONS:

        return "audio"

    if extension in IMAGE_EXTENSIONS:

        return "image"

    video, audio = get_streams(
        file_path
    )

    if video:

        return "video"

    if audio:

        return "audio"

    return "document"


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    output_template,
    url,
):

    options = {

        # ----------------------------------------------------
        # Best video + best audio
        # ----------------------------------------------------

        "format": (
            "bv*+ba/"
            "b"
        ),

        "outtmpl": output_template,

        # Instagram/Facebook carousels
        # can contain multiple entries.
        "noplaylist": False,

        "quiet": False,

        "no_warnings": False,

        "ignoreerrors": False,

        "retries": 3,

        "fragment_retries": 3,

        "file_access_retries": 3,

        "socket_timeout": 30,

        "concurrent_fragment_downloads": 4,

        # Merge video + audio into MP4
        "merge_output_format": "mp4",

        # Don't download metadata files
        "writethumbnail": False,

        "writeinfojson": False,

        "writesubtitles": False,

        "writeautomaticsub": False,

        # Browser-like headers
        "http_headers": {

            "User-Agent":
                (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/140.0.0.0 "
                    "Safari/537.36"
                ),

            "Accept":
                "*/*",

            "Accept-Language":
                "en-US,en;q=0.9",

        },
    }

    # ========================================================
    # COOKIES
    # ========================================================

    if (
        is_instagram(url)
        or is_facebook(url)
    ):

        cookie_file = (
            create_cookie_file()
        )

        if cookie_file:

            options["cookiefile"] = (
                cookie_file
            )

    return options


# ============================================================
# FIND DOWNLOADED FILES
# ============================================================

def find_media_files(folder):

    files = []

    for file in folder.iterdir():

        if not file.is_file():
            continue

        suffix = (
            file.suffix.lower()
        )

        if (
            suffix in VIDEO_EXTENSIONS
            or suffix in AUDIO_EXTENSIONS
            or suffix in IMAGE_EXTENSIONS
        ):

            files.append(file)

    # --------------------------------------------------------
    # Remove tiny / empty files
    # --------------------------------------------------------

    files = [
        file
        for file in files
        if file.stat().st_size > 100
    ]

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    files.sort(
        key=lambda x: x.name
    )

    return files


# ============================================================
# DOWNLOAD MEDIA
# ============================================================

def download_media(url):

    # Normalize URL first
    url = normalize_social_url(
        url
    )

    logger.info(
        "Final URL: %s",
        url,
    )

    folder = Path(
        tempfile.mkdtemp(
            prefix="media_",
            dir=BASE_DIR,
        )
    )

    output_template = str(
        folder
        / "%(playlist_index&{}|)s%(id)s.%(ext)s"
    )

    # --------------------------------------------------------
    # Some extractors don't provide playlist_index correctly.
    # Use a simpler fallback template.
    # --------------------------------------------------------

    output_template = str(
        folder
        / "%(autonumber)03d_%(id)s.%(ext)s"
    )

    attempts = [
        url
    ]

    # --------------------------------------------------------
    # Clean URL
    # --------------------------------------------------------

    cleaned = normalize_social_url(
        url
    )

    if cleaned != url:

        attempts.append(
            cleaned
        )

    # Remove duplicates
    attempts = list(
        dict.fromkeys(
            attempts
        )
    )

    last_error = None

    for attempt_number, current_url in enumerate(
        attempts,
        start=1,
    ):

        logger.info(
            "Download attempt %s",
            attempt_number,
        )

        logger.info(
            "URL: %s",
            current_url,
        )

        try:

            options = (
                build_ydl_options(
                    output_template,
                    current_url,
                )
            )

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                info = ydl.extract_info(
                    current_url,
                    download=True,
                )

                if info:

                    logger.info(
                        "Extractor: %s",
                        info.get(
                            "extractor"
                        ),
                    )

                    logger.info(
                        "Title: %s",
                        info.get(
                            "title"
                        ),
                    )

                    logger.info(
                        "Media type: %s",
                        info.get(
                            "_type"
                        ),
                    )

        except Exception as e:

            last_error = str(e)

            logger.error(
                "yt-dlp error: %s",
                last_error,
            )

            continue

        files = find_media_files(
            folder
        )

        if files:

            logger.info(
                "Found %d media files.",
                len(files),
            )

            return (
                files,
                folder,
                None,
            )

    return (
        None,
        folder,
        last_error
        or "No media was downloaded.",
    )


# ============================================================
# VIDEO CONVERSION
# ============================================================

def convert_video(
    input_file
):

    input_file = Path(
        input_file
    )

    output_file = (
        input_file.parent
        / f"{input_file.stem}_telegram.mp4"
    )

    video, audio = get_streams(
        input_file
    )

    if not video:

        return (
            None,
            "No video stream found.",
        )

    width = int(
        video.get("width")
        or 0
    )

    height = int(
        video.get("height")
        or 0
    )

    codec = video.get(
        "codec_name",
        "",
    )

    pixel_format = video.get(
        "pix_fmt",
        "",
    )

    logger.info(
        "Video: %sx%s codec=%s pix_fmt=%s",
        width,
        height,
        codec,
        pixel_format,
    )

    logger.info(
        "Audio: %s",
        (
            audio.get("codec_name")
            if audio
            else "NONE"
        ),
    )

    # --------------------------------------------------------
    # Keep aspect ratio, maximum 720p
    # --------------------------------------------------------

    video_filter = (
        "scale="
        "w='min(720,iw)':"
        "h='min(720,ih)':"
        "force_original_aspect_ratio=decrease,"
        "pad="
        "ceil(iw/2)*2:"
        "ceil(ih/2)*2:"
        "(ow-iw)/2:"
        "(oh-ih)/2"
    )

    command = [

        "ffmpeg",

        "-y",

        "-i",
        str(input_file),

        # Video
        "-map",
        "0:v:0",

        # Audio is optional
        "-map",
        "0:a:0?",

        "-vf",
        video_filter,

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "28",

        "-pix_fmt",
        "yuv420p",

        "-r",
        "30",

        # Audio
        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-ar",
        "44100",

        "-ac",
        "2",

        # Streaming
        "-movflags",
        "+faststart",

        "-avoid_negative_ts",
        "make_zero",

        str(output_file),
    ]

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
        )

    except subprocess.TimeoutExpired:

        return (
            None,
            "FFmpeg conversion timed out.",
        )

    except Exception as e:

        return (
            None,
            str(e),
        )

    if result.returncode != 0:

        logger.error(
            "FFmpeg error: %s",
            result.stderr[-4000:],
        )

        return (
            None,
            result.stderr[-4000:],
        )

    if not output_file.exists():

        return (
            None,
            "FFmpeg output was not created.",
        )

    output_video, output_audio = (
        get_streams(
            output_file
        )
    )

    if not output_video:

        return (
            None,
            "Converted file contains no video.",
        )

    # If source had audio, make sure output has audio
    if audio and not output_audio:

        return (
            None,
            "Audio was lost during conversion.",
        )

    return (
        output_file,
        None,
    )


# ============================================================
# SEND IMAGE
# ============================================================

async def send_image(
    update,
    file,
):

    size = file.stat().st_size

    # Telegram photo limit safety
    if size <= 10 * 1024 * 1024:

        with open(
            file,
            "rb",
        ) as image:

            await update.message.reply_photo(
                photo=image
            )

    else:

        with open(
            file,
            "rb",
        ) as image:

            await update.message.reply_document(
                document=image,
                caption="🖼️ Image",
            )


# ============================================================
# SEND AUDIO
# ============================================================

async def send_audio(
    update,
    file,
):

    if (
        file.stat().st_size
        > MAX_FILE_SIZE
    ):

        raise RuntimeError(
            "Audio file is too large."
        )

    with open(
        file,
        "rb",
    ) as audio:

        await update.message.reply_audio(
            audio=audio
        )


# ============================================================
# SEND DOCUMENT
# ============================================================

async def send_document(
    update,
    file,
):

    if (
        file.stat().st_size
        > MAX_FILE_SIZE
    ):

        raise RuntimeError(
            "File is too large for Telegram."
        )

    with open(
        file,
        "rb",
    ) as document:

        await update.message.reply_document(
            document=document
        )


# ============================================================
# SEND VIDEO
# ============================================================

async def send_video(
    update,
    file,
):

    file = Path(
        file
    )

    information = probe(
        file
    )

    if not information:

        return (
            False,
            "Could not inspect video.",
        )

    streams = information.get(
        "streams",
        [],
    )

    video_stream = next(
        (
            stream
            for stream in streams
            if stream.get(
                "codec_type"
            ) == "video"
        ),
        None,
    )

    audio_stream = next(
        (
            stream
            for stream in streams
            if stream.get(
                "codec_type"
            ) == "audio"
        ),
        None,
    )

    if not video_stream:

        return (
            False,
            "No video stream found.",
        )

    codec = video_stream.get(
        "codec_name"
    )

    pixel_format = video_stream.get(
        "pix_fmt"
    )

    height = int(
        video_stream.get(
            "height"
        )
        or 0
    )

    # --------------------------------------------------------
    # Determine whether conversion is necessary
    # --------------------------------------------------------

    needs_conversion = (
        codec != "h264"
        or pixel_format != "yuv420p"
        or height > MAX_VIDEO_HEIGHT
        or file.stat().st_size
        > MAX_FILE_SIZE
    )

    final_file = file

    if needs_conversion:

        logger.info(
            "Video requires conversion."
        )

        converted, error = (
            await asyncio.to_thread(
                convert_video,
                file,
            )
        )

        if not converted:

            return (
                False,
                error,
            )

        final_file = converted

    # --------------------------------------------------------
    # Final size
    # --------------------------------------------------------

    if (
        final_file.stat().st_size
        > MAX_FILE_SIZE
    ):

        return (
            False,
            "Final video is larger than "
            "the Telegram upload limit.",
        )

    # --------------------------------------------------------
    # Verify audio
    # --------------------------------------------------------

    final_video, final_audio = (
        get_streams(
            final_file
        )
    )

    if audio_stream and not final_audio:

        return (
            False,
            "The final video has no audio.",
        )

    # --------------------------------------------------------
    # Send
    # --------------------------------------------------------

    with open(
        final_file,
        "rb",
    ) as video:

        await update.message.reply_video(
            video=video,
            supports_streaming=True,
            caption="🎬 Done",
        )

    return (
        True,
        None,
    )


# ============================================================
# SEND MEDIA
# ============================================================

async def send_media_file(
    update,
    file,
):

    media_type = (
        detect_media_type(
            file
        )
    )

    logger.info(
        "Sending %s: %s",
        media_type,
        file.name,
    )

    if media_type == "video":

        return await send_video(
            update,
            file,
        )

    if media_type == "audio":

        await send_audio(
            update,
            file,
        )

        return (
            True,
            None,
        )

    if media_type == "image":

        await send_image(
            update,
            file,
        )

        return (
            True,
            None,
        )

    await send_document(
        update,
        file,
    )

    return (
        True,
        None,
    )


# ============================================================
# STATUS MESSAGE
# ============================================================

async def send_status(
    update
):

    try:

        with open(
            LIGHTNING_GIF,
            "rb",
        ) as animation:

            return await update.message.reply_animation(
                animation=animation,
                caption=(
                    "Thanks for providing the link!"
                ),
            )

    except Exception as e:

        logger.error(
            "Lightning animation error: %s",
            e,
        )

        return await update.message.reply_text(
            "Thanks for providing the link!\n\n"
            "⚡ Processing..."
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

        "🔗 Send me any supported media link.\n\n"

        "I can download:\n"
        "🎬 Videos\n"
        "🎵 Audio\n"
        "🖼️ Photos\n"
        "📚 Carousels\n"
        "📱 Reels\n"
        "▶️ YouTube Shorts\n"
        "📹 Facebook videos\n"
        "🎵 TikTok media\n\n"

        "The bot automatically detects "
        "the media type."
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
        or update.message.caption
        or ""
    )

    url = extract_url(
        text
    )

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid HTTP/HTTPS media URL."
        )

        return

    logger.info(
        "Received URL: %s",
        url,
    )

    # ========================================================
    # STATUS
    # ========================================================

    status = await send_status(
        update
    )

    folder = None

    try:

        # ====================================================
        # DOWNLOAD
        # ====================================================

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        files, folder, error = (
            await asyncio.to_thread(
                download_media,
                url,
            )
        )

        # ====================================================
        # DOWNLOAD ERROR
        # ====================================================

        if error:

            try:
                await status.delete()
            except Exception:
                pass

            error_lower = (
                error.lower()
            )

            # -----------------------------------------------
            # Facebook authentication
            # -----------------------------------------------

            if is_facebook(url) and (
                "login"
                in error_lower
                or "unsupported url"
                in error_lower
                or "authentication"
                in error_lower
                or "cookies"
                in error_lower
                or "private"
                in error_lower
                or "unable to download"
                in error_lower
            ):

                message = (
                    "❌ Facebook could not provide "
                    "the media.\n\n"

                    "Possible reasons:\n"
                    "• Facebook requires login\n"
                    "• The post is private/restricted\n"
                    "• Facebook redirected the request "
                    "to login\n"
                    "• Facebook cookies are missing/expired\n"
                    "• The video is unavailable\n\n"

                    "If this post is visible only after "
                    "logging into Facebook, configure "
                    "valid Facebook cookies in "
                    "INSTAGRAM_COOKIES_B64."
                )

            # -----------------------------------------------
            # Instagram authentication
            # -----------------------------------------------

            elif is_instagram(url) and (
                "empty media"
                in error_lower
                or "login"
                in error_lower
                or "cookies"
                in error_lower
                or "authentication"
                in error_lower
            ):

                message = (
                    "❌ Instagram could not provide "
                    "the media.\n\n"

                    "Possible reasons:\n"
                    "• Login required\n"
                    "• Cookies expired\n"
                    "• Private/restricted post\n"
                    "• Instagram blocked the request\n\n"

                    "Check your Instagram cookies."
                )

            else:

                message = (
                    "❌ Download failed.\n\n"
                    f"{error[-3000:]}"
                )

            await update.message.reply_text(
                message
            )

            return

        # ====================================================
        # DELETE STATUS
        # ====================================================

        try:

            await status.delete()

        except Exception:

            pass

        # ====================================================
        # SEND FILES
        # ====================================================

        sent = 0

        total = len(
            files
        )

        for index, file in enumerate(
            files,
            start=1,
        ):

            if not file.exists():

                continue

            logger.info(
                "Processing media %s/%s: %s",
                index,
                total,
                file.name,
            )

            try:

                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action=ChatAction.UPLOAD_VIDEO,
                )

                ok, send_error = (
                    await send_media_file(
                        update,
                        file,
                    )
                )

                if ok:

                    sent += 1

                else:

                    await update.message.reply_text(
                        "⚠️ Could not send media.\n\n"
                        f"{send_error}"
                    )

            except Exception as e:

                logger.exception(
                    "Media send error."
                )

                await update.message.reply_text(
                    "⚠️ Could not send one media file.\n\n"
                    f"{str(e)[:1500]}"
                )

        # ====================================================
        # FINAL RESULT
        # ====================================================

        if sent == 0:

            await update.message.reply_text(
                "❌ No media could be sent."
            )

        elif total > 1:

            await update.message.reply_text(
                f"✅ Done — {sent}/{total} "
                "media files sent."
            )

    except Exception as e:

        logger.exception(
            "Universal downloader error."
        )

        try:

            await status.delete()

        except Exception:

            pass

        await update.message.reply_text(
            "❌ Something went wrong.\n\n"
            f"{str(e)[:3000]}"
        )

    finally:

        # ====================================================
        # CLEAN TEMP FILES
        # ====================================================

        if folder and folder.exists():

            try:

                await asyncio.to_thread(
                    shutil.rmtree,
                    folder,
                    True,
                )

            except Exception:

                pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "Telegram application error",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable "
            "is missing."
        )

    create_lightning_gif()

    logger.info(
        "=========================================="
    )

    logger.info(
        "Universal Media Downloader"
    )

    logger.info(
        "yt-dlp version: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "FFmpeg available: %s",
        ffmpeg_available(),
    )

    logger.info(
        "Cookies configured: %s",
        bool(
            INSTAGRAM_COOKIES_B64
        ),
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

    # URLs sent as normal text
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    # Captions containing URLs
    application.add_handler(
        MessageHandler(
            filters.CaptionRegex(
                r"https?://"
            ),
            handle_message,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot started."
    )

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":

    main()
