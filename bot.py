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

MAX_SIZE = 45 * 1024 * 1024
MAX_HEIGHT = 720

BASE_DIR = Path(tempfile.gettempdir()) / "telegram_downloader"
BASE_DIR.mkdir(parents=True, exist_ok=True)

LIGHTNING_GIF = BASE_DIR / "lightning.gif"

SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# LIGHTNING ANIMATION
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

        pulse = 1.0 + (0.08 * (i % 6) / 5)

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

        draw.polygon(
            points,
            fill=(255, 215, 0, 255),
        )

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
# URL
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


def clean_instagram_url(url):

    """
    Remove Instagram tracking/query parameters.

    Example:
    /reel/ABC/?igsh=xxxx
    becomes:
    /reel/ABC/
    """

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


def is_supported_url(url):

    url = url.lower()

    return any(
        host in url
        for host in SUPPORTED_HOSTS
    )


# ============================================================
# INSTAGRAM COOKIES
# ============================================================

def create_cookie_file():

    if not INSTAGRAM_COOKIES_B64:
        return None

    try:

        cookie_file = (
            BASE_DIR /
            "instagram_cookies.txt"
        )

        cookie_data = base64.b64decode(
            INSTAGRAM_COOKIES_B64
        )

        cookie_file.write_bytes(
            cookie_data
        )

        logger.info(
            "Instagram cookie file created."
        )

        return str(cookie_file)

    except Exception as e:

        logger.error(
            "Unable to create Instagram cookies: %s",
            e,
        )

        return None


# ============================================================
# FFMPEG
# ============================================================

def check_ffmpeg():

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
# MEDIA INFO
# ============================================================

def get_media_info(file_path):

    try:

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
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

        data = json.loads(
            result.stdout
        )

        streams = data.get(
            "streams",
            [],
        )

        video = next(
            (
                s for s in streams
                if s.get("codec_type") == "video"
            ),
            None,
        )

        audio = next(
            (
                s for s in streams
                if s.get("codec_type") == "audio"
            ),
            None,
        )

        return {
            "video": video,
            "audio": audio,
            "streams": streams,
        }

    except Exception as e:

        logger.error(
            "ffprobe error: %s",
            e,
        )

        return None


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    output_template,
    url,
    cookie_file=None,
):

    options = {

        # ====================================================
        # VIDEO + AUDIO
        # ====================================================

        "format": (
            "bv*[height<=720]+ba/"
            "bv*[height<=1080]+ba/"
            "bv*+ba/"
            "b[height<=720]/"
            "b[height<=1080]/"
            "b"
        ),

        "outtmpl": output_template,

        "noplaylist": True,

        "playlist": False,

        "merge_output_format": "mp4",

        "retries": 2,

        "fragment_retries": 2,

        "file_access_retries": 2,

        "socket_timeout": 25,

        "concurrent_fragment_downloads": 4,

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

        # Keep Railway logs useful
        "quiet": False,

        "no_warnings": False,
    }

    if cookie_file:

        options["cookiefile"] = cookie_file

    return options


# ============================================================
# FIND MEDIA FILE
# ============================================================

def find_media_file(folder):

    candidates = []

    for file in folder.iterdir():

        if not file.is_file():
            continue

        if file.suffix.lower() in (
            ".mp4",
            ".mkv",
            ".webm",
            ".mov",
            ".m4v",
        ):

            candidates.append(file)

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda p: p.stat().st_size,
    )


# ============================================================
# SINGLE YT-DLP ATTEMPT
# ============================================================

