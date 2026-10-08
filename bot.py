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
from urllib.parse import urlsplit, urlunsplit

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
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
INSTAGRAM_COOKIES_B64 = os.getenv("INSTAGRAM_COOKIES_B64")

# Telegram upload safety margin
MAX_FILE_SIZE = 45 * 1024 * 1024

# Video conversion
MAX_VIDEO_HEIGHT = 720

BASE_DIR = Path(
    tempfile.gettempdir()
) / "universal_media_bot"

BASE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

LIGHTNING_GIF = BASE_DIR / "lightning.gif"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# SUPPORTED MEDIA EXTENSIONS
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
# CREATE ANIMATED LIGHTNING
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

        draw = ImageDraw.Draw(image)

        pulse = 1.0 + (
            0.08 * (i % 6) / 5
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
                fill=(255, 220, 0, alpha),
                width=width,
                joint="curve",
            )

        # Lightning
        draw.polygon(
            points,
            fill=(255, 215, 0, 255),
        )

        # White edge
        draw.line(
            points + [points[0]],
            fill=(255, 255, 255, 255),
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
# URL HELPERS
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

    return match.group(0).rstrip(
        ".,!?)]}"
    )


def clean_url(url):

    try:

        parts = urlsplit(url)

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                "",
                "",
            )
        )

    except Exception:

        return url


def is_instagram(url):

    return "instagram.com" in url.lower()


# ============================================================
# COOKIES
# ============================================================

def create_cookie_file():

    if not INSTAGRAM_COOKIES_B64:
        return None

    cookie_file = (
        BASE_DIR /
        "cookies.txt"
    )

    try:

        value = (
            INSTAGRAM_COOKIES_B64
            .strip()
        )

        # ----------------------------------------------------
        # Decode Base64
        # ----------------------------------------------------

        try:

            decoded = base64.b64decode(
                value,
                validate=True,
            )

        except Exception:

            decoded = None

        if decoded:

            # UTF-8 BOM
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
                    "Cookie variable decoded to "
                    "binary data, not text."
                )

                return None

        else:

            # ------------------------------------------------
            # Allow plain cookies.txt
            # ------------------------------------------------

            text = value

        # ----------------------------------------------------
        # Validate Netscape cookie format
        # ----------------------------------------------------

        if (
            "# Netscape HTTP Cookie File"
            not in text
            and "\t" not in text
        ):

            logger.error(
                "Cookie data is not a valid "
                "Netscape cookies.txt file."
            )

            return None

        cookie_file.write_text(
            text,
            encoding="utf-8",
        )

        logger.info(
            "Cookies loaded."
        )

        return str(cookie_file)

    except Exception as e:

        logger.error(
            "Cookie error: %s",
            e,
        )

        return None


# ============================================================
# FFMPEG
# ============================================================

def ffmpeg_available():

    try:

        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

        return result.returncode == 0

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


def get_streams(file_path):

    data = probe(file_path)

    if not data:

        return None, None

    streams = data.get(
        "streams",
        [],
    )

    video = next(
        (
            x for x in streams
            if x.get("codec_type") == "video"
        ),
        None,
    )

    audio = next(
        (
            x for x in streams
            if x.get("codec_type") == "audio"
        ),
        None,
    )

    return video, audio


# ============================================================
# FILE TYPE
# ============================================================

def detect_type(file_path):

    suffix = (
        Path(file_path)
        .suffix
        .lower()
    )

    if suffix in VIDEO_EXTENSIONS:

        return "video"

    if suffix in AUDIO_EXTENSIONS:

        return "audio"

    if suffix in IMAGE_EXTENSIONS:

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
# VIDEO CONVERSION
# ============================================================

