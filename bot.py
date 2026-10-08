import os
import re
import asyncio
import logging
import base64
import tempfile
import subprocess
import shutil
import json

from pathlib import Path

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
        for width, alpha in [
            (30, 25),
            (20, 45),
            (12, 75),
        ]:

            draw.line(
                points + [points[0]],
                fill=(255, 220, 0, alpha),
                width=width,
                joint="curve",
            )

        # Main lightning
        draw.polygon(
            points,
            fill=(255, 215, 0, 255),
        )

        # White border
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

    logger.info("Animated lightning created")


# ============================================================
# EXTRACT URL
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


# ============================================================
# CHECK SUPPORTED URL
# ============================================================

def is_supported_url(url):

    url = url.lower()

    return any(
        host in url
        for host in SUPPORTED_HOSTS
    )


# ============================================================
# CREATE INSTAGRAM COOKIE FILE
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

        return str(cookie_file)

    except Exception as e:

        logger.error(
            "Cookie error: %s",
            e,
        )

        return None


# ============================================================
# CHECK FFMPEG
# ============================================================

def check_ffmpeg():

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

        return result.returncode == 0

    except Exception:

        return False


# ============================================================
# MEDIA INFORMATION
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
                stream
                for stream in streams
                if stream.get("codec_type")
                == "video"
            ),
            None,
        )

        audio = next(
            (
                stream
                for stream in streams
                if stream.get("codec_type")
                == "audio"
            ),
            None,
        )

        return {
            "video": video,
            "audio": audio,
        }

    except Exception as e:

        logger.error(
            "ffprobe error: %s",
            e,
        )

        return None


# ============================================================
# CONVERT VIDEO
# ============================================================

