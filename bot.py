import os
import re
import asyncio
import logging
import tempfile
import subprocess
import base64
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

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

MAX_FILE_SIZE = 45 * 1024 * 1024
MAX_HEIGHT = 720

# Railway environment variable.
#
# Store the Base64 encoded contents of your Instagram
# Netscape cookies.txt file here.
INSTAGRAM_COOKIES_B64 = os.getenv(
    "INSTAGRAM_COOKIES_B64"
)

# Optional direct cookie file path.
INSTAGRAM_COOKIE_FILE = os.getenv(
    "INSTAGRAM_COOKIE_FILE",
    "cookies.txt",
)


# ============================================================
# SUPPORTED HOSTS
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
# VIDEO EXTENSIONS
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
# USER AGENT
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/140.0.0.0 "
    "Safari/537.36"
)


# ============================================================
# URL HELPERS
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

    url = match.group(0)

    return url.rstrip(
        ").,]}>'\""
    )


def is_instagram_url(url: str) -> bool:

    return "instagram.com" in url.lower()


# ============================================================
# CREATE INSTAGRAM COOKIE FILE
# ============================================================

def prepare_instagram_cookies(
    temp_dir: str,
) -> Path | None:
    """
    Create a temporary Instagram cookies.txt file.

    Priority:

    1. INSTAGRAM_COOKIES_B64
    2. local cookies.txt
    """

    try:

        # ----------------------------------------------------
        # Railway environment variable
        # ----------------------------------------------------

        if INSTAGRAM_COOKIES_B64:

            cookie_file = (
                Path(temp_dir)
                / "instagram_cookies.txt"
            )

            try:

                decoded = base64.b64decode(
                    INSTAGRAM_COOKIES_B64
                )

                cookie_file.write_bytes(
                    decoded
                )

                if cookie_file.stat().st_size > 0:

                    logger.info(
                        "Instagram cookies loaded "
                        "from environment variable."
                    )

                    return cookie_file

            except Exception:

                logger.exception(
                    "Could not decode "
                    "INSTAGRAM_COOKIES_B64."
                )

        # ----------------------------------------------------
        # Local cookies.txt
        # ----------------------------------------------------

        local_cookie_file = Path(
            INSTAGRAM_COOKIE_FILE
        )

        if local_cookie_file.exists():

            if local_cookie_file.stat().st_size > 0:

                logger.info(
                    "Instagram cookies.txt found."
                )

                return local_cookie_file

    except Exception:

        logger.exception(
            "Instagram cookie preparation failed."
        )

    return None


# ============================================================
# FIND VIDEO
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
            (
                ".part",
                ".ytdl",
                ".tmp",
            )
        ):
            continue

        if file.suffix.lower() not in VIDEO_EXTENSIONS:
            continue

        try:

            if file.stat().st_size > 0:

                video_files.append(file)

        except OSError:

            continue

    if not video_files:
        return None

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
# FIND FFMPEG
# ============================================================

def find_ffmpeg() -> str | None:

    try:

        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:

            return "ffmpeg"

    except Exception:

        pass

    return None


# ============================================================
# CONVERT VIDEO
# ============================================================

def convert_to_mp4(
    input_file: Path,
    temp_dir: str,
) -> tuple[Path | None, str | None]:

    output_file = (
        Path(temp_dir)
        / "final_video.mp4"
    )

    ffmpeg = find_ffmpeg()

    if not ffmpeg:

        return (
            None,
            "FFmpeg is not installed."
        )

    command = [
        ffmpeg,

        "-y",

        "-i",
        str(input_file),

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "23",

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-pix_fmt",
        "yuv420p",

        "-movflags",
        "+faststart",

        str(output_file),
    ]

    try:

        logger.info(
            "Converting %s to MP4",
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
                result.stderr[-4000:],
            )

            return (
                None,
                "FFmpeg could not convert the video."
            )

        if not output_file.exists():

            return (
                None,
                "FFmpeg did not create the MP4 file."
            )

        if output_file.stat().st_size <= 0:

            return (
                None,
                "The converted video is empty."
            )

        return (
            output_file,
            None,
        )

    except subprocess.TimeoutExpired:

        return (
            None,
            "Video conversion timed out."
        )

    except Exception as error:

        logger.exception(
            "FFmpeg conversion error."
        )

        return (
            None,
            f"{type(error).__name__}: {error}"
        )


