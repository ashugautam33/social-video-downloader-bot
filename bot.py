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
# CREATE LIGHTNING GIF
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

        # Lightning
        draw.polygon(
            points,
            fill=(255, 215, 0, 255),
        )

        # Border
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

    logger.info("Lightning animation created")


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

        data = base64.b64decode(
            INSTAGRAM_COOKIES_B64
        )

        cookie_file.write_bytes(data)

        return str(cookie_file)

    except Exception as e:

        logger.error(
            "Cookie error: %s",
            e,
        )

        return None


# ============================================================
# FFMPEG CHECK
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
# FFPROBE
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
                s
                for s in streams
                if s.get("codec_type") == "video"
            ),
            None,
        )

        audio = next(
            (
                s
                for s in streams
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
# CONVERT VIDEO + AUDIO
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
            "Unable to inspect downloaded file."
        )

    video = info.get("video")
    audio = info.get("audio")

    if not video:

        return None, (
            "Downloaded file has no video stream."
        )

    width = int(
        video.get("width") or 0
    )

    height = int(
        video.get("height") or 0
    )

    video_codec = video.get(
        "codec_name",
        "",
    )

    audio_codec = (
        audio.get("codec_name")
        if audio
        else None
    )

    logger.info(
        "Input video: %sx%s",
        width,
        height,
    )

    logger.info(
        "Input video codec: %s",
        video_codec,
    )

    logger.info(
        "Input audio codec: %s",
        audio_codec,
    )

    logger.info(
        "Input has audio: %s",
        bool(audio),
    )

    # ========================================================
    # SCALE TO MAX 720
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

        # Audio if present
        "-map",
        "0:a:0?",

        # Scale
        "-vf",
        video_filter,

        # H.264
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

    logger.info(
        "Running FFmpeg..."
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

        error = result.stderr[-5000:]

        logger.error(
            "FFmpeg error:\n%s",
            error,
        )

        return None, error

    if not output_file.exists():

        return None, (
            "FFmpeg did not create output."
        )

    # ========================================================
    # VERIFY OUTPUT
    # ========================================================

    output_info = get_media_info(
        output_file
    )

    if not output_info:

        return None, (
            "Unable to verify output video."
        )

    output_video = (
        output_info.get("video")
    )

    output_audio = (
        output_info.get("audio")
    )

    logger.info(
        "Output video present: %s",
        bool(output_video),
    )

    logger.info(
        "Output audio present: %s",
        bool(output_audio),
    )

    if not output_video:

        return None, (
            "Final MP4 has no video."
        )

    # If source had audio, final MUST have audio.
    if audio and not output_audio:

        return None, (
            "Audio existed in the downloaded "
            "file but disappeared during "
            "FFmpeg conversion."
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
        # IMPORTANT FORMAT SELECTION
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

        # Keep logs visible in Railway
        "quiet": False,

        "no_warnings": False,

        "retries": 3,

        "fragment_retries": 3,

        "file_access_retries": 3,

        "socket_timeout": 30,

        "concurrent_fragment_downloads": 4,

        # Let yt-dlp merge video/audio
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

    # ========================================================
    # INSTAGRAM COOKIES
    # ========================================================

    if (
        cookie_file
        and "instagram.com" in url.lower()
    ):

        options["cookiefile"] = cookie_file

        logger.info(
            "Instagram cookies enabled."
        )

    return options


# ============================================================
# DOWNLOAD
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
            "================================"
        )

        logger.info(
            "Starting download"
        )

        logger.info(
            "URL: %s",
            url,
        )

        logger.info(
            "================================"
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

            # =================================================
            # DEBUG INFORMATION
            # =================================================

            logger.info(
                "Selected format: %s",
                info.get("format"),
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

            logger.info(
                "Format ID: %s",
                info.get("format_id"),
            )

        # ====================================================
        # FIND MEDIA FILES
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
                "Downloaded media file "
                "was not found."
            )

        # Largest media file
        input_file = max(
            media_files,
            key=lambda p: p.stat().st_size,
        )

        logger.info(
            "Downloaded file: %s",
            input_file,
        )

        logger.info(
            "Downloaded size: %.2f MB",
            input_file.stat().st_size
            / 1024
            / 1024,
        )

        # ====================================================
        # INSPECT DOWNLOADED FILE
        # ====================================================

        media_info = get_media_info(
            input_file
        )

        if media_info:

            downloaded_video = (
                media_info.get("video")
            )

            downloaded_audio = (
                media_info.get("audio")
            )

            logger.info(
                "Downloaded video stream: %s",
                bool(downloaded_video),
            )

            logger.info(
                "Downloaded audio stream: %s",
                bool(downloaded_audio),
            )

            if downloaded_audio:

                logger.info(
                    "Downloaded audio codec: %s",
                    downloaded_audio.get(
                        "codec_name"
                    ),
                )

            else:

                logger.warning(
                    "WARNING: downloaded file "
                    "contains NO AUDIO."
                )

        return input_file, None

    except Exception as e:

        logger.exception(
            "yt-dlp error"
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
# STATUS ANIMATION
# ============================================================

async def send_status(update):

    try:

        with open(
            LIGHTNING_GIF,
            "rb",
        ) as animation:

            message = (
                await update.message.reply_animation(
                    animation=animation,
                    caption=(
                        "Thanks for providing "
                        "the link!"
                    ),
                )
            )

        return message

    except Exception as e:

        logger.error(
            "Lightning animation error: %s",
            e,
        )

        return await update.message.reply_text(
            "Thanks for providing the link!\n\n"
            "⚡"
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
    # SUPPORTED WEBSITE
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
    # ANIMATION
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
                "Technical reason:\n\n"
                f"{error[-3500:]}"
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
                "Technical reason:\n\n"
                f"{error[-3500:]}"
            )

            return

        # ====================================================
        # FINAL CHECK
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

        final_video = (
            final_info.get("video")
        )

        final_audio = (
            final_info.get("audio")
        )

        if not final_video:

            try:
                await status.delete()
            except Exception:
                pass

            await update.message.reply_text(
                "❌ Final video has no video stream."
            )

            return

        if not final_audio:

            try:
                await status.delete()
            except Exception:
                pass

            await update.message.reply_text(
                "❌ The final video has no audio.\n\n"
                "Please check the Railway logs. "
                "The bot now reports the selected "
                "video and audio formats."
            )

            return

        logger.info(
            "Final audio codec: %s",
            final_audio.get(
                "codec_name"
            ),
        )

        # ====================================================
        # SIZE
        # ====================================================

        file_size = (
            output_file.stat().st_size
        )

        logger.info(
            "Final size: %.2f MB",
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
        # DELETE ANIMATION
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
            f"{str(e)[:3500]}"
        )

    finally:

        if temp_dir:

            await asyncio.to_thread(
                cleanup_folder,
                temp_dir,
            )


# ============================================================
# TELEGRAM ERROR
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
        "================================"
    )

    logger.info(
        "Telegram downloader starting"
    )

    logger.info(
        "yt-dlp: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "FFmpeg available: %s",
        check_ffmpeg(),
    )

    logger.info(
        "Instagram cookies: %s",
        bool(INSTAGRAM_COOKIES_B64),
    )

    logger.info(
        "================================"
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
