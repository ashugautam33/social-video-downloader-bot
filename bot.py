import os
import re
import asyncio
import base64
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
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
INSTAGRAM_COOKIES_B64 = os.getenv("INSTAGRAM_COOKIES_B64")

MAX_SIZE = 45 * 1024 * 1024
MAX_HEIGHT = 720

SUPPORTED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
)

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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


# ============================================================
# URL HELPERS
# ============================================================

def extract_url(text):
    match = re.search(r"https?://[^\s]+", text or "", re.I)

    if not match:
        return None

    return match.group(0).rstrip(").,]}>'\"")


def is_supported(url):
    return any(
        host in url.lower()
        for host in SUPPORTED_HOSTS
    )


def is_instagram(url):
    return "instagram.com" in url.lower()


# ============================================================
# INSTAGRAM COOKIES
# ============================================================

def get_cookie_file(temp_dir):

    # Railway Base64 cookies
    if INSTAGRAM_COOKIES_B64:

        try:
            cookie_file = (
                Path(temp_dir) /
                "instagram_cookies.txt"
            )

            cookie_file.write_bytes(
                base64.b64decode(
                    INSTAGRAM_COOKIES_B64,
                    validate=True,
                )
            )

            if cookie_file.stat().st_size > 0:
                return str(cookie_file)

        except Exception as e:
            print("Cookie error:", e)

    # Local cookies.txt
    local = Path("cookies.txt")

    if local.exists() and local.stat().st_size > 0:
        return str(local)

    return None


# ============================================================
# FFMPEG
# ============================================================

def ffmpeg_available():

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )

        return result.returncode == 0

    except Exception:
        return False


# ============================================================
# MEDIA INFO
# ============================================================

def get_media_info(file):

    info = {
        "video": False,
        "audio": False,
        "codec": "",
        "width": 0,
        "height": 0,
    }

    try:

        video = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height",
                "-of",
                "default=noprint_wrappers=1",
                str(file),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if video.returncode == 0:

            info["video"] = True

            for line in video.stdout.splitlines():

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)

                if key == "codec_name":
                    info["codec"] = value

                elif key == "width":
                    try:
                        info["width"] = int(value)
                    except ValueError:
                        pass

                elif key == "height":
                    try:
                        info["height"] = int(value)
                    except ValueError:
                        pass

        audio = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "csv=p=0",
                str(file),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        info["audio"] = bool(
            audio.stdout.strip()
        )

    except Exception as e:

        print("ffprobe error:", e)

    return info


# ============================================================
# FIND DOWNLOADED FILE
# ============================================================

def find_video(temp_dir):

    files = []

    for file in Path(temp_dir).iterdir():

        if not file.is_file():
            continue

        if file.suffix.lower() not in VIDEO_EXTENSIONS:
            continue

        try:

            if file.stat().st_size > 0:
                files.append(file)

        except OSError:
            pass

    if not files:
        return None

    # Prefer MP4
    mp4 = [
        f for f in files
        if f.suffix.lower() == ".mp4"
    ]

    if mp4:
        return max(
            mp4,
            key=lambda f: f.stat().st_size
        )

    return max(
        files,
        key=lambda f: f.stat().st_size
    )


# ============================================================
# FAST VIDEO CONVERSION
# ============================================================