# ============================================================
# BASE YT-DLP OPTIONS
# ============================================================

def get_base_ydl_options(
    temp_dir: str,
) -> dict:

    output_template = str(
        Path(temp_dir)
        / "download_%(id)s_%(autonumber)s.%(ext)s"
    )

    return {

        "outtmpl": output_template,

        # ----------------------------------------------------
        # Video selection
        # ----------------------------------------------------

        "format": (
            f"bestvideo[height<={MAX_HEIGHT}]"
            f"+bestaudio/"
            f"best[height<={MAX_HEIGHT}]/"
            f"best"
        ),

        "merge_output_format": "mp4",

        # ----------------------------------------------------
        # Download behavior
        # ----------------------------------------------------

        "noplaylist": True,

        "restrictfilenames": True,

        "writethumbnail": False,

        "writeinfojson": False,

        "writesubtitles": False,

        "writeautomaticsub": False,

        # ----------------------------------------------------
        # Network
        # ----------------------------------------------------

        "retries": 5,

        "fragment_retries": 5,

        "file_access_retries": 5,

        "socket_timeout": 30,

        "continuedl": True,

        "overwrites": True,

        "concurrent_fragment_downloads": 4,

        # ----------------------------------------------------
        # Do not download gigantic source files
        # ----------------------------------------------------

        "max_filesize": MAX_FILE_SIZE,

        # ----------------------------------------------------
        # Browser headers
        # ----------------------------------------------------

        "http_headers": {

            "User-Agent": USER_AGENT,

            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,"
                "image/webp,"
                "image/apng,"
                "*/*;q=0.8"
            ),

            "Accept-Language":
                "en-US,en;q=0.9",

            "Sec-Fetch-Dest":
                "document",

            "Sec-Fetch-Mode":
                "navigate",

            "Sec-Fetch-Site":
                "none",

            "Upgrade-Insecure-Requests":
                "1",
        },

        # ----------------------------------------------------
        # Avoid unnecessary output
        # ----------------------------------------------------

        "quiet": True,

        "no_warnings": False,
    }


# ============================================================
# INSTAGRAM OPTIONS
# ============================================================

def get_instagram_options(
    temp_dir: str,
    use_cookies: bool,
) -> dict:

    options = get_base_ydl_options(
        temp_dir
    )

    # Instagram-specific browser headers.
    options["http_headers"].update({

        "Referer":
            "https://www.instagram.com/",

        "Origin":
            "https://www.instagram.com/",
    })

    # --------------------------------------------------------
    # Cookies
    # --------------------------------------------------------

    if use_cookies:

        cookie_file = (
            prepare_instagram_cookies(
                temp_dir
            )
        )

        if cookie_file:

            options["cookiefile"] = (
                str(cookie_file)
            )

            logger.info(
                "Instagram authentication "
                "cookies enabled."
            )

        else:

            logger.warning(
                "Instagram cookies requested "
                "but no cookies were found."
            )

    return options


# ============================================================
# GENERIC OPTIONS
# ============================================================

def get_generic_options(
    temp_dir: str,
) -> dict:

    return get_base_ydl_options(
        temp_dir
    )


# ============================================================
# CLASSIFY ERROR
# ============================================================

