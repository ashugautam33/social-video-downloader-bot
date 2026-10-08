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

# IMPORTANT:
# Do not expose Telegram API URLs / bot token in Railway logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Maximum final video size.
# Keep some margin below Telegram's upload limit.
MAX_FILE_SIZE = 45 * 1024 * 1024

# Maximum video resolution.
MAX_HEIGHT = 720


# ============================================================
# SUPPORTED PLATFORMS
# ============================================================

SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)


# ============================================================
# VIDEO FILE EXTENSIONS
# ============================================================

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
# CHECK SUPPORTED URL
# ============================================================

def is_supported_url(url: str) -> bool:
    """Check if URL belongs to a supported platform."""

    url_lower = url.lower()

    return any(
        host in url_lower
        for host in SUPPORTED_HOSTS
    )


# ============================================================
# EXTRACT URL
# ============================================================

def extract_url(text: str) -> str | None:
    """Extract the first HTTP/HTTPS URL from a Telegram message."""

    match = re.search(
        r"https?://[^\s]+",
        text.strip(),
        re.IGNORECASE,
    )

    if not match:
        return None

    url = match.group(0)

    # Remove punctuation accidentally included after URL.
    url = url.rstrip(
        ").,]}>'\""
    )

    return url


# ============================================================
# FIND VIDEO FILE
# ============================================================

def find_downloaded_video(
    temp_dir: str,
) -> Path | None:
    """
    Find an actual video file.

    JPG/PNG/WebP thumbnails are deliberately ignored.
    """

    directory = Path(temp_dir)

    if not directory.exists():
        return None

    video_files = []

    for file in directory.iterdir():

        if not file.is_file():
            continue

        # Ignore temporary files.
        if file.name.endswith(
            (
                ".part",
                ".ytdl",
                ".tmp",
            )
        ):
            continue

        # Only accept actual video extensions.
        if file.suffix.lower() not in VIDEO_EXTENSIONS:
            continue

        try:

            if file.stat().st_size > 0:
                video_files.append(file)

        except OSError:
            continue

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
    """
    Convert downloaded video to:

    MP4
    H.264 video
    AAC audio
    yuv420p pixel format
    Fast-start MP4

    This improves compatibility with Telegram/iPhone Photos.
    """

    output_file = (
        Path(temp_dir)
        / "final_video.mp4"
    )

    command = [
        "ffmpeg",

        "-y",

        "-i",
        str(input_file),

        # Video codec
        "-c:v",
        "libx264",

        # Reasonable quality
        "-preset",
        "veryfast",

        "-crf",
        "23",

        # Audio codec
        "-c:a",
        "aac",

        "-b:a",
        "128k",

        # iPhone compatibility
        "-pix_fmt",
        "yuv420p",

        # Streaming / phone compatibility
        "-movflags",
        "+faststart",

        str(output_file),
    ]

    try:

        logger.info(
            "Converting video to MP4: %s",
            input_file.name,
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

        if output_file.stat().st_size <= 0:

            return (
                None,
                "The converted video file is empty.",
            )

        return (
            output_file,
            None,
        )

    except subprocess.TimeoutExpired:

        return (
            None,
            "Video conversion took too long.",
        )

    except FileNotFoundError:

        return (
            None,
            "FFmpeg is not installed in the Railway container.",
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
    """
    Download a video using yt-dlp.

    Returns:
        (video_file, error_message)
    """

    output_template = str(
        Path(temp_dir)
        / "download_%(id)s.%(ext)s"
    )

    is_instagram = (
        "instagram.com" in url.lower()
    )

    # --------------------------------------------------------
    # yt-dlp configuration
    # --------------------------------------------------------

    ydl_opts = {

        "outtmpl": output_template,

        # Prefer real video + audio.
        # Do not intentionally select image thumbnails.
        "format": (
            "bestvideo[ext=mp4][height<=720]+"
            "bestaudio[ext=m4a]/"
            "best[ext=mp4][height<=720]/"
            "bestvideo[height<=720]+"
            "bestaudio/"
            "best[height<=720]/"
            "best"
        ),

        "merge_output_format": "mp4",

        # One URL at a time.
        "noplaylist": True,

        # Safe filenames.
        "restrictfilenames": True,

        # Do not download thumbnails.
        "writethumbnail": False,

        # Do not download metadata files.
        "writeinfojson": False,

        # Do not download subtitles.
        "writesubtitles": False,
        "writeautomaticsub": False,

        # Network retries.
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 5,

        # Network timeout.
        "socket_timeout": 30,

        # Resume downloads.
        "continuedl": True,

        # Replace existing temporary files.
        "overwrites": True,

        # Concurrent fragments.
        "concurrent_fragment_downloads": 4,

        # Maximum source size.
        "max_filesize": MAX_FILE_SIZE,

        # Browser-like headers.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Safari/537.36"
            ),

            "Accept-Language": (
                "en-US,en;q=0.9"
            ),
        },
    }

    # --------------------------------------------------------
    # INSTAGRAM COOKIES
    # --------------------------------------------------------

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
                "Instagram cookies.txt not found. "
                "Trying public access."
            )

    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    try:

        logger.info(
            "Starting video download."
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
                    "yt-dlp could not extract video information.",
                )

        # ----------------------------------------------------
        # Find actual video.
        # ----------------------------------------------------

        downloaded_file = (
            find_downloaded_video(
                temp_dir
            )
        )

        if not downloaded_file:

            return (
                None,
                "No video was found at this URL.\n\n"
                "The post may contain only an image, "
                "or the platform may have restricted "
                "the video."
            )

        logger.info(
            "Video downloaded: %s",
            downloaded_file.name,
        )

        # ----------------------------------------------------
        # Convert to iPhone-compatible MP4.
        # ----------------------------------------------------

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
            "Final MP4 created: %s",
            final_file.name,
        )

        return (
            final_file,
            None,
        )

    # --------------------------------------------------------
    # yt-dlp error
    # --------------------------------------------------------

    except yt_dlp.utils.DownloadError as error:

        error_text = str(error)

        logger.error(
            "yt-dlp error: %s",
            error_text,
        )

        return (
            None,
            error_text,
        )

    # --------------------------------------------------------
    # General error
    # --------------------------------------------------------

    except Exception as error:

        logger.exception(
            "Download error"
        )

        return (
            None,
            f"{type(error).__name__}: {error}",
        )


