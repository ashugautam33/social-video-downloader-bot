import os
import re
import asyncio
import logging
import base64
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

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "telegram_downloader"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

ANIMATION_FILE = DOWNLOAD_DIR / "lightning.gif"

SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# CREATE ANIMATED LIGHTNING GIF
# ============================================================

def create_lightning_gif():

    if ANIMATION_FILE.exists():
        return

    frames = []

    for i in range(12):

        img = Image.new(
            "RGBA",
            (400, 400),
            (0, 0, 0, 0)
        )

        draw = ImageDraw.Draw(img)

        pulse = 1.0 + (0.08 * (i % 6) / 5)

        cx = 200
        cy = 200

        points = [
            (
                cx + int(35 * pulse),
                cy - int(145 * pulse)
            ),
            (
                cx - int(70 * pulse),
                cy + int(5 * pulse)
            ),
            (
                cx - int(5 * pulse),
                cy + int(5 * pulse)
            ),
            (
                cx - int(45 * pulse),
                cy + int(145 * pulse)
            ),
            (
                cx + int(85 * pulse),
                cy - int(20 * pulse)
            ),
            (
                cx + int(20 * pulse),
                cy - int(20 * pulse)
            ),
        ]

        # Glow
        for width, alpha in [
            (28, 30),
            (20, 50),
            (12, 80),
        ]:
            draw.line(
                points + [points[0]],
                fill=(255, 220, 0, alpha),
                width=width,
                joint="curve",
            )

        # Lightning
        draw.polygon(
            points,
            fill=(255, 215, 0, 255)
        )

        draw.line(
            points + [points[0]],
            fill=(255, 255, 255, 255),
            width=5,
            joint="curve",
        )

        frames.append(img)

    frames[0].save(
        ANIMATION_FILE,
        save_all=True,
        append_images=frames[1:],
        duration=90,
        loop=0,
        disposal=2,
    )

    logger.info("Lightning animation created")


# ============================================================
# URL EXTRACTION
# ============================================================

def extract_url(text):

    if not text:
        return None

    match = re.search(
        r"https?://[^\s]+",
        text
    )

    if not match:
        return None

    url = match.group(0).rstrip(".,!?)]}")

    return url


# ============================================================
# SUPPORTED URL
# ============================================================

def is_supported_url(url):

    url_lower = url.lower()

    return any(
        host in url_lower
        for host in SUPPORTED_HOSTS
    )


# ============================================================
# INSTAGRAM COOKIES
# ============================================================

def create_cookie_file():

    if not INSTAGRAM_COOKIES_B64:
        return None

    try:

        cookie_path = DOWNLOAD_DIR / "instagram_cookies.txt"

        cookie_data = base64.b64decode(
            INSTAGRAM_COOKIES_B64
        )

        cookie_path.write_bytes(cookie_data)

        return str(cookie_path)

    except Exception as e:

        logger.error(
            "Cookie error: %s",
            e
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
            timeout=20,
        )

        if result.returncode != 0:
            return None

        import json

        data = json.loads(result.stdout)

        streams = data.get("streams", [])

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
        }

    except Exception as e:

        logger.warning(
            "ffprobe error: %s",
            e
        )

        return None


# ============================================================
# CONVERT TO IPHONE MP4
# ============================================================

