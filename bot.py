import os
import re
import asyncio
import logging
import tempfile
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

# Keep some margin below Telegram's upload limit.
MAX_FILE_SIZE = 45 * 1024 * 1024


SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)


# ============================================================
# URL CHECK
# ============================================================

def is_supported_url(url: str) -> bool:
    """Check whether the URL belongs to a supported platform."""

    url_lower = url.lower()

    return any(
        host in url_lower
        for host in SUPPORTED_HOSTS
    )


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

    # Remove common punctuation accidentally included after URLs.
    url = url.rstrip(").,]}>'\"")

    return url


# ============================================================
# FIND DOWNLOADED FILE
# ============================================================

def find_downloaded_file(temp_dir: str) -> Path | None:
    """Find the downloaded media file."""

    directory = Path(temp_dir)

    if not directory.exists():
        return None

    files = [
        file
        for file in directory.iterdir()
        if file.is_file()
        and not file.name.endswith(
            (".part", ".ytdl", ".tmp")
        )
    ]

    if not files:
        return None

    # Prefer MP4.
    mp4_files = [
        file
        for file in files
        if file.suffix.lower() == ".mp4"
    ]

    if mp4_files:
        return max(
            mp4_files,
            key=lambda file: file.stat().st_size,
        )

    return max(
        files,
        key=lambda file: file.stat().st_size,
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
        (downloaded_file, error_message)
    """

    output_template = str(
        Path(temp_dir) / "download_%(id)s.%(ext)s"
    )

    is_instagram = "instagram.com" in url.lower()

    ydl_opts = {
        "outtmpl": output_template,

        # Prefer MP4 and keep quality reasonable.
        "format": (
            "bestvideo[ext=mp4][height<=720]+"
            "bestaudio[ext=m4a]/"
            "best[ext=mp4][height<=720]/"
            "best[height<=720]/"
            "best"
        ),

        "merge_output_format": "mp4",

        # One URL at a time.
        "noplaylist": True,

        # File names.
        "restrictfilenames": True,

        # Network retries.
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 5,

        # Network timeout.
        "socket_timeout": 30,

        # Continue interrupted downloads.
        "continuedl": True,
        "overwrites": True,

        # Download fragments concurrently.
        "concurrent_fragment_downloads": 4,

        # Don't download files larger than our limit.
        "max_filesize": MAX_FILE_SIZE,

        # Browser-like headers.
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

    # --------------------------------------------------------
    # Optional Instagram cookies
    # --------------------------------------------------------
    #
    # If cookies.txt exists in the Railway project,
    # yt-dlp can use it for Instagram authentication.
    #
    if is_instagram:

        cookies_file = Path("cookies.txt")

        if cookies_file.exists():
            ydl_opts["cookiefile"] = str(cookies_file)

            logger.info(
                "Instagram cookies.txt found. "
                "Using authenticated session."
            )
        else:
            logger.info(
                "No Instagram cookies.txt found. "
                "Trying public access."
            )

    try:

        logger.info(
            "Starting download: %s",
            url,
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True,
            )

            if not info:
                return (
                    None,
                    "yt-dlp could not extract video information.",
                )

        downloaded_file = find_downloaded_file(
            temp_dir
        )

        if not downloaded_file:
            return (
                None,
                "No downloadable video file was created.",
            )

        logger.info(
            "Download completed: %s",
            downloaded_file,
        )

        return downloaded_file, None

    except yt_dlp.utils.DownloadError as error:

        error_text = str(error)

        logger.error(
            "yt-dlp error: %s",
            error_text,
        )

        return None, error_text

    except Exception as error:

        logger.exception(
            "Unexpected download error"
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
        "📥 Send me a video URL and I'll try to download it.\n\n"
        "Supported platforms:\n"
        "📸 Instagram\n"
        "▶️ YouTube\n"
        "🎬 YouTube Shorts\n"
        "🎵 TikTok\n"
        "📘 Facebook\n\n"
        "⚠️ Private, restricted, unavailable or "
        "DRM-protected content may not work.\n\n"
        "Use /help for more information."
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
        "📥 Send a public video URL from:\n\n"
        "• Instagram\n"
        "• YouTube\n"
        "• YouTube Shorts\n"
        "• TikTok\n"
        "• Facebook\n\n"
        "The bot downloads the video and sends it "
        "back to you.\n\n"
        "⚠️ Private/restricted videos may require "
        "authentication and may not be available."
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

    text = update.message.text.strip()

    # --------------------------------------------------------
    # Extract URL
    # --------------------------------------------------------

    url = extract_url(text)

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid video URL."
        )

        return

    # --------------------------------------------------------
    # Check supported platform
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
            # Run yt-dlp in a worker thread so the Telegram
            # bot remains responsive.
            # ------------------------------------------------

            downloaded_file, error = await asyncio.to_thread(
                download_video,
                url,
                temp_dir,
            )

            # ------------------------------------------------
            # Download failed
            # ------------------------------------------------

            if not downloaded_file:

                error_text = (
                    error or "Unknown error"
                )

                # Telegram message length protection.
                error_text = error_text[:2500]

                await status.edit_text(
                    "❌ I couldn't download this video.\n\n"
                    f"Technical reason:\n"
                    f"{error_text}\n\n"
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
            # Check file size
            # ------------------------------------------------

            file_size = downloaded_file.stat().st_size

            logger.info(
                "Downloaded file size: %.2f MB",
                file_size / (1024 * 1024),
            )

            if file_size > MAX_FILE_SIZE:

                await status.edit_text(
                    "❌ The downloaded video is too large.\n\n"
                    f"Maximum allowed by this bot: "
                    f"{MAX_FILE_SIZE // (1024 * 1024)} MB."
                )

                return

            # ------------------------------------------------
            # Send video
            # ------------------------------------------------

            await status.edit_text(
                "📤 Download complete!\n"
                "Sending video..."
            )

            try:

                with downloaded_file.open(
                    "rb"
                ) as video_file:

                    await update.message.reply_video(
                        video=video_file,
                        caption="✅ Downloaded successfully",
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

            except Exception as upload_error:

                logger.exception(
                    "Telegram upload failed"
                )

                await status.edit_text(
                    "❌ Video downloaded, but Telegram "
                    "couldn't upload it.\n\n"
                    f"Error: {type(upload_error).__name__}: "
                    f"{upload_error}"
                )

                return

            # ------------------------------------------------
            # Delete status
            # ------------------------------------------------

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
                f"Error: {type(error).__name__}: "
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
    # Check Telegram token
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
    # Create Telegram application
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
    # URL/message handler
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
    # Startup message
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
    # Start Telegram polling
    # --------------------------------------------------------

    application.run_polling(
        drop_pending_updates=True,
    )


# ============================================================
# START PROGRAM
# ============================================================

if __name__ == "__main__":
    main()