def classify_download_error(
    error_text: str,
    instagram: bool,
) -> str:

    text = error_text.lower()

    # --------------------------------------------------------
    # Instagram login
    # --------------------------------------------------------

    if instagram and any(
        phrase in text
        for phrase in (
            "login required",
            "login",
            "authentication",
            "cookies",
            "sign in",
        )
    ):

        return (
            "Instagram requires login/authentication "
            "for this post. Add a fresh Instagram "
            "cookies.txt file to Railway."
        )

    # --------------------------------------------------------
    # No formats
    # --------------------------------------------------------

    if "no video formats found" in text:

        return (
            "Instagram did not provide a downloadable "
            "video format.\n\n"
            "This can happen when the post is image-only, "
            "restricted, unavailable, or Instagram has "
            "changed the data returned to yt-dlp."
        )

    # --------------------------------------------------------
    # Private
    # --------------------------------------------------------

    if "private" in text:

        return (
            "This video appears to be private. "
            "Use Instagram cookies from an account "
            "that can view the post."
        )

    # --------------------------------------------------------
    # 403 / forbidden
    # --------------------------------------------------------

    if (
        "403" in text
        or "forbidden" in text
        or "access denied" in text
    ):

        return (
            "Instagram/platform access was denied. "
            "Fresh authentication cookies may be required."
        )

    # --------------------------------------------------------
    # 404
    # --------------------------------------------------------

    if "404" in text:

        return (
            "The Instagram post could not be found. "
            "It may have been deleted, changed to private, "
            "or is no longer available."
        )

    # --------------------------------------------------------
    # Rate limit
    # --------------------------------------------------------

    if any(
        phrase in text
        for phrase in (
            "rate limit",
            "too many requests",
            "429",
        )
    ):

        return (
            "The platform is rate-limiting the bot. "
            "Please wait and try again later."
        )

    # --------------------------------------------------------
    # Generic
    # --------------------------------------------------------

    return error_text[:2500]


# ============================================================
# ONE DOWNLOAD ATTEMPT
# ============================================================

