import os
import logging
import tempfile
from pathlib import Path

import yt_dlp
from telegram import Update
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

MAX_FILE_SIZE = 45 * 1024 * 1024


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Social Video Downloader!\n\n"
        "Send me a public video URL and I'll try to download it for you.\n\n"
        "Use /help for more information."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 Send a publicly accessible video URL.\n\n"
        "The bot will download the video and send it back to you.\n\n"
        "⚠️ Private or restricted content is not supported."
    )


async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text(
            "❌ Please send a valid video URL."
        )
        return

    status = await update.message.reply_text(
        "⏳ Downloading video...\nPlease wait."
    )

    try:
        with tempfile.TemporaryDirectory() as temp_dir:

            output_template = str(
                Path(temp_dir) / "%(title).80s.%(ext)s"
            )

            ydl_opts = {
                "outtmpl": output_template,
                "format": (
                    "best[ext=mp4][height<=720]/"
                    "best[height<=720]/"
                    "best"
                ),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "restrictfilenames": True,
                "max_filesize": MAX_FILE_SIZE,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                downloaded_file = Path(
                    ydl.prepare_filename(info)
                )

                if not downloaded_file.exists():
                    files = list(Path(temp_dir).glob("*"))
                    if not files:
                        raise FileNotFoundError(
                            "Downloaded file was not found."
                        )
                    downloaded_file = files[0]

            file_size = downloaded_file.stat().st_size

            if file_size > MAX_FILE_SIZE:
                await status.edit_text(
                    "❌ The video is too large for Telegram."
                )
                return

            await status.edit_text(
                "📤 Download complete!\nSending video..."
            )

            with open(downloaded_file, "rb") as video:
                await update.message.reply_video(
                    video=video,
                    caption="✅ Downloaded successfully",
                    supports_streaming=True,
                )

            await status.delete()

    except Exception as error:
        logging.exception("Download error: %s", error)

        await status.edit_text(
            "❌ I couldn't download this video.\n\n"
            "Possible reasons:\n"
            "• The URL is unsupported\n"
            "• The video is private/restricted\n"
            "• The video is unavailable\n"
            "• The video is too large\n\n"
            "Please try another public video URL."
        )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
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
            download_video,
        )
    )

    print("🤖 Social Video Downloader Bot is running...")

    application.run_polling()


if __name__ == "__main__":
    main()