def convert_video(input_file):

    input_file = Path(input_file)

    output_file = input_file.with_name(
        input_file.stem + "_iphone.mp4"
    )

    info = get_media_info(input_file)

    if not info or not info.get("video"):

        return None, "Unable to read video stream."

    video = info["video"]
    audio = info["audio"]

    codec = video.get("codec_name", "")
    pixel_format = video.get("pix_fmt", "")
    width = int(video.get("width", 0) or 0)
    height = int(video.get("height", 0) or 0)

    logger.info(
        "Video: codec=%s size=%sx%s pix_fmt=%s audio=%s",
        codec,
        width,
        height,
        pixel_format,
        bool(audio),
    )

    # --------------------------------------------------------
    # FAST PATH
    # --------------------------------------------------------

    if (
        codec == "h264"
        and pixel_format == "yuv420p"
        and height <= MAX_HEIGHT
    ):

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),
            "-map",
            "0:v:0",
        ]

        if audio:
            command += [
                "-map",
                "0:a:0?",
                "-c:a",
                "copy",
            ]

        command += [
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            str(output_file),
        ]

    else:

        # ----------------------------------------------------
        # SAFE SCALE
        # ----------------------------------------------------

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
        ]

        if audio:

            command += [
                "-map",
                "0:a:0?",

                "-c:a",
                "aac",

                "-b:a",
                "96k",

                "-ar",
                "44100",

                "-ac",
                "2",
            ]

        command += [
            "-movflags",
            "+faststart",

            "-avoid_negative_ts",
            "make_zero",

            str(output_file),
        ]

    logger.info(
        "Running FFmpeg"
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

        return None, "FFmpeg conversion timed out."

    except Exception as e:

        return None, str(e)

    if result.returncode != 0:

        error = result.stderr[-3000:]

        logger.error(
            "FFmpeg error:\n%s",
            error
        )

        return None, error

    if not output_file.exists():

        return None, "FFmpeg did not create the output file."

    return output_file, None


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def get_ydl_options(output_template, url):

    cookie_file = create_cookie_file()

    options = {

        # Fast format selection
        "format":
            "best[height<=720]/"
            "best[height<=1080]/"
            "bestvideo[height<=720]+bestaudio/"
            "bestvideo[height<=1080]+bestaudio/"
            "best",

        "outtmpl": output_template,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "retries": 2,

        "fragment_retries": 2,

        "socket_timeout": 15,

        "concurrent_fragment_downloads": 4,

        "http_headers": {
            "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36",
        },

        "merge_output_format": "mp4",

        "postprocessors": [],
    }

    if cookie_file and "instagram.com" in url.lower():

        options["cookiefile"] = cookie_file

    return options


# ============================================================
# DOWNLOAD
# ============================================================

def download_video(url):

    temp_dir = Path(
        tempfile.mkdtemp(
            dir=DOWNLOAD_DIR
        )
    )

    output_template = str(
        temp_dir / "%(id)s.%(ext)s"
    )

    options = get_ydl_options(
        output_template,
        url
    )

    try:

        logger.info(
            "Downloading: %s",
            url
        )

        with yt_dlp.YoutubeDL(options) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

            if not info:
                return None, "yt-dlp returned no information."

            requested = info.get(
                "requested_downloads"
            )

            candidates = []

            if requested:

                for item in requested:

                    path = item.get(
                        "filepath"
                    )

                    if path:
                        candidates.append(
                            Path(path)
                        )

            if info.get("filepath"):
                candidates.append(
                    Path(info["filepath"])
                )

            if info.get("_filename"):
                candidates.append(
                    Path(info["_filename"])
                )

        # Find actual media file
        files = [
            p for p in temp_dir.iterdir()
            if p.is_file()
            and p.suffix.lower() in (
                ".mp4",
                ".webm",
                ".mkv",
                ".mov",
                ".m4v",
            )
        ]

        candidates.extend(files)

        existing = [
            p for p in candidates
            if p.exists()
        ]

        if not existing:

            return None, "Downloaded file was not found."

        # Largest file is normally the actual video
        input_file = max(
            existing,
            key=lambda p: p.stat().st_size
        )

        logger.info(
            "Downloaded: %s",
            input_file
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

def cleanup_folder(path):

    try:

        import shutil

        shutil.rmtree(
            path,
            ignore_errors=True
        )

    except Exception:
        pass


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "Send me an Instagram, YouTube, TikTok or "
        "Facebook video link and I'll download it for you."
    )


# ============================================================
# ANIMATED LIGHTNING
# ============================================================

async def send_lightning_status(update):

    try:

        message = await update.message.reply_animation(
            animation=str(ANIMATION_FILE),
            caption="Thanks for providing the link!"
        )

        return message

    except Exception as e:

        logger.error(
            "Animation error: %s",
            e
        )

        # Fallback if GIF fails
        return await update.message.reply_text(
            "Thanks for providing the link!\n\n"
            "⚡"
        )


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    text = update.message.text or ""

    url = extract_url(text)

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid video link."
        )

        return

    if not is_supported_url(url):

        await update.message.reply_text(
            "❌ This website is not supported.\n\n"
            "Supported:\n"
            "• Instagram\n"
            "• YouTube\n"
            "• TikTok\n"
            "• Facebook"
        )

        return

    # --------------------------------------------------------
    # SEND ANIMATED LIGHTNING
    # --------------------------------------------------------

    status = await send_lightning_status(
        update
    )

    temp_dir = None

    try:

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        input_file, error = await asyncio.to_thread(
            download_video,
            url
        )

        if error:

            await status.delete()

            await update.message.reply_text(
                "❌ Download failed.\n\n"
                f"Technical reason:\n{error[-2500:]}"
            )

            return

        temp_dir = input_file.parent

        # ----------------------------------------------------
        # CONVERT
        # ----------------------------------------------------

        output_file, error = await asyncio.to_thread(
            convert_video,
            input_file
        )

        if error:

            await status.delete()

            await update.message.reply_text(
                "❌ Video conversion failed.\n\n"
                f"Technical reason:\n{error[-2500:]}"
            )

            return

        # ----------------------------------------------------
        # CHECK SIZE
        # ----------------------------------------------------

        file_size = output_file.stat().st_size

        logger.info(
            "Final size: %.2f MB",
            file_size / 1024 / 1024
        )

        if file_size > MAX_SIZE:

            await status.delete()

            await update.message.reply_text(
                "❌ The converted video is too large.\n\n"
                f"Maximum allowed: "
                f"{MAX_SIZE / 1024 / 1024:.0f} MB"
            )

            return

        # ----------------------------------------------------
        # DELETE ANIMATION
        # ----------------------------------------------------

        try:
            await status.delete()
        except Exception:
            pass

        # ----------------------------------------------------
        # SEND VIDEO
        # ----------------------------------------------------

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        with open(
            output_file,
            "rb"
        ) as video:

            await update.message.reply_video(
                video=video,
                supports_streaming=True,
                width=None,
                height=None,
                caption="✅ Done"
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
            f"{str(e)[:2500]}"
        )

    finally:

        # ----------------------------------------------------
        # CLEANUP
        # ----------------------------------------------------

        if temp_dir:

            await asyncio.to_thread(
                cleanup_folder,
                temp_dir
            )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Telegram error",
        exc_info=context.error
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
        "yt-dlp version: %s",
        yt_dlp.version.__version__
    )

    logger.info(
        "FFmpeg available: %s",
        check_ffmpeg()
    )

    logger.info(
        "Instagram cookies configured: %s",
        bool(INSTAGRAM_COOKIES_B64)
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot started"
    )

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