# ============================================================
# /START
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

        "Supported platforms:\n"
        "📸 Instagram\n"
        "▶️ YouTube\n"
        "🎬 YouTube Shorts\n"
        "🎵 TikTok\n"
        "📘 Facebook\n\n"

        "🎬 Videos are converted to "
        "iPhone-compatible MP4.\n\n"

        "⚠️ Private/restricted videos may not work."
    )


# ============================================================
# /HELP
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

        "The bot downloads the video, "
        "converts it to MP4 and sends it "
        "back to you.\n\n"

        "Maximum size: 45 MB."
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

    # --------------------------------------------------------
    # Extract URL
    # --------------------------------------------------------

    url = extract_url(
        update.message.text
    )

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid video URL."
        )

        return

    # --------------------------------------------------------
    # Check supported URL
    # --------------------------------------------------------

    if not is_supported_url(url):

        await update.message.reply_text(
            "❌ Unsupported URL.\n\n"

            "Supported platforms:\n"
            "📸 Instagram\n"
            "▶️ YouTube\n"
            "🎬 YouTube Shorts\n"
            "🎵 TikTok\n"
            "📘 Facebook"
        )

        return

    # --------------------------------------------------------
    # Status message
    # --------------------------------------------------------

    status = await update.message.reply_text(
        "⏳ Downloading video...\n\n"
        "Please wait."
    )

    try:

        # ----------------------------------------------------
        # Telegram upload indicator
        # ----------------------------------------------------

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        # ----------------------------------------------------
        # Temporary directory
        # ----------------------------------------------------

        with tempfile.TemporaryDirectory(
            prefix="social_video_"
        ) as temp_dir:

            # ------------------------------------------------
            # Run downloader in background thread.
            # ------------------------------------------------

            downloaded_file, error = (
                await asyncio.to_thread(
                    download_video,
                    url,
                    temp_dir,
                )
            )

            # ------------------------------------------------
            # Download failed
            # ------------------------------------------------

            if not downloaded_file:

                error_text = (
                    error
                    or "Unknown download error."
                )

                await status.edit_text(
                    "❌ I couldn't download this video.\n\n"
                    f"Technical reason:\n"
                    f"{error_text[:2500]}\n\n"

                    "Possible reasons:\n"
                    "• Video is private/restricted\n"
                    "• Video is unavailable\n"
                    "• Login/cookies are required\n"
                    "• Platform blocked the request\n"
                    "• Video is too large\n"
                    "• yt-dlp/FFmpeg problem"
                )

                return

            # ------------------------------------------------
            # Check final file size
            # ------------------------------------------------

            file_size = (
                downloaded_file.stat().st_size
            )

            file_size_mb = (
                file_size / (1024 * 1024)
            )

            logger.info(
                "Final video size: %.2f MB",
                file_size_mb,
            )

            if file_size > MAX_FILE_SIZE:

                await status.edit_text(
                    "❌ Video is too large.\n\n"
                    f"Size: {file_size_mb:.1f} MB\n"
                    f"Maximum: "
                    f"{MAX_FILE_SIZE // (1024 * 1024)} MB"
                )

                return

            # ------------------------------------------------
            # Sending
            # ------------------------------------------------

            await status.edit_text(
                "📤 Video ready!\n\n"
                "Sending to Telegram..."
            )

            try:

                # ------------------------------------------------
                # Send as VIDEO.
                # ------------------------------------------------

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
                    "Telegram upload failed."
                )

                await status.edit_text(
                    "❌ Video was downloaded, "
                    "but Telegram could not send it.\n\n"

                    f"Error: "
                    f"{type(upload_error).__name__}: "
                    f"{upload_error}"
                )

                return

            # ------------------------------------------------
            # Remove status message.
            # ------------------------------------------------

            try:

                await status.delete()

            except Exception:

                pass

    except Exception as error:

        logger.exception(
            "Handler error."
        )

        try:

            await status.edit_text(
                "❌ Something went wrong.\n\n"
                f"Error: "
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

    # --------------------------------------------------------
    # Check BOT_TOKEN
    # --------------------------------------------------------

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing.\n"
            "Add BOT_TOKEN in Railway Variables."
        )

    logger.info(
        "Starting Social Video Downloader Bot..."
    )

    # --------------------------------------------------------
    # Telegram application
    # --------------------------------------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # URL messages
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            download_video_handler,
        )
    )

    # --------------------------------------------------------
    # Error handler
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # Startup
    # --------------------------------------------------------

    print(
        "🤖 Social Video Downloader Bot is running...",
        flush=True,
    )

    print(
        "Supported: Instagram, YouTube, "
        "YouTube Shorts, TikTok, Facebook",
        flush=True,
    )

    # --------------------------------------------------------
    # Start polling
    # --------------------------------------------------------

    application.run_polling(
        drop_pending_updates=True,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()