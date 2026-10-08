import os
import re
import json
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
# URL
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

            if cookie_file.exists() and cookie_file.stat().st_size > 0:
                return str(cookie_file)

        except Exception as e:
            print(
                "Cookie error:",
                e,
                flush=True,
            )

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
            [
                "ffmpeg",
                "-version",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )

        return result.returncode == 0

    except Exception:
        return False


# ============================================================
# MEDIA PROBE
# ============================================================

def get_media_info(file):

    try:

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream="
                "index,"
                "codec_type,"
                "codec_name,"
                "width,"
                "height,"
                "pix_fmt",
                "-of",
                "json",
                str(file),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

        if result.returncode != 0:

            print(
                "FFprobe error:",
                result.stderr[-2000:],
                flush=True,
            )

            return None

        data = json.loads(result.stdout)

        streams = data.get("streams", [])

        video = None
        audio = None

        for stream in streams:

            if stream.get("codec_type") == "video":
                if video is None:
                    video = stream

            elif stream.get("codec_type") == "audio":
                if audio is None:
                    audio = stream

        if video is None:
            return None

        return {
            "codec": video.get(
                "codec_name",
                "",
            ),

            "width": int(
                video.get(
                    "width",
                    0,
                ) or 0
            ),

            "height": int(
                video.get(
                    "height",
                    0,
                ) or 0
            ),

            "pix_fmt": video.get(
                "pix_fmt",
                "",
            ),

            "audio": audio is not None,
        }

    except Exception as e:

        print(
            "Probe error:",
            e,
            flush=True,
        )

        return None


# ============================================================
# FIND DOWNLOADED VIDEO
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

    mp4_files = [
        file
        for file in files
        if file.suffix.lower() == ".mp4"
    ]

    if mp4_files:

        return max(
            mp4_files,
            key=lambda x: x.stat().st_size,
        )

    return max(
        files,
        key=lambda x: x.stat().st_size,
    )


# ============================================================
# VIDEO CONVERSION
# ============================================================

def convert_video(source, temp_dir):

    if not ffmpeg_available():

        return (
            None,
            "FFmpeg is not installed.",
        )

    info = get_media_info(source)

    if not info:

        return (
            None,
            "FFmpeg could not read the downloaded video.",
        )

    output = (
        Path(temp_dir) /
        "final.mp4"
    )

    print(
        "Media information:",
        info,
        flush=True,
    )

    # --------------------------------------------------------
    # FAST PATH
    # Already H.264 + MP4 + 720p or below + yuv420p
    # --------------------------------------------------------

    compatible = (
        source.suffix.lower() == ".mp4"
        and info["codec"] == "h264"
        and info["pix_fmt"] in (
            "yuv420p",
            "yuvj420p",
        )
        and info["width"] > 0
        and info["height"] > 0
        and max(
            info["width"],
            info["height"],
        ) <= MAX_HEIGHT
    )

    if compatible:

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

    else:

        # ----------------------------------------------------
        # CONVERSION
        # ----------------------------------------------------

        # Resize while preserving aspect ratio.
        #
        # Example:
        # 1440x2560 -> 405x720
        #
        # Padding ensures even dimensions for H.264.

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
            str(source),

            # Video stream
            "-map",
            "0:v:0",

            "-vf",
            video_filter,

            # H.264
            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-crf",
            "28",

            # iPhone-compatible pixel format
            "-pix_fmt",
            "yuv420p",

            # Limit frame rate
            "-r",
            "30",

            # Use all available CPU threads
            "-threads",
            "0",
        ]

        # ----------------------------------------------------
        # AUDIO
        # ----------------------------------------------------
        #
        # IMPORTANT:
        # Some Instagram videos have NO AUDIO STREAM.
        #
        # Therefore audio options are added only when
        # an audio stream actually exists.

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

            "-avoid_negative_ts",
            "make_zero",

            str(output),
        ]

    print(
        "FFmpeg command:",
        " ".join(command),
        flush=True,
    )

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:

            error = (
                result.stderr[-4000:]
                if result.stderr
                else "Unknown FFmpeg error."
            )

            print(
                "\n========== FFMPEG ERROR ==========\n",
                error,
                "\n===================================\n",
                flush=True,
            )

            return (
                None,
                "Video conversion failed.\n\n"
                + error,
            )

        if not output.exists():

            return (
                None,
                "FFmpeg did not create the MP4 file.",
            )

        if output.stat().st_size <= 0:

            return (
                None,
                "FFmpeg created an empty MP4 file.",
            )

        print(
            "Conversion successful:",
            output,
            flush=True,
        )

        print(
            "Final size:",
            f"{output.stat().st_size / 1024 / 1024:.2f} MB",
            flush=True,
        )

        return output, None

    except subprocess.TimeoutExpired:

        return (
            None,
            "Video conversion timed out after 5 minutes.",
        )

    except Exception as e:

        return (
            None,
            f"FFmpeg error: {type(e).__name__}: {e}",
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

        # ----------------------------------------------------
        # FORMAT
        # ----------------------------------------------------
        #
        # First try a single combined video.
        # This is faster.
        #
        # Then try separate video/audio streams.
        #
        # Finally use whatever format is available.

        "format": (
            "best[height<=720]/"
            "best[height<=1080]/"
            "bestvideo[height<=720]+bestaudio/"
            "bestvideo[height<=1080]+bestaudio/"
            "best"
        ),

        "merge_output_format": "mp4",

        "noplaylist": True,

        "max_filesize": MAX_SIZE,

        # ----------------------------------------------------
        # RETRIES
        # ----------------------------------------------------

        "retries": 2,

        "fragment_retries": 2,

        "file_access_retries": 2,

        "socket_timeout": 15,

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        "continuedl": True,

        "overwrites": True,

        "concurrent_fragment_downloads": 4,

        # ----------------------------------------------------
        # HEADERS
        # ----------------------------------------------------

        "http_headers": {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },

        "quiet": True,

        "no_warnings": True,

        "noprogress": True,
    }

    if cookie:
        options["cookiefile"] = cookie

    return options


