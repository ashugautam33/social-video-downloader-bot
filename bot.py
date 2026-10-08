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
MAX_DIM = 720

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
# URL
# ============================================================

def extract_url(text):
    match = re.search(r"https?://[^\s]+", text or "", re.I)

    if not match:
        return None

    return match.group(0).rstrip(").,]}>'\"")


def is_supported(url):
    url = url.lower()
    return any(host in url for host in SUPPORTED_HOSTS)


def is_instagram(url):
    return "instagram.com" in url.lower()


# ============================================================
# INSTAGRAM COOKIES
# ============================================================

def get_cookies(temp_dir):
    if INSTAGRAM_COOKIES_B64:

        try:
            cookie_file = Path(temp_dir) / "instagram_cookies.txt"

            cookie_file.write_bytes(
                base64.b64decode(
                    INSTAGRAM_COOKIES_B64,
                    validate=True
                )
            )

            if cookie_file.stat().st_size > 0:
                return str(cookie_file)

        except Exception as e:
            print("Cookie decode error:", e)

    local = Path("cookies.txt")

    if local.exists() and local.stat().st_size > 0:
        return str(local)

    return None


# ============================================================
# FFMPEG
# ============================================================

def find_ffmpeg():

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10
        )

        if result.returncode == 0:
            return "ffmpeg"

    except Exception:
        pass

    return None


# ============================================================
# VIDEO INFORMATION
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

        result = subprocess.run(
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

        if result.returncode == 0:

            info["video"] = True

            for line in result.stdout.splitlines():

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)

                if key == "codec_name":
                    info["codec"] = value

                elif key == "width":
                    try:
                        info["width"] = int(value)
                    except:
                        pass

                elif key == "height":
                    try:
                        info["height"] = int(value)
                    except:
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

        info["audio"] = bool(audio.stdout.strip())

    except Exception as e:

        print("Media probe error:", e)

    return info


# ============================================================
# FIND VIDEO
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
        except:
            pass

    if not files:
        return None

    return max(
        files,
        key=lambda x: x.stat().st_size
    )


# ============================================================
# CONVERT TO MP4
# ============================================================

