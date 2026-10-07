import os
import logging
import tempfile
import re
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Keep below Telegram's upload limit used by this bot.
MAX_FILE_SIZE = 45 * 1024 * 1024

SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)

def is_supported_url(url: str) -> bool:
    """Check whether the URL belongs to one of the supported platforms."""
    url_lower = url.lower()
    return any(host in url_lower for host in SUPPORTED_HOSTS)

def find_downloaded_file(temp_dir: str) -> Path | None:
    """Find the final downloaded media file."""
    files = [
        p for p in Path(temp_dir).iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]

    if not files:
        return None

    # Prefer MP4 because Telegram handles it well as a video.
    mp4_files = [p for p in files if p.suffix.lower() == ".mp4"]
    if mp4_files:
        return max(mp4_files, key=lambda p: p.stat().st_size)

    return max(files, key=lambda p: p.stat().st_size)

def download_video(url: str, temp_dir: str) -> tuple[Path | None, str | None]:
    """
    Download a single public video using yt-dlp.

    Returns:
        (file_path, error_message)
    """

    output_template = str(
        Path(temp_dir) / "download_%(id)s.%(ext)s"
    )

    ydl_opts = {
        "outtmpl": output_template,

        # Prefer MP4 <= 720p. If separate video/audio streams are needed,
        # ffmpeg will merge them into MP4.
        "format": (
            "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/"
            "best[ext=mp4][height<=720]/"
            "best[height<=720]/"
            "best"
        ),

        "merge_output_format": "mp4",

        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,

        # Helps yt-dlp work better with current YouTube extraction.
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"]
            }
        },

        # Don't download huge files.
        "max_filesize": MAX_FILE_SIZE,

        # Retry transient network failures.
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 3,

        # Avoid leaving partial files behind.
        "continuedl": True,
        "overwrites": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        downloaded_file = find_downloaded_file(temp_dir)

        if not downloaded_file:
            return None, "No downloadable video file was created."

        return downloaded_file, None

    except yt_dlp.utils.DownloadError as error:
        return None, str(error)

    except Exception as error:
        logging.exception("Unexpected download error")
        return None, f"{type(error).__name__}: {error}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Social Video Downloader!\n\n"
        "Supported platforms:\n"
        "📸 Instagram\n"
        "▶️ YouTube\n"
        "🎬 YouTube Shorts\n"
        "🎵 TikTok\n"
        "📘 Facebook\n\n"
        "Send me a public video URL and I'll try to download it.\n\n"
        "Use /help for more information."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 Send a public video URL from:\n\n"
        "• Instagram\n"
        "• YouTube\n"
        "• YouTube Shorts\n"
        "• TikTok\n"
        "• Facebook\n\n"
        "⚠️ Private, restricted, DRM-protected, or unavailable content "
        "is not supported."
    )

async def download_video_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message or not update.message.text:
        return

    # Extract the first URL from the message.
    match = re.search(r"https?://\S+", update.message.text.strip())

    if not match:
        await update.message.reply_text(
            "❌ Please send a valid video URL."
        )
        return

    url = match.group(0).rstrip(").,]}>'\"")

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

    status = await update.message.reply_text(
        "⏳ Downloading video...\nPlease wait."
    )

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.UPLOAD_VIDEO,
    )

    try:
        with tempfile.TemporaryDirectory(prefix="social_video_") as temp_dir:

            downloaded_file, error = await __import__("asyncio").to_thread(
                download_video,
                url,
                temp_dir,
            )

            if not downloaded_file:
                error_text = (error or "Unknown error")[:1500]

                await status.edit_text(
                    "❌ I couldn't download this video.\n\n"
                    f"Technical reason:\n{error_text}\n\n"
                    "Possible reasons:\n"
                    "• Video is private/restricted\n"
                    "• Video is unavailable\n"
                    "• Platform blocked the request\n"
                    "• Login/cookies are required\n"
                    "• Video is too large\n"
                    "• yt-dlp or ffmpeg needs updating"
                )
                return

            file_size = downloaded_file.stat().st_size

            if file_size > MAX_FILE_SIZE:
                await status.edit_text(
                    "❌ The downloaded video is too large.\n"
                    f"Maximum allowed by this bot: "
                    f"{MAX_FILE_SIZE // (1024 * 1024)} MB."
                )
                return

            await status.edit_text(
                "📤 Download complete!\nSending video..."
            )

            with downloaded_file.open("rb") as video:
                await update.message.reply_video(
                    video=video,
                    caption="✅ Downloaded successfully",
                    supports_streaming=True,
                )

            await status.delete()

    except Exception as error:
        logging.exception("Download error: %s", error)

        try:
            await status.edit_text(
                "❌ Download/upload failed.\n\n"
                f"Error: {type(error).__name__}: {error}"
            )
        except Exception:
            pass

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing.\n"
            "Set your BotFather token before starting the bot."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            download_video_handler,
        )
    )

    print("🤖 Social Video Downloader Bot is running...")
    print("Supported: Instagram, YouTube, YouTube Shorts, TikTok, Facebook")

    application.run_polling()

if __name__ == "__main__":
    main()