# ============================================================
# DOWNLOAD
# ============================================================

def download_video(url, temp_dir):

    errors = []

    # Public attempt first
    cookies = [None]

    # Instagram authenticated fallback
    if is_instagram(url):

        cookie = get_cookie_file(temp_dir)

        if cookie:
            cookies.append(cookie)

    for cookie in cookies:

        # ----------------------------------------------------
        # Remove previous downloaded files
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Instagram headers
        # ----------------------------------------------------

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
                (
                    "authenticated"
                    if cookie
                    else "public"
                ),
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

            if not source:

                errors.append(
                    "No video file was created."
                )

                continue

            print(
                "Downloaded:",
                source,
                flush=True,
            )

            # ------------------------------------------------
            # CONVERT
            # ------------------------------------------------

            video, error = convert_video(
                source,
                temp_dir,
            )

            if video:

                return video, None

            # Conversion failed.
            # Return the real FFmpeg error immediately.

            return None, error

        except yt_dlp.utils.DownloadError as e:

            error = str(e)

            print(
                "yt-dlp error:",
                error,
                flush=True,
            )

            errors.append(error)

        except Exception as e:

            error = (
                f"{type(e).__name__}: {e}"
            )

            print(
                error,
                flush=True,
            )

            errors.append(error)

    # ========================================================
    # FRIENDLY ERRORS
    # ========================================================

    error = "\n\n".join(errors)

    low = error.lower()

    if "requested format is not available" in low:

        return (
            None,
            "The requested video format "
            "is not available."
        )

    if "no video formats found" in low:

        return (
            None,
            "No downloadable video format "
            "was provided by the platform."
        )

    if any(
        x in low
        for x in (
            "login required",
            "authentication",
            "sign in",
            "cookies",
        )
    ):

        return (
            None,
            "Authentication is required. "
            "Your Instagram cookies may be expired."
        )

    if "private" in low:

        return (
            None,
            "This video is private or "
            "your account cannot access it."
        )

    if (
        "403" in low
        or "forbidden" in low
        or "access denied" in low
    ):

        return (
            None,
            "The platform denied access "
            "to this video."
        )

    if (
        "429" in low
        or "rate limit" in low
    ):

        return (
            None,
            "The platform is rate-limiting "
            "the bot. Try again later."
        )

    return (
        None,
        error[-4000:]
        if error
        else "Unknown download error."
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
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await start(
        update,
        context,
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

    text = update.message.text

    if not text:
        return

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    url = extract_url(text)

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid video URL."
        )

        return

    # --------------------------------------------------------
    # PLATFORM
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    status = await update.message.reply_text(
        "⚡ Finding video..."
    )

    try:

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        # ----------------------------------------------------
        # TEMP DIRECTORY
        # ----------------------------------------------------

        with tempfile.TemporaryDirectory(
            prefix="video_"
        ) as temp_dir:

            # ------------------------------------------------
            # DOWNLOAD IN BACKGROUND
            # ------------------------------------------------

            video, error = await asyncio.to_thread(
                download_video,
                url,
                temp_dir,
            )

            # ------------------------------------------------
            # FAILED
            # ------------------------------------------------

            if not video:

                await status.edit_text(
                    "❌ Download failed.\n\n"
                    + (
                        error
                        or "Unknown error."
                    )
                )

                return

            # ------------------------------------------------
            # SIZE
            # ------------------------------------------------

            size = video.stat().st_size

            if size > MAX_SIZE:

                await status.edit_text(

                    "❌ Video is too large.\n\n"

                    f"Size: "
                    f"{size / 1024 / 1024:.1f} MB\n"

                    "Maximum: 45 MB"
                )

                return

            # ------------------------------------------------
            # UPLOAD
            # ------------------------------------------------

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

            # ------------------------------------------------
            # DELETE STATUS
            # ------------------------------------------------

            try:

                await status.delete()

            except Exception:
                pass

    except Exception as e:

        print(
            "Handler error:",
            repr(e),
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
# TELEGRAM ERROR HANDLER
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
        (
            "OK"
            if ffmpeg_available()
            else "NOT FOUND"
        ),
        flush=True,
    )

    print(
        "Instagram cookies:",
        (
            "YES"
            if INSTAGRAM_COOKIES_B64
            else "NO"
        ),
        flush=True,
    )

    # --------------------------------------------------------
    # TELEGRAM APP
    # --------------------------------------------------------

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # URL MESSAGES
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # POLLING
    # --------------------------------------------------------

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