def convert_video(source, temp_dir):

    if not ffmpeg_available():
        return None, "FFmpeg is not installed."

    info = get_media_info(source)

    if not info["video"]:
        return None, "Downloaded file has no video stream."

    output = Path(temp_dir) / "final.mp4"

    # --------------------------------------------------------
    # Already compatible
    # --------------------------------------------------------

    if (
        source.suffix.lower() == ".mp4"
        and info["codec"] == "h264"
        and info["width"]
        and info["height"]
        and max(
            info["width"],
            info["height"]
        ) <= MAX_HEIGHT
    ):

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(source),

            "-map",
            "0:v:0",

            "-c:v",
            "copy",
        ]

        if info["audio"]:

            command += [
                "-map",
                "0:a:0?",
                "-c:a",
                "copy",
            ]

        command += [
            "-movflags",
            "+faststart",
            str(output),
        ]

    # --------------------------------------------------------
    # Convert
    # --------------------------------------------------------

    else:

        video_filter = (
            "scale="
            "w='min(720,iw)':"
            "h='min(720,ih)':"
            "force_original_aspect_ratio=decrease"
        )

        command = [
            "ffmpeg",
            "-y",

            "-i",
            str(source),

            "-map",
            "0:v:0",

            "-vf",
            video_filter,

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
        ]

        if info["audio"]:

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
            str(output),
        ]

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=240,
        )

        if result.returncode != 0:

            print(
                "FFmpeg error:",
                result.stderr[-3000:],
            )

            return (
                None,
                "Video conversion failed."
            )

        if not output.exists():
            return (
                None,
                "FFmpeg did not create the MP4."
            )

        if output.stat().st_size <= 0:
            return (
                None,
                "Final MP4 is empty."
            )

        return output, None

    except subprocess.TimeoutExpired:

        return (
            None,
            "Video conversion timed out."
        )

    except Exception as e:

        return (
            None,
            f"{type(e).__name__}: {e}"
        )


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def ydl_options(temp_dir, cookie=None):

    options = {

        "outtmpl": str(
            Path(temp_dir) /
            "video_%(id)s.%(ext)s"
        ),

        # FAST:
        # Prefer a single combined format.
        # Fall back to other available formats.
        "format": (
            "best[height<=720]/"
            "bestvideo[height<=720]+bestaudio/"
            "best"
        ),

        "merge_output_format": "mp4",

        "noplaylist": True,

        "max_filesize": MAX_SIZE,

        # Small retry count = faster failure
        "retries": 1,
        "fragment_retries": 1,
        "file_access_retries": 1,

        "socket_timeout": 10,

        "continuedl": True,
        "overwrites": True,

        "concurrent_fragment_downloads": 4,

        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },

        "quiet": True,
        "no_warnings": True,
    }

    if cookie:
        options["cookiefile"] = cookie

    return options


# ============================================================
# DOWNLOAD
# ============================================================

def download_video(url, temp_dir):

    attempts = []

    # Public attempt first
    attempt_cookies = [None]

    # Instagram gets authenticated fallback
    if is_instagram(url):

        cookie = get_cookie_file(temp_dir)

        if cookie:
            attempt_cookies.append(cookie)

    for cookie in attempt_cookies:

        # Remove old files before retry
        for file in Path(temp_dir).iterdir():

            if (
                file.is_file()
                and file.suffix.lower()
                in VIDEO_EXTENSIONS
            ):

                try:
                    file.unlink()
                except OSError:
                    pass

        options = ydl_options(
            temp_dir,
            cookie,
        )

        if is_instagram(url):

            options["http_headers"].update({
                "Referer":
                    "https://www.instagram.com/",
                "Origin":
                    "https://www.instagram.com/",
            })

        try:

            print(
                "Download attempt:",
                "authenticated"
                if cookie
                else "public",
                flush=True,
            )

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                ydl.extract_info(
                    url,
                    download=True,
                )

            source = find_video(temp_dir)

            if source:

                print(
                    "Downloaded:",
                    source,
                    flush=True,
                )

                return convert_video(
                    source,
                    temp_dir,
                )

            attempts.append(
                "No video file was created."
            )

        except yt_dlp.utils.DownloadError as e:

            error = str(e)

            print(
                "yt-dlp:",
                error,
                flush=True,
            )

            attempts.append(error)

        except Exception as e:

            error = (
                f"{type(e).__name__}: {e}"
            )

            print(error, flush=True)

            attempts.append(error)

    error = "\n\n".join(attempts)

    # Friendly errors
    low = error.lower()

    if (
        "requested format is not available"
        in low
    ):

        message = (
            "Instagram did not provide the "
            "requested format."
        )

    elif "no video formats found" in low:

        message = (
            "No downloadable video format "
            "was provided by Instagram."
        )

    elif any(
        x in low
        for x in (
            "login required",
            "authentication",
            "sign in",
            "cookies",
        )
    ):

        message = (
            "Instagram requires authentication. "
            "Your cookies may be expired."
        )

    elif "private" in low:

        message = (
            "This video is private or "
            "your account cannot access it."
        )

    elif (
        "403" in low
        or "forbidden" in low
        or "access denied" in low
    ):

        message = (
            "The platform denied access "
            "to this video."
        )

    elif (
        "429" in low
        or "rate limit" in low
    ):

        message = (
            "The platform is rate-limiting "
            "the bot. Try again later."
        )

    else:

        message = error[-3000:]

    return None, message


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
        "👋 Social Video Downloader\n\n"
        "📥 Send a video URL.\n\n"
        "Supported:\n"
        "📸 Instagram\n"
        "▶️ YouTube\n"
        "🎬 YouTube Shorts\n"
        "🎵 TikTok\n"
        "📘 Facebook\n\n"
        "⚡ Fast download\n"
        "🎬 H.264 MP4\n"
        "📺 Max 720p\n"
        "📦 Max 45 MB"
    )


