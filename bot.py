import os
import re
import asyncio
import logging
import tempfile
import subprocess
from pathlib import Path

import yt_dlp

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
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

MAX_FILE_SIZE = 45 * 1024 * 1024

SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)

# Video extensions only.
VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
    ".flv",
    ".ts",
}


# ============================================================
# URL
# ============================================================

def is_supported_url(url: str) -> bool:
    url_lower = url.lower()

    return any(
        host in url_lower
        for host in SUPPORTED_HOSTS
    )


def extract_url(text: str) -> str | None:
    match = re.search(
        r"https?://[^\s]+",
        text.strip(),
        re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(0).rstrip(").,]}>'\"")


# ============================================================
# FIND VIDEO FILE
# ============================================================

def find_downloaded_video(
    temp_dir: str,
) -> Path | None:

    directory = Path(temp_dir)

    if not directory.exists():
        return None

    video_files = []

    for file in directory.iterdir():

        if not file.is_file():
            continue

        if file.name.endswith(
            (".part", ".ytdl", ".tmp")
        ):
            continue

        if file.suffix.lower() in VIDEO_EXTENSIONS:

            try:
                if file.stat().st_size > 0:
                    video_files.append(file)
            except OSError:
                pass

    if not video_files:
        return None

    # Prefer MP4.
    mp4_files = [
        file
        for file in video_files
        if file.suffix.lower() == ".mp4"
    ]

    if mp4_files:
        return max(
            mp4_files,
            key=lambda file: file.stat().st_size,
        )

    return max(
        video_files,
        key=lambda file: file.stat().st_size,
    )


# ============================================================
# CONVERT VIDEO TO IPHONE COMPATIBLE MP4
# ============================================================

def convert_to_mp4(
    input_file: Path,
    temp_dir: str,
) -> tuple[Path | None, str | None]:

    output_file = (
        Path(temp_dir) / "final_video.mp4"
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),

        # Video
        "-c:v",
        "libx264",

        # Audio
        "-c:a",
        "aac",

        # Compatibility
        "-pix_fmt",
        "yuv420p",

        # Fast start for phones/streaming
        "-movflags",
        "+faststart",

        str(output_file),
    ]

    try:

        logger.info(
            "Converting video to MP4: %s",
            input_file,
        )

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:

            logger.error(
                "FFmpeg error: %s",
                result.stderr[-3000:],
            )

            return (
                None,
                "FFmpeg could not convert the video.",
            )

        if not output_file.exists():

            return (
                None,
                "FFmpeg did not create the MP4 file.",
            )

        if output_file.stat().st_size == 0:

            return (
                None,
                "The converted MP4 file is empty.",
            )

        return output_file, None

    except subprocess.TimeoutExpired:

        return (
            None,
            "Video conversion took too long.",
        )

    except Exception as error:

        logger.exception(
            "FFmpeg conversion error"
        )

        return (
            None,
            f"{type(error).__name__}: {error}",
        )


# ============================================================
# DOWNLOAD VIDEO
# ============================================================