def convert_video(input_file):

    input_file = Path(
        input_file
    )

    output_file = (
        input_file.parent /
        f"{input_file.stem}_telegram.mp4"
    )

    video, audio = get_streams(
        input_file
    )

    if not video:

        return None, (
            "No video stream found."
        )

    width = int(
        video.get("width") or 0
    )

    height = int(
        video.get("height") or 0
    )

    codec = video.get(
        "codec_name",
        "",
    )

    pix_fmt = video.get(
        "pix_fmt",
        "",
    )

    logger.info(
        "Video: %sx%s %s %s",
        width,
        height,
        codec,
        pix_fmt,
    )

    logger.info(
        "Audio: %s",
        audio.get("codec_name")
        if audio else "NONE",
    )

    # --------------------------------------------------------
    # Video filter
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

        "-map",
        "0:v:0",

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

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-ar",
        "44100",

        "-ac",
        "2",

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

        return None, (
            "Video conversion timed out."
        )

    except Exception as e:

        return None, str(e)

    if result.returncode != 0:

        return None, (
            result.stderr[-4000:]
        )

    if not output_file.exists():

        return None, (
            "FFmpeg did not create output."
        )

    out_video, out_audio = (
        get_streams(output_file)
    )

    if not out_video:

        return None, (
            "Output contains no video."
        )

    # If original had audio,
    # output should have audio.
    if audio and not out_audio:

        return None, (
            "Audio was lost during conversion."
        )

    return output_file, None


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def get_ydl_options(
    output_template,
    url,
):

    cookie_file = None

    if is_instagram(url):

        cookie_file = (
            create_cookie_file()
        )

    options = {

        # ----------------------------------------------------
        # Universal video/audio selection
        # ----------------------------------------------------

        "format": (
            "bv*+ba/"
            "b"
        ),

        "outtmpl": output_template,

        "noplaylist": False,

        "quiet": False,

        "no_warnings": False,

        "retries": 3,

        "fragment_retries": 3,

        "file_access_retries": 3,

        "socket_timeout": 30,

        "concurrent_fragment_downloads": 4,

        "merge_output_format": "mp4",

        "http_headers": {
            "User-Agent":
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Safari/537.36",

            "Accept":
                "*/*",

            "Accept-Language":
                "en-US,en;q=0.9",
        },
    }

    if cookie_file:

        options["cookiefile"] = (
            cookie_file
        )

    return options


# ============================================================
# DOWNLOAD
# ============================================================

def download_media(url):

    folder = Path(
        tempfile.mkdtemp(
            dir=BASE_DIR
        )
    )

    output_template = str(
        folder /
        "%(playlist_index)03d_%(id)s.%(ext)s"
    )

    attempts = []

    # Original URL
    attempts.append(url)

    # Clean URL
    cleaned = clean_url(url)

    if cleaned != url:

        attempts.append(
            cleaned
        )

    last_error = None

    for attempt_number, current_url in enumerate(
        attempts,
        start=1,
    ):

        logger.info(
            "Download attempt %s: %s",
            attempt_number,
            current_url,
        )

        try:

            options = get_ydl_options(
                output_template,
                current_url,
            )

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                info = ydl.extract_info(
                    current_url,
                    download=True,
                )

                if not info:

                    last_error = (
                        "No media information returned."
                    )

                    continue

                logger.info(
                    "Title: %s",
                    info.get("title"),
                )

                logger.info(
                    "Extractor: %s",
                    info.get("extractor"),
                )

                logger.info(
                    "Type: %s",
                    info.get("_type"),
                )

        except Exception as e:

            last_error = str(e)

            logger.error(
                "yt-dlp error: %s",
                last_error,
            )

            continue

        # ----------------------------------------------------
        # Find all downloaded media
        # ----------------------------------------------------

        files = []

        for file in folder.iterdir():

            if not file.is_file():
                continue

            if (
                file.suffix.lower()
                in VIDEO_EXTENSIONS
                or file.suffix.lower()
                in AUDIO_EXTENSIONS
                or file.suffix.lower()
                in IMAGE_EXTENSIONS
            ):

                files.append(file)

        if files:

            logger.info(
                "Downloaded %s media file(s).",
                len(files),
            )

            return files, folder, None

    return (
        None,
        folder,
        last_error or "Download failed.",
    )


# ============================================================
# SEND IMAGE
# ============================================================

async def send_image(
    update,
    file,
):

    size = file.stat().st_size

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

    if file.stat().st_size > MAX_FILE_SIZE:

        await update.message.reply_document(
            document=open(file, "rb"),
            caption="🎵 Audio",
        )

        return

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

    if file.stat().st_size > MAX_FILE_SIZE:

        return False

    with open(
        file,
        "rb",
    ) as document:

        await update.message.reply_document(
            document=document
        )

    return True


# ============================================================
# SEND VIDEO
# ============================================================