# ============================================================
# /HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await start(update, context)


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    text = update.message.text

    if not text:
        return

    url = extract_url(text)

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid video URL."
        )

        return

    if not is_supported(url):

        await update.message.reply_text(
            "❌ Unsupported platform.\n\n"
            "Supported:\n"
            "Instagram\n"
            "YouTube\n"
            "YouTube Shorts\n"
            "TikTok\n"
            "Facebook"
        )

        return

    status = await update.message.reply_text(
        "⚡ Finding video..."
    )

    try:

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        with tempfile.TemporaryDirectory(
            prefix="video_"
        ) as temp_dir:

            # Download in background
            video, error = await asyncio.to_thread(
                download_video,
                url,
                temp_dir,
            )

            if not video:

                await status.edit_text(
                    "❌ Download failed.\n\n"
                    + (
                        error
                        or "Unknown error."
                    )
                )

                return

            size = video.stat().st_size

            if size > MAX_SIZE:

                await status.edit_text(
                    "❌ Video is too large.\n\n"
                    f"Size: "
                    f"{size / 1024 / 1024:.1f} MB\n"
                    "Maximum: 45 MB"
                )

                return

            await status.edit_text(
                "📤 Sending video..."
            )

            try:

                with video.open("rb") as file:

                    await update.message.reply_video(
                        video=file,

                        caption=(
                            "✅ Downloaded\n"
                            "🎬 MP4 • H.264"
                        ),

                        supports_streaming=True,

                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

            except Exception as e:

                await status.edit_text(
                    "❌ Telegram upload failed.\n\n"
                    f"{type(e).__name__}: {e}"
                )

                return

            try:
                await status.delete()
            except Exception:
                pass

    except Exception as e:

        print(
            "Handler error:",
            e,
            flush=True,
        )

        try:

            await status.edit_text(
                "❌ Something went wrong.\n\n"
                f"{type(e).__name__}: {e}"
            )

        except Exception:
            pass


# ============================================================
# TELEGRAM ERROR
# ============================================================

async def error_handler(
    update,
    context,
):

    print(
        "Telegram error:",
        context.error,
        flush=True,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing. "
            "Add it to Railway Variables."
        )

    print(
        "🤖 Starting bot...",
        flush=True,
    )

    print(
        "yt-dlp:",
        yt_dlp.version.__version__,
        flush=True,
    )

    print(
        "FFmpeg:",
        "OK" if ffmpeg_available()
        else "NOT FOUND",
        flush=True,
    )

    print(
        "Instagram cookies:",
        "YES" if INSTAGRAM_COOKIES_B64
        else "NO",
        flush=True,
    )

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_error_handler(
        error_handler
    )

    print(
        "🚀 Bot is running...",
        flush=True,
    )

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