def download_video(
    url: str,
    temp_dir: str,
) -> tuple[Path | None, str | None]:

    output_template = str(
        Path(temp_dir)
        / "download_%(id)s.%(ext)s"
    )

    is_instagram = (
        "instagram.com" in url.lower()
    )

    ydl_opts = {

        "outtmpl": output_template,

        # IMPORTANT:
        # Don't select images.
        "format": (
            "bestvideo[height<=720]+bestaudio/"
            "best[height<=720]/"
            "best"
        ),

        "merge_output_format": "mp4",

        "noplaylist": True,

        "restrictfilenames": True,

        "writethumbnail": False,

        "writeinfojson": False,

        "writesubtitles": False,

        "writeautomaticsub": False,

        "retries": 5,

        "fragment_retries": 5,

        "file_access_retries": 5,

        "socket_timeout": 30,

        "continuedl": True,

        "overwrites": True,

        "concurrent_fragment_downloads": 4,

        "max_filesize": MAX_FILE_SIZE,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),

            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    # ========================================================
    # INSTAGRAM COOKIES
    # ========================================================

    if is_instagram:

        cookies_file = Path(
            "cookies.txt"
        )

        if cookies_file.exists():

            ydl_opts["cookiefile"] = (
                str(cookies_file)
            )

            logger.info(
                "Instagram cookies enabled."
            )

        else:

            logger.info(
                "Instagram cookies.txt not found."
            )

    try:

        logger.info(
            "Downloading: %s",
            url,
        )

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True,
            )

            if not info:

                return (
                    None,
                    "yt-dlp could not extract the video.",
                )

        downloaded_file = (
            find_downloaded_video(temp_dir)
        )

        # ====================================================
        # IMPORTANT:
        # If no VIDEO was found, don't send the image.
        # ====================================================

        if not downloaded_file:

            return (
                None,
                "No video was found at this URL. "
                "The post may contain only an image, "
                "or Instagram may have restricted the video."
            )

        logger.info(
            "Video downloaded: %s",
            downloaded_file,
        )

        # ====================================================
        # Convert everything to MP4/H264/AAC
        # ====================================================

        final_file, conversion_error = (
            convert_to_mp4(
                downloaded_file,
                temp_dir,
            )
        )

        if not final_file:

            return (
                None,
                conversion_error
                or "Video conversion failed.",
            )

        logger.info(
            "Final MP4: %s",
            final_file,
        )

        return final_file, None

    except yt_dlp.utils.DownloadError as error:

        error_text = str(error)

        logger.error(
            "yt-dlp error: %s",
            error_text,
        )

        return None, error_text

    except Exception as error:

        logger.exception(
            "Download error"
        )

        return (
            None,
            f"{type(error).__name__}: {error}",
        )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(
        "👋 Welcome to Social Video Downloader!\n\n"
        "📥 Send me a video URL.\n\n"
        "Supported:\n"
        "📸 Instagram\n"
        "▶️ YouTube\n"
        "🎬 YouTube Shorts\n"
        "🎵 TikTok\n"
        "📘 Facebook\n\n"
        "Videos are converted to "
        "iPhone-compatible MP4."
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    await update.message.reply_text(
        "📥 Send a video URL from:\n\n"
        "• Instagram\n"
        "• YouTube\n"
        "• YouTube Shorts\n"
        "• TikTok\n"
        "• Facebook\n\n"
        "The bot will download and send "
        "the video as MP4."
    )


# ============================================================
# DOWNLOAD HANDLER
# ============================================================

async def download_video_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    if not update.message.text:
        return

    url = extract_url(
        update.message.text
    )

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid video URL."
        )

        return

    if not is_supported_url(url):

        await update.message.reply_text(
            "❌ Unsupported URL.\n\n"
            "Supported:\n"
            "📸 Instagram\n"
            "▶️ YouTube\n"
            "🎬 YouTube Shorts\n"
            "🎵 TikTok\n"
            "📘 Facebook"
        )

        return

    status = await update.message.reply_text(
        "⏳ Downloading video..."
    )

    try:

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        with tempfile.TemporaryDirectory(
            prefix="social_video_"
        ) as temp_dir:

            downloaded_file, error = (
                await asyncio.to_thread(
                    download_video,
                    url,
                    temp_dir,
                )
            )

            if not downloaded_file:

                await status.edit_text(
                    "❌ I couldn't download this video.\n\n"
                    f"{(error or 'Unknown error')[:2500]}"
                )

                return

            # =================================================
            # FILE SIZE
            # =================================================

            file_size = (
                downloaded_file.stat().st_size
            )

            logger.info(
                "Final video size: %.2f MB",
                file_size / (1024 * 1024),
            )

            if file_size > MAX_FILE_SIZE:

                await status.edit_text(
                    "❌ Video is too large.\n\n"
                    f"Maximum: "
                    f"{MAX_FILE_SIZE // (1024 * 1024)} MB."
                )

                return

            # =================================================
            # SEND AS VIDEO
            # =================================================

            await status.edit_text(
                "📤 Video ready!\n"
                "Sending to Telegram..."
            )

            try:

                with downloaded_file.open(
                    "rb"
                ) as video_file:

                    await update.message.reply_video(
                        video=video_file,

                        caption=(
                            "✅ Downloaded successfully\n"
                            "🎬 MP4 • H.264 • AAC"
                        ),

                        supports_streaming=True,

                        read_timeout=180,

                        write_timeout=180,

                        connect_timeout=30,

                        pool_timeout=30,
                    )

            except Exception as upload_error:

                logger.exception(
                    "Telegram upload failed"
                )

                await status.edit_text(
                    "❌ Video was downloaded, "
                    "but Telegram could not send it.\n\n"
                    f"{type(upload_error).__name__}: "
                    f"{upload_error}"
                )

                return

            try:
                await status.delete()
            except Exception:
                pass

    except Exception as error:

        logger.exception(
            "Handler error"
        )

        try:

            await status.edit_text(
                "❌ Something went wrong.\n\n"
                f"{type(error).__name__}: "
                f"{error}"
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

    logger.error(
        "Telegram bot error: %s",
        context.error,
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    logger.info(
        "Starting Social Video Downloader Bot..."
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
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
            filters.TEXT & ~filters.COMMAND,
            download_video_handler,
        )
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "🤖 Social Video Downloader Bot is running...",
        flush=True,
    )

    print(
        "Supported: Instagram, YouTube, "
        "YouTube Shorts, TikTok, Facebook",
        flush=True,
    )

    application.run_polling(
        drop_pending_updates=True,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()