def convert_video(source, temp_dir):

    ffmpeg = find_ffmpeg()

    if not ffmpeg:
        return None, "FFmpeg is not installed."

    info = get_media_info(source)

    if not info["video"]:
        return None, "Downloaded file contains no video."

    output = Path(temp_dir) / "final.mp4"

    # --------------------------------------------------------
    # Already compatible H264 MP4
    # --------------------------------------------------------

    if (
        source.suffix.lower() == ".mp4"
        and info["codec"] == "h264"
        and info["width"]
        and info["height"]
        and max(info["width"], info["height"]) <= MAX_DIM
    ):

        command = [
            ffmpeg,
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
    # Convert VP9 / AV1 / large video
    # --------------------------------------------------------

    else:

        video_filter = (
            "scale="
            "w='min(720,iw)':"
            "h='min(720,ih)':"
            "force_original_aspect_ratio=decrease"
        )

        command = [
            ffmpeg,
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
            "26",

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

    print("FFmpeg:", " ".join(command))

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:

            print(result.stderr[-5000:])

            return (
                None,
                "FFmpeg conversion failed.\n\n"
                + result.stderr[-2500:]
            )

        if not output.exists():
            return None, "FFmpeg did not create the MP4."

        if output.stat().st_size <= 0:
            return None, "Final MP4 is empty."

        return output, None

    except subprocess.TimeoutExpired:

        return None, "FFmpeg conversion timed out."

    except Exception as e:

        return None, f"{type(e).__name__}: {e}"


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def get_ydl_options(temp_dir, cookie=None):

    options = {

        "outtmpl": str(
            Path(temp_dir) /
            "video_%(id)s_%(autonumber)s.%(ext)s"
        ),

        # IMPORTANT:
        # Try <=720 first.
        # If unavailable, fall back to ANY available format.
        "format": (
            "bestvideo[height<=720]+bestaudio/"
            "best[height<=720]/"
            "bestvideo+bestaudio/"
            "best"
        ),

        "merge_output_format": "mp4",

        "noplaylist": True,

        "max_filesize": MAX_SIZE,

        "retries": 5,

        "fragment_retries": 5,

        "file_access_retries": 5,

        "socket_timeout": 30,

        "continuedl": True,

        "overwrites": True,

        "concurrent_fragment_downloads": 4,

        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },

        "quiet": True,

        "no_warnings": False,
    }

    if cookie:
        options["cookiefile"] = cookie

    return options


# ============================================================
# DOWNLOAD
# ============================================================

def download_video(url, temp_dir):

    errors = []

    attempts = [None]

    # Instagram gets a second authenticated attempt
    if is_instagram(url):

        cookie = get_cookies(temp_dir)

        if cookie:
            attempts.append(cookie)

    for cookie in attempts:

        # Remove previous downloaded files
        for file in Path(temp_dir).iterdir():

            if (
                file.is_file()
                and file.suffix.lower() in VIDEO_EXTENSIONS
            ):

                try:
                    file.unlink()
                except:
                    pass

        options = get_ydl_options(
            temp_dir,
            cookie
        )

        if is_instagram(url):

            options["http_headers"].update({
                "Referer": "https://www.instagram.com/",
                "Origin": "https://www.instagram.com/",
            })

        try:

            print(
                "Downloading:",
                "Instagram authenticated"
                if cookie
                else "Public extraction"
            )

            with yt_dlp.YoutubeDL(options) as ydl:

                ydl.extract_info(
                    url,
                    download=True
                )

            source = find_video(temp_dir)

            if source:

                print("Downloaded:", source)

                return convert_video(
                    source,
                    temp_dir
                )

            errors.append(
                "yt-dlp did not create a video file."
            )

        except yt_dlp.utils.DownloadError as e:

            print("yt-dlp error:", e)

            errors.append(str(e))

        except Exception as e:

            print("Download error:", e)

            errors.append(
                f"{type(e).__name__}: {e}"
            )

    error = "\n\n".join(errors)

    # Friendly Instagram errors
    low = error.lower()

    if "no video formats found" in low:
        error = (
            "Instagram did not provide a downloadable "
            "video format."
        )

    elif "requested format is not available" in low:
        error = (
            "Instagram did not provide the requested "
            "video format. The bot tried fallback formats."
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

        error = (
            "Instagram requires authentication. "
            "Your Instagram cookies may be expired."
        )

    elif "private" in low:

        error = (
            "This Instagram video is private or "
            "your account cannot access it."
        )

    elif (
        "403" in low
        or "forbidden" in low
        or "access denied" in low
    ):

        error = (
            "The platform denied access to this video."
        )

    elif (
        "429" in low
        or "rate limit" in low
        or "too many requests" in low
    ):

        error = (
            "The platform is rate-limiting the bot. "
            "Please try again later."
        )

    return None, error[:3500]


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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

        "🎬 Output: H.264 MP4\n"
        "📺 Maximum: 720p\n"
        "📦 Maximum: 45 MB"
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await start(update, context)


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
                    f"Size: {size / 1024 / 1024:.1f} MB\n"
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
                            "✅ Downloaded successfully\n"
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
            except:
                pass

    except Exception as e:

        try:

            await status.edit_text(
                "❌ Something went wrong.\n\n"
                f"{type(e).__name__}: {e}"
            )

        except:
            pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    print(
        "Telegram error:",
        context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing.\n"
            "Add BOT_TOKEN in Railway Variables."
        )

    print(
        "🤖 Social Video Downloader starting..."
    )

    print(
        "yt-dlp:",
        yt_dlp.version.__version__
    )

    print(
        "FFmpeg:",
        "Available"
        if find_ffmpeg()
        else "NOT FOUND"
    )

    print(
        "Instagram cookies:",
        "Configured"
        if INSTAGRAM_COOKIES_B64
        else "Not configured"
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
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    app.add_error_handler(
        error_handler
    )

    print(
        "🤖 Bot is running...",
        flush=True
    )

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