def perform_yt_dlp_download(
    url: str,
    temp_dir: str,
    options: dict,
) -> tuple[Path | None, str | None]:

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            info = ydl.extract_info(
                url,
                download=True,
            )

            if not info:

                return (
                    None,
                    "yt-dlp returned no information."
                )

        video = find_downloaded_video(
            temp_dir
        )

        if not video:

            return (
                None,
                "No video file was created."
            )

        return (
            video,
            None,
        )

    except yt_dlp.utils.DownloadError as error:

        logger.error(
            "yt-dlp DownloadError: %s",
            error,
        )

        return (
            None,
            str(error),
        )

    except Exception as error:

        logger.exception(
            "yt-dlp unexpected error."
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

    instagram = is_instagram_url(
        url
    )

    errors = []

    # ========================================================
    # INSTAGRAM
    # ========================================================

    if instagram:

        # ----------------------------------------------------
        # ATTEMPT 1
        # Public access
        # ----------------------------------------------------

        logger.info(
            "Instagram attempt 1: public extraction."
        )

        options = get_instagram_options(
            temp_dir,
            use_cookies=False,
        )

        video, error = (
            perform_yt_dlp_download(
                url,
                temp_dir,
                options,
            )
        )

        if video:

            return finalize_video(
                video,
                temp_dir,
            )

        if error:

            errors.append(
                "Public extraction:\n"
                + error
            )

        # ----------------------------------------------------
        # ATTEMPT 2
        # Authenticated cookies
        # ----------------------------------------------------

        cookie_file = (
            prepare_instagram_cookies(
                temp_dir
            )
        )

        if cookie_file:

            logger.info(
                "Instagram attempt 2: "
                "authenticated extraction."
            )

            options = get_instagram_options(
                temp_dir,
                use_cookies=True,
            )

            video, error = (
                perform_yt_dlp_download(
                    url,
                    temp_dir,
                    options,
                )
            )

            if video:

                return finalize_video(
                    video,
                    temp_dir,
                )

            if error:

                errors.append(
                    "Authenticated extraction:\n"
                    + error
                )

        # ----------------------------------------------------
        # Final Instagram error
        # ----------------------------------------------------

        combined_error = "\n\n".join(
            errors
        )

        return (
            None,
            classify_download_error(
                combined_error,
                True,
            ),
        )

    # ========================================================
    # OTHER PLATFORMS
    # ========================================================

    logger.info(
        "Generic yt-dlp extraction."
    )

    options = get_generic_options(
        temp_dir
    )

    video, error = (
        perform_yt_dlp_download(
            url,
            temp_dir,
            options,
        )
    )

    if not video:

        return (
            None,
            classify_download_error(
                error
                or "Unknown download error.",
                False,
            ),
        )

    return finalize_video(
        video,
        temp_dir,
    )


# ============================================================
# FINALIZE VIDEO
# ============================================================

def finalize_video(
    downloaded_file: Path,
    temp_dir: str,
) -> tuple[Path | None, str | None]:

    logger.info(
        "Downloaded file: %s",
        downloaded_file,
    )

    try:

        source_size = (
            downloaded_file.stat().st_size
        )

        if source_size <= 0:

            return (
                None,
                "Downloaded video is empty."
            )

    except OSError:

        return (
            None,
            "Could not read downloaded video."
        )

    # --------------------------------------------------------
    # Convert to MP4
    # --------------------------------------------------------

    final_file, error = (
        convert_to_mp4(
            downloaded_file,
            temp_dir,
        )
    )

    if not final_file:

        return (
            None,
            error
            or "Video conversion failed."
        )

    # --------------------------------------------------------
    # Final size
    # --------------------------------------------------------

    try:

        final_size = (
            final_file.stat().st_size
        )

    except OSError:

        return (
            None,
            "Could not read final MP4."
        )

    if final_size <= 0:

        return (
            None,
            "Final MP4 is empty."
        )

    if final_size > MAX_FILE_SIZE:

        return (
            None,
            (
                "Final video is too large.\n"
                f"Size: "
                f"{final_size / 1024 / 1024:.1f} MB\n"
                f"Maximum: "
                f"{MAX_FILE_SIZE / 1024 / 1024:.0f} MB"
            ),
        )

    logger.info(
        "Final MP4: %.2f MB",
        final_size / 1024 / 1024,
    )

    return (
        final_file,
        None,
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

        "⚠️ Private/restricted videos may "
        "require authentication cookies."
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
    # Supported URL
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
    # Status
    # --------------------------------------------------------

    status = await update.message.reply_text(
        "⏳ Downloading video...\n\n"
        "Please wait."
    )

    try:

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
            # Download
            # ------------------------------------------------

            downloaded_file, error = (
                await asyncio.to_thread(
                    download_video,
                    url,
                    temp_dir,
                )
            )

            # ------------------------------------------------
            # Failure
            # ------------------------------------------------

            if not downloaded_file:

                error_text = (
                    error
                    or "Unknown download error."
                )

                await status.edit_text(
                    "❌ I couldn't download this video.\n\n"
                    "Technical reason:\n"
                    f"{error_text[:3000]}"
                )

                return

            # ------------------------------------------------
            # Size check
            # ------------------------------------------------

            file_size = (
                downloaded_file.stat().st_size
            )

            file_size_mb = (
                file_size / 1024 / 1024
            )

            if file_size > MAX_FILE_SIZE:

                await status.edit_text(
                    "❌ Video is too large.\n\n"
                    f"Size: {file_size_mb:.1f} MB\n"
                    "Maximum: 45 MB"
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
                    f"{type(upload_error).__name__}: "
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
            "Handler error."
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
            "BOT_TOKEN environment variable is missing.\n"
            "Add BOT_TOKEN in Railway Variables."
        )

    logger.info(
        "Starting Social Video Downloader Bot..."
    )

    logger.info(
        "yt-dlp version: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "FFmpeg available: %s",
        bool(find_ffmpeg()),
    )

    logger.info(
        "Instagram cookies configured: %s",
        bool(INSTAGRAM_COOKIES_B64)
        or Path(INSTAGRAM_COOKIE_FILE).exists(),
    )

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
    # URLs
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
    # Start
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

    application.run_polling(
        drop_pending_updates=True,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()