def download_attempt(
    url,
    folder,
    attempt_name,
):

    output_template = str(
        folder /
        "%(id)s.%(ext)s"
    )

    cookie_file = create_cookie_file()

    options = build_ydl_options(
        output_template,
        url,
        cookie_file,
    )

    logger.info(
        "========================================"
    )

    logger.info(
        "Instagram attempt: %s",
        attempt_name,
    )

    logger.info(
        "URL: %s",
        url,
    )

    logger.info(
        "Cookies: %s",
        bool(cookie_file),
    )

    logger.info(
        "========================================"
    )

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True,
            )

            if not info:

                return None, (
                    "yt-dlp returned no media information."
                )

            # Log format details
            logger.info(
                "Format: %s",
                info.get("format"),
            )

            logger.info(
                "Format ID: %s",
                info.get("format_id"),
            )

            logger.info(
                "Video codec: %s",
                info.get("vcodec"),
            )

            logger.info(
                "Audio codec: %s",
                info.get("acodec"),
            )

            logger.info(
                "Requested downloads: %s",
                info.get(
                    "requested_downloads"
                ),
            )

        file = find_media_file(
            folder
        )

        if not file:

            return None, (
                "yt-dlp completed but no "
                "video file was created."
            )

        logger.info(
            "Downloaded file: %s",
            file,
        )

        media_info = get_media_info(
            file
        )

        if media_info:

            video = media_info.get(
                "video"
            )

            audio = media_info.get(
                "audio"
            )

            logger.info(
                "Downloaded video stream: %s",
                bool(video),
            )

            logger.info(
                "Downloaded audio stream: %s",
                bool(audio),
            )

            if audio:

                logger.info(
                    "Audio codec: %s",
                    audio.get(
                        "codec_name"
                    ),
                )

        return file, None

    except Exception as e:

        error = str(e)

        logger.error(
            "Download attempt failed: %s",
            error,
        )

        return None, error


# ============================================================
# DOWNLOAD WITH INSTAGRAM RETRIES
# ============================================================

def download_video(url):

    folder = Path(
        tempfile.mkdtemp(
            dir=BASE_DIR
        )
    )

    # --------------------------------------------------------
    # ATTEMPT 1
    # Original URL + cookies
    # --------------------------------------------------------

    file, error = download_attempt(
        url,
        folder,
        "original URL",
    )

    if file:

        return file, folder, None

    # --------------------------------------------------------
    # ATTEMPT 2
    # Clean Instagram URL
    # --------------------------------------------------------

    if is_instagram(url):

        clean_url = clean_instagram_url(
            url
        )

        if clean_url != url:

            logger.info(
                "Trying cleaned Instagram URL..."
            )

            file, error2 = download_attempt(
                clean_url,
                folder,
                "clean Instagram URL",
            )

            if file:

                return file, folder, None

            error = error2

    # --------------------------------------------------------
    # ATTEMPT 3
    # Same URL with trailing slash normalized
    # --------------------------------------------------------

    if is_instagram(url):

        clean_url = clean_instagram_url(
            url
        ).rstrip("/") + "/"

        file, error3 = download_attempt(
            clean_url,
            folder,
            "normalized Instagram URL",
        )

        if file:

            return file, folder, None

        error = error3

    # --------------------------------------------------------
    # ALL FAILED
    # --------------------------------------------------------

    return None, folder, error


# ============================================================
# CONVERT TO IPHONE MP4
# ============================================================

def convert_video(input_file):

    input_file = Path(input_file)

    output_file = (
        input_file.parent /
        f"{input_file.stem}_iphone.mp4"
    )

    info = get_media_info(
        input_file
    )

    if not info:

        return None, (
            "Unable to inspect downloaded video."
        )

    video = info.get("video")
    audio = info.get("audio")

    if not video:

        return None, (
            "No video stream found."
        )

    logger.info(
        "Source video codec: %s",
        video.get("codec_name"),
    )

    logger.info(
        "Source audio codec: %s",
        audio.get("codec_name")
        if audio else "NONE",
    )

    # ========================================================
    # SCALE
    # ========================================================

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

    # ========================================================
    # FFMPEG
    # ========================================================

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(input_file),

        # Video
        "-map",
        "0:v:0",

        # Audio if available
        "-map",
        "0:a:0?",

        # Scale
        "-vf",
        video_filter,

        # H264
        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "27",

        "-pix_fmt",
        "yuv420p",

        "-r",
        "30",

        # AAC
        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-ar",
        "44100",

        "-ac",
        "2",

        # MP4
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
            "FFmpeg conversion timed out."
        )

    except Exception as e:

        return None, str(e)

    if result.returncode != 0:

        error = result.stderr[-5000:]

        logger.error(
            "FFmpeg error:\n%s",
            error,
        )

        return None, error

    if not output_file.exists():

        return None, (
            "FFmpeg did not create output file."
        )

    # ========================================================
    # VERIFY
    # ========================================================

    final_info = get_media_info(
        output_file
    )

    if not final_info:

        return None, (
            "Unable to verify converted video."
        )

    final_video = final_info.get(
        "video"
    )

    final_audio = final_info.get(
        "audio"
    )

    logger.info(
        "Final video stream: %s",
        bool(final_video),
    )

    logger.info(
        "Final audio stream: %s",
        bool(final_audio),
    )

    if not final_video:

        return None, (
            "Final MP4 has no video."
        )

    # Only require audio if source had it.
    if audio and not final_audio:

        return None, (
            "Source contained audio but "
            "FFmpeg output does not."
        )

    return output_file, None