async def send_video(
    update,
    file,
):

    file = Path(file)

    # --------------------------------------------------------
    # If already small enough, convert only when necessary
    # --------------------------------------------------------

    video_info = probe(file)

    if not video_info:

        return False, (
            "Could not inspect video."
        )

    streams = video_info.get(
        "streams",
        [],
    )

    video_stream = next(
        (
            x for x in streams
            if x.get("codec_type") == "video"
        ),
        None,
    )

    audio_stream = next(
        (
            x for x in streams
            if x.get("codec_type") == "audio"
        ),
        None,
    )

    codec = (
        video_stream.get("codec_name")
        if video_stream
        else ""
    )

    pix_fmt = (
        video_stream.get("pix_fmt")
        if video_stream
        else ""
    )

    height = int(
        video_stream.get("height") or 0
    ) if video_stream else 0

    # --------------------------------------------------------
    # Convert if needed
    # --------------------------------------------------------

    needs_conversion = (
        codec != "h264"
        or pix_fmt != "yuv420p"
        or height > MAX_VIDEO_HEIGHT
        or file.stat().st_size > MAX_FILE_SIZE
    )

    final_file = file

    if needs_conversion:

        converted, error = (
            await asyncio.to_thread(
                convert_video,
                file,
            )
        )

        if not converted:

            return False, error

        final_file = converted

    # --------------------------------------------------------
    # Size check
    # --------------------------------------------------------

    if (
        final_file.stat().st_size
        > MAX_FILE_SIZE
    ):

        return False, (
            "Video is larger than the "
            "Telegram upload limit."
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

    return True, None


# ============================================================
# SEND MEDIA
# ============================================================

async def send_media_file(
    update,
    file,
):

    media_type = detect_type(
        file
    )

    logger.info(
        "Sending %s: %s",
        media_type,
        file,
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

        return True, None

    if media_type == "image":

        await send_image(
            update,
            file,
        )

        return True, None

    ok = await send_document(
        update,
        file,
    )

    if not ok:

        return False, (
            "File is too large for Telegram."
        )

    return True, None


# ============================================================
# ANIMATED STATUS
# ============================================================

async def send_status(update):

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
            "Animation error: %s",
            e,
        )

        return await update.message.reply_text(
            "Thanks for providing the link!\n\n"
            "⚡"
        )


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "👋 Welcome to Universal Media Downloader!\n\n"
        "Send me a media link.\n\n"
        "I can download:\n"
        "🎬 Videos\n"
        "🎵 Audio\n"
        "🖼️ Images\n"
        "📚 Carousels\n"
        "📱 Shorts/Reels\n\n"
        "Supported sites include Instagram, "
        "YouTube, TikTok, Facebook and many "
        "other sites supported by yt-dlp."
    )


# ============================================================
# MAIN HANDLER
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
            "❌ Please send a valid media URL."
        )

        return

    # ========================================================
    # ANIMATION
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

        if error:

            try:
                await status.delete()
            except Exception:
                pass

            if (
                is_instagram(url)
                and (
                    "empty media response"
                    in error.lower()
                    or "cookies"
                    in error.lower()
                    or "authentication"
                    in error.lower()
                )
            ):

                message = (
                    "❌ Instagram did not provide "
                    "the media.\n\n"
                    "Possible reasons:\n"
                    "• Login is required\n"
                    "• Instagram cookies expired\n"
                    "• The post is restricted\n"
                    "• Instagram blocked the server\n"
                    "• Instagram changed its media API\n\n"
                    "Check INSTAGRAM_COOKIES_B64."
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
        # REMOVE STATUS
        # ====================================================

        try:
            await status.delete()
        except Exception:
            pass

        # ====================================================
        # MEDIA COUNT
        # ====================================================

        logger.info(
            "Total media: %s",
            len(files),
        )

        # ====================================================
        # SEND EACH MEDIA
        # ====================================================

        sent = 0

        for index, file in enumerate(
            files,
            start=1,
        ):

            if not file.exists():
                continue

            logger.info(
                "Processing %s/%s: %s",
                index,
                len(files),
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
                        "⚠️ Could not send "
                        f"{file.name}.\n\n"
                        f"{send_error}"
                    )

            except Exception as e:

                logger.exception(
                    "Send error"
                )

                await update.message.reply_text(
                    "⚠️ Could not send media.\n\n"
                    f"{str(e)[:1500]}"
                )

        # ====================================================
        # RESULT
        # ====================================================

        if sent == 0:

            await update.message.reply_text(
                "❌ No downloadable media "
                "could be sent."
            )

        elif len(files) > 1:

            await update.message.reply_text(
                f"✅ Done — {sent} media files sent."
            )

    except Exception as e:

        logger.exception(
            "Universal downloader error"
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

        if folder:

            await asyncio.to_thread(
                shutil.rmtree,
                folder,
                True,
            )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "Telegram error",
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
        "yt-dlp: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "FFmpeg: %s",
        ffmpeg_available(),
    )

    logger.info(
        "Instagram cookies: %s",
        bool(INSTAGRAM_COOKIES_B64),
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
        "Bot is running..."
    )

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