def convert_video(input_file):

    input_file = Path(input_file)

    output_file = (
        input_file.parent /
        f"{input_file.stem}_final.mp4"
    )

    info = get_media_info(
        input_file
    )

    if not info:
        return None, "Unable to inspect video."

    video = info.get("video")
    audio = info.get("audio")

    if not video:
        return None, "No video stream found."

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

    pixel_format = video.get(
        "pix_fmt",
        "",
    )

    has_audio = audio is not None

    logger.info(
        "Input: %sx%s codec=%s pixel=%s audio=%s",
        width,
        height,
        codec,
        pixel_format,
        has_audio,
    )

    # ========================================================
    # SAFE VIDEO FILTER
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
    # FFMPEG COMMAND
    # ========================================================

    command = [
        "ffmpeg",
        "-y",

        "-i",
        str(input_file),

        # VIDEO
        "-map",
        "0:v:0",

        # AUDIO
        "-map",
        "0:a:0?",

        # VIDEO FILTER
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

        # 30 FPS
        "-r",
        "30",

        # AUDIO
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

    logger.info(
        "Starting FFmpeg conversion"
    )

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

        error = result.stderr[-4000:]

        logger.error(
            "FFmpeg failed:\n%s",
            error,
        )

        return None, error

    if not output_file.exists():

        return None, (
            "FFmpeg did not create "
            "the output file."
        )

    # ========================================================
    # VERIFY OUTPUT
    # ========================================================

    output_info = get_media_info(
        output_file
    )

    if not output_info:
        return None, (
            "Unable to verify converted video."
        )

    output_video = output_info.get(
        "video"
    )

    output_audio = output_info.get(
        "audio"
    )

    if not output_video:

        return None, (
            "Converted file has no video."
        )

    if has_audio and not output_audio:

        logger.error(
            "Audio was present in source "
            "but missing from output."
        )

        return None, (
            "Audio was present in the "
            "source video but was lost "
            "during conversion."
        )

    logger.info(
        "Output verified: video=%s audio=%s",
        bool(output_video),
        bool(output_audio),
    )

    return output_file, None


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def get_ydl_options(
    output_template,
    url,
):

    cookie_file = create_cookie_file()

    options = {

        # ====================================================
        # IMPORTANT:
        # Prefer separate VIDEO + AUDIO
        # ====================================================

        "format": (
            "bestvideo[height<=720]+bestaudio/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=720]/"
            "best[height<=1080]/"
            "best"
        ),

        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        # Download retries
        "retries": 3,

        "fragment_retries": 3,

        "file_access_retries": 3,

        "socket_timeout": 20,

        # Faster fragmented downloads
        "concurrent_fragment_downloads": 4,

        # IMPORTANT
        # yt-dlp will merge separate
        # video/audio streams.
        "merge_output_format": "mp4",

        # Don't download playlists
        "playlist": False,

        "http_headers": {
            "User-Agent":
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Safari/537.36",

            "Accept-Language":
                "en-US,en;q=0.9",
        },
    }

    # ========================================================
    # INSTAGRAM COOKIES
    # ========================================================

    if (
        cookie_file
        and "instagram.com" in url.lower()
    ):

        options["cookiefile"] = (
            cookie_file
        )

    return options


# ============================================================
# DOWNLOAD VIDEO
# ============================================================

def download_video(url):

    temp_dir = Path(
        tempfile.mkdtemp(
            dir=BASE_DIR
        )
    )

    output_template = str(
        temp_dir /
        "%(id)s.%(ext)s"
    )

    options = get_ydl_options(
        output_template,
        url,
    )

    try:

        logger.info(
            "Downloading: %s",
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

                return None, (
                    "yt-dlp returned no information."
                )

        # ====================================================
        # FIND DOWNLOADED MEDIA
        # ====================================================

        media_files = []

        for file in temp_dir.iterdir():

            if not file.is_file():
                continue

            if file.suffix.lower() in (
                ".mp4",
                ".mkv",
                ".webm",
                ".mov",
                ".m4v",
            ):

                media_files.append(
                    file
                )

        if not media_files:

            return None, (
                "Downloaded video file "
                "was not found."
            )

        # Largest media file
        input_file = max(
            media_files,
            key=lambda p: p.stat().st_size,
        )

        # ====================================================
        # CHECK DOWNLOADED AUDIO
        # ====================================================

        media_info = get_media_info(
            input_file
        )

        if media_info:

            has_audio = bool(
                media_info.get("audio")
            )

            logger.info(
                "Downloaded file audio: %s",
                has_audio,
            )

            if not has_audio:

                logger.warning(
                    "Downloaded file contains "
                    "NO AUDIO STREAM."
                )

        return input_file, None

    except Exception as e:

        logger.exception(
            "yt-dlp download error"
        )

        return None, str(e)


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

async def send_status(
    update,
):

    try:

        message = (
            await update.message.reply_animation(
                animation=open(
                    LIGHTNING_GIF,
                    "rb",
                ),
                caption=(
                    "Thanks for providing "
                    "the link!"
                ),
            )
        )

        return message

    except Exception as e:

        logger.error(
            "Animation failed: %s",
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
    # URL CHECK
    # ========================================================

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid video link."
        )

        return

    # ========================================================
    # WEBSITE CHECK
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
    # ANIMATED LIGHTNING
    # ========================================================

    status = await send_status(
        update
    )

    temp_dir = None

    try:

        # ====================================================
        # DOWNLOAD
        # ====================================================

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        input_file, error = (
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

            await update.message.reply_text(
                "❌ Download failed.\n\n"
                "Technical reason:\n"
                f"{error[-3000:]}"
            )

            return

        temp_dir = input_file.parent

        # ====================================================
        # CONVERT
        # ====================================================

        output_file, error = (
            await asyncio.to_thread(
                convert_video,
                input_file,
            )
        )

        if error:

            try:
                await status.delete()
            except Exception:
                pass

            await update.message.reply_text(
                "❌ Video conversion failed.\n\n"
                "Technical reason:\n"
                f"{error[-3000:]}"
            )

            return

        # ====================================================
        # FINAL AUDIO CHECK
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

        final_audio = (
            final_info.get("audio")
        )

        if not final_audio:

            try:
                await status.delete()
            except Exception:
                pass

            await update.message.reply_text(
                "❌ The final video has no audio.\n\n"
                "The source platform may have "
                "provided a video-only stream."
            )

            return

        # ====================================================
        # SIZE CHECK
        # ====================================================

        size = output_file.stat().st_size

        logger.info(
            "Final video size: %.2f MB",
            size / 1024 / 1024,
        )

        if size > MAX_SIZE:

            try:
                await status.delete()
            except Exception:
                pass

            await update.message.reply_text(
                "❌ Video is too large.\n\n"
                f"Maximum: "
                f"{MAX_SIZE / 1024 / 1024:.0f} MB"
            )

            return

        # ====================================================
        # DELETE LIGHTNING
        # ====================================================

        try:
            await status.delete()
        except Exception:
            pass

        # ====================================================
        # UPLOAD
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
            "Unexpected bot error"
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
        # CLEANUP
        # ====================================================

        if temp_dir:

            await asyncio.to_thread(
                cleanup_folder,
                temp_dir,
            )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "Telegram error:",
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

    # Create animation
    create_lightning_gif()

    logger.info(
        "yt-dlp version: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "FFmpeg available: %s",
        check_ffmpeg(),
    )

    logger.info(
        "Instagram cookies configured: %s",
        bool(INSTAGRAM_COOKIES_B64),
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
        "Telegram bot started"
    )

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