# ============================================================
# CLEANUP
# ============================================================

def cleanup_folder(folder):

    try:

        shutil.rmtree(
            folder,
            ignore_errors=True,
        )

    except Exception:
        pass


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "Send me an Instagram, YouTube, "
        "TikTok or Facebook video link."
    )


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
                    "Thanks for providing "
                    "the link!"
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
# MAIN MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = update.message.text or ""

    url = extract_url(text)

    # ========================================================
    # URL
    # ========================================================

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid video link."
        )

        return

    # ========================================================
    # SUPPORTED
    # ========================================================

    if not is_supported_url(url):

        await update.message.reply_text(
            "❌ Website not supported.\n\n"
            "Supported:\n"
            "• Instagram\n"
            "• YouTube\n"
            "• TikTok\n"
            "• Facebook"
        )

        return

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

        input_file, folder, error = (
            await asyncio.to_thread(
                download_video,
                url,
            )
        )

        if error:

            try:
                await status.delete()
            except Exception:
                pass

            # Better Instagram error
            if (
                is_instagram(url)
                and (
                    "empty media response"
                    in error.lower()
                    or "authentication"
                    in error.lower()
                    or "cookies"
                    in error.lower()
                )
            ):

                message = (
                    "❌ Instagram could not provide "
                    "the media to yt-dlp.\n\n"
                    "Possible reasons:\n"
                    "• Instagram requires login\n"
                    "• Instagram cookies are expired\n"
                    "• The Reel is restricted\n"
                    "• Instagram temporarily blocked "
                    "the Railway server IP\n"
                    "• Instagram changed its media API\n\n"
                    "Make sure INSTAGRAM_COOKIES_B64 "
                    "contains fresh cookies."
                )

            else:

                message = (
                    "❌ Download failed.\n\n"
                    "Technical reason:\n\n"
                    f"{error[-3000:]}"
                )

            await update.message.reply_text(
                message
            )

            return

        # ====================================================
        # CONVERT
        # ====================================================

        output_file, conversion_error = (
            await asyncio.to_thread(
                convert_video,
                input_file,
            )
        )

        if conversion_error:

            try:
                await status.delete()
            except Exception:
                pass

            await update.message.reply_text(
                "❌ Video conversion failed.\n\n"
                "Technical reason:\n\n"
                f"{conversion_error[-3000:]}"
            )

            return

        # ====================================================
        # FINAL INFO
        # ====================================================

        final_info = get_media_info(
            output_file
        )

        if not final_info:

            try:
                await status.delete()
            except Exception:
                pass

            await update.message.reply_text(
                "❌ Could not verify final video."
            )

            return

        if not final_info.get("video"):

            try:
                await status.delete()
            except Exception:
                pass

            await update.message.reply_text(
                "❌ Final file contains no video."
            )

            return

        # ====================================================
        # AUDIO INFORMATION
        # ====================================================

        final_audio = final_info.get(
            "audio"
        )

        if final_audio:

            logger.info(
                "Final audio codec: %s",
                final_audio.get(
                    "codec_name"
                ),
            )

        else:

            logger.warning(
                "Final file has no audio."
            )

        # ====================================================
        # SIZE
        # ====================================================

        file_size = (
            output_file.stat().st_size
        )

        logger.info(
            "Final file: %.2f MB",
            file_size / 1024 / 1024,
        )

        if file_size > MAX_SIZE:

            try:
                await status.delete()
            except Exception:
                pass

            await update.message.reply_text(
                "❌ Video is too large.\n\n"
                f"Maximum allowed: "
                f"{MAX_SIZE / 1024 / 1024:.0f} MB"
            )

            return

        # ====================================================
        # REMOVE ANIMATION
        # ====================================================

        try:
            await status.delete()
        except Exception:
            pass

        # ====================================================
        # SEND VIDEO
        # ====================================================

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        with open(
            output_file,
            "rb",
        ) as video:

            await update.message.reply_video(
                video=video,
                supports_streaming=True,
                caption="✅ Done",
            )

    except Exception as e:

        logger.exception(
            "Unexpected error"
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
                cleanup_folder,
                folder,
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
        "Telegram Video Downloader"
    )

    logger.info(
        "yt-dlp: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "FFmpeg: %s",
        check_ffmpeg(),
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
