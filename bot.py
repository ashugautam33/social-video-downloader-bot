import os
import re
import json
import base64
import shutil
import asyncio
import logging
import tempfile
import subprocess

from pathlib import Path
from urllib.parse import (
    urlsplit,
    urlunsplit,
    parse_qs,
    unquote,
)

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

SOCIAL_COOKIES_B64 = os.getenv(
    "INSTAGRAM_COOKIES_B64"
)

MAX_FILE_SIZE = 45 * 1024 * 1024
MAX_VIDEO_HEIGHT = 720

BASE_DIR = (
    Path(tempfile.gettempdir())
    / "universal_media_downloader"
)

BASE_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

LIGHTNING_GIF = (
    BASE_DIR / "lightning.gif"
)


# ============================================================
# EXTENSIONS
# ============================================================

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".avi",
    ".flv",
    ".3gp",
    ".ts",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".flac",
    ".wma",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tiff",
}

SVG_EXTENSIONS = {
    ".svg",
}

GIF_EXTENSIONS = {
    ".gif",
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format=(
        "%(asctime)s - "
        "%(levelname)s - "
        "%(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(
    "UniversalMediaDownloader"
)


# ============================================================
# LIGHTNING GIF
# ============================================================

def create_lightning_gif():

    if LIGHTNING_GIF.exists():
        return

    frames = []

    for i in range(12):

        image = Image.new(
            "RGBA",
            (400, 400),
            (0, 0, 0, 0),
        )

        draw = ImageDraw.Draw(image)

        pulse = (
            1.0
            + 0.08 * (i % 6) / 5
        )

        cx = 200
        cy = 200

        points = [
            (
                cx + int(35 * pulse),
                cy - int(145 * pulse),
            ),
            (
                cx - int(70 * pulse),
                cy + int(5 * pulse),
            ),
            (
                cx - int(5 * pulse),
                cy + int(5 * pulse),
            ),
            (
                cx - int(45 * pulse),
                cy + int(145 * pulse),
            ),
            (
                cx + int(85 * pulse),
                cy - int(20 * pulse),
            ),
            (
                cx + int(20 * pulse),
                cy - int(20 * pulse),
            ),
        ]

        for width, alpha in (
            (30, 25),
            (20, 45),
            (12, 75),
        ):

            draw.line(
                points + [points[0]],
                fill=(
                    255,
                    220,
                    0,
                    alpha,
                ),
                width=width,
                joint="curve",
            )

        draw.polygon(
            points,
            fill=(
                255,
                215,
                0,
                255,
            ),
        )

        draw.line(
            points + [points[0]],
            fill=(
                255,
                255,
                255,
                255,
            ),
            width=5,
            joint="curve",
        )

        frames.append(image)

    frames[0].save(
        LIGHTNING_GIF,
        save_all=True,
        append_images=frames[1:],
        duration=90,
        loop=0,
        disposal=2,
    )


# ============================================================
# URL EXTRACTION
# ============================================================

def extract_url(text):

    if not text:
        return None

    match = re.search(
        r"https?://[^\s]+",
        text,
    )

    if not match:
        return None

    url = match.group(0).rstrip(
        ".,!?)]}"
    )

    return normalize_url(url)


# ============================================================
# FACEBOOK URL NORMALIZATION
# ============================================================

def normalize_url(url):

    if not url:
        return url

    try:

        parts = urlsplit(url)

        host = parts.netloc.lower()
        path = parts.path.lower()

        # ----------------------------------------------------
        # Facebook login redirect
        # ----------------------------------------------------

        if (
            "facebook.com" in host
            and path.startswith("/login")
        ):

            query = parse_qs(
                parts.query
            )

            next_values = query.get(
                "next"
            )

            if next_values:

                next_url = unquote(
                    next_values[0]
                )

                logger.info(
                    "Facebook login redirect detected."
                )

                return normalize_url(
                    next_url
                )

        # ----------------------------------------------------
        # Facebook
        # ----------------------------------------------------

        if (
            "facebook.com" in host
            or "fb.watch" in host
        ):

            # Keep useful query parameters for
            # Facebook story/share URLs.
            return urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    parts.query,
                    "",
                )
            )

        # ----------------------------------------------------
        # Other websites
        # ----------------------------------------------------

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                "",
                "",
            )
        )

    except Exception:

        return url


# ============================================================
# WEBSITE CHECKS
# ============================================================

def is_instagram(url):

    return (
        "instagram.com"
        in url.lower()
    )


def is_facebook(url):

    value = url.lower()

    return (
        "facebook.com" in value
        or "fb.watch" in value
    )


# ============================================================
# COOKIE CREATION
# ============================================================

def create_cookie_file():

    if not SOCIAL_COOKIES_B64:

        return None

    cookie_file = (
        BASE_DIR
        / "social_cookies.txt"
    )

    try:

        value = (
            SOCIAL_COOKIES_B64
            .strip()
        )

        # ----------------------------------------------------
        # Base64
        # ----------------------------------------------------

        try:

            decoded = base64.b64decode(
                value,
                validate=True,
            )

        except Exception:

            decoded = None

        if decoded:

            if decoded.startswith(
                b"\xef\xbb\xbf"
            ):

                decoded = decoded[3:]

            try:

                text = decoded.decode(
                    "utf-8"
                )

            except UnicodeDecodeError:

                logger.error(
                    "Cookie data is not UTF-8."
                )

                return None

        else:

            # Plain cookies.txt
            text = value

        # ----------------------------------------------------
        # Validate Netscape cookies
        # ----------------------------------------------------

        if (
            "# Netscape HTTP Cookie File"
            not in text
            and "\t" not in text
        ):

            logger.error(
                "Invalid cookies.txt format."
            )

            return None

        cookie_file.write_text(
            text,
            encoding="utf-8",
        )

        return str(
            cookie_file
        )

    except Exception as e:

        logger.error(
            "Cookie processing error: %s",
            e,
        )

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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

        return (
            result.returncode == 0
        )

    except Exception:

        return False


# ============================================================
# FFPROBE
# ============================================================

def probe(file_path):

    try:

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(file_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return None

        return json.loads(
            result.stdout
        )

    except Exception:

        return None


def get_streams(file_path):

    data = probe(
        file_path
    )

    if not data:
        return None, None

    streams = data.get(
        "streams",
        [],
    )

    video = next(
        (
            stream
            for stream in streams
            if stream.get(
                "codec_type"
            ) == "video"
        ),
        None,
    )

    audio = next(
        (
            stream
            for stream in streams
            if stream.get(
                "codec_type"
            ) == "audio"
        ),
        None,
    )

    return video, audio


# ============================================================
# MEDIA TYPE
# ============================================================

def detect_media_type(file_path):

    extension = (
        Path(file_path)
        .suffix
        .lower()
    )

    if extension in VIDEO_EXTENSIONS:
        return "video"

    if extension in AUDIO_EXTENSIONS:
        return "audio"

    if extension in IMAGE_EXTENSIONS:
        return "image"

    if extension in SVG_EXTENSIONS:
        return "svg"

    if extension in GIF_EXTENSIONS:
        return "gif"

    video, audio = get_streams(
        file_path
    )

    if video:
        return "video"

    if audio:
        return "audio"

    return "document"


# ============================================================
# YT-DLP OPTIONS
# ============================================================

def build_ydl_options(
    output_template,
    url,
):

    options = {

        # Best video that may already
        # contain audio, then separate
        # audio if required.
        "format": (
            "bv*+ba/"
            "b"
        ),

        "outtmpl": output_template,

        # Needed for Instagram/TikTok
        # carousels/slideshows.
        "noplaylist": False,

        "quiet": False,

        "no_warnings": False,

        "ignoreerrors": False,

        "retries": 3,

        "fragment_retries": 3,

        "file_access_retries": 3,

        "socket_timeout": 30,

        "concurrent_fragment_downloads": 4,

        "merge_output_format": "mp4",

        "writethumbnail": False,

        "writeinfojson": False,

        "writesubtitles": False,

        "writeautomaticsub": False,

        "http_headers": {

            "User-Agent":
                (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/140.0.0.0 "
                    "Safari/537.36"
                ),

            "Accept": "*/*",

            "Accept-Language":
                "en-US,en;q=0.9",
        },
    }

    # --------------------------------------------------------
    # Instagram / Facebook cookies
    # --------------------------------------------------------

    if (
        is_instagram(url)
        or is_facebook(url)
    ):

        cookie_file = (
            create_cookie_file()
        )

        if cookie_file:

            options[
                "cookiefile"
            ] = cookie_file

    return options


# ============================================================
# FIND DOWNLOADED MEDIA
# ============================================================

def find_media_files(folder):

    supported = (
        VIDEO_EXTENSIONS
        | AUDIO_EXTENSIONS
        | IMAGE_EXTENSIONS
        | SVG_EXTENSIONS
        | GIF_EXTENSIONS
    )

    files = []

    for file in folder.iterdir():

        if not file.is_file():
            continue

        if (
            file.suffix.lower()
            not in supported
        ):
            continue

        if file.stat().st_size <= 100:
            continue

        files.append(file)

    files.sort(
        key=lambda x: x.name
    )

    return files


# ============================================================
# DOWNLOAD
# ============================================================

def download_media(url):

    url = normalize_url(
        url
    )

    logger.info(
        "Normalized URL: %s",
        url,
    )

    folder = Path(
        tempfile.mkdtemp(
            prefix="media_",
            dir=BASE_DIR,
        )
    )

    output_template = str(
        folder
        / "%(autonumber)03d_%(id)s.%(ext)s"
    )

    attempts = [
        url
    ]

    cleaned = normalize_url(
        url
    )

    if cleaned != url:

        attempts.append(
            cleaned
        )

    attempts = list(
        dict.fromkeys(
            attempts
        )
    )

    last_error = None

    for number, current_url in enumerate(
        attempts,
        start=1,
    ):

        logger.info(
            "Download attempt %d",
            number,
        )

        try:

            options = (
                build_ydl_options(
                    output_template,
                    current_url,
                )
            )

            with yt_dlp.YoutubeDL(
                options
            ) as ydl:

                info = ydl.extract_info(
                    current_url,
                    download=True,
                )

                if info:

                    logger.info(
                        "Extractor: %s",
                        info.get(
                            "extractor"
                        ),
                    )

                    logger.info(
                        "Title: %s",
                        info.get(
                            "title"
                        ),
                    )

                    logger.info(
                        "Type: %s",
                        info.get(
                            "_type"
                        ),
                    )

        except Exception as e:

            last_error = str(e)

            logger.error(
                "yt-dlp error: %s",
                last_error,
            )

            continue

        files = find_media_files(
            folder
        )

        if files:

            return (
                files,
                folder,
                None,
            )

    return (
        None,
        folder,
        last_error
        or "No media was downloaded.",
    )


# ============================================================
# VIDEO CONVERSION
# ============================================================

def convert_video(
    input_file
):

    input_file = Path(
        input_file
    )

    output_file = (
        input_file.parent
        / f"{input_file.stem}_telegram.mp4"
    )

    video, audio = get_streams(
        input_file
    )

    if not video:

        return (
            None,
            "No video stream found.",
        )

    height = int(
        video.get("height")
        or 0
    )

    codec = video.get(
        "codec_name",
        "",
    )

    pixel_format = video.get(
        "pix_fmt",
        "",
    )

    logger.info(
        "Input video: codec=%s height=%s pixel=%s audio=%s",
        codec,
        height,
        pixel_format,
        bool(audio),
    )

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

        # Optional audio
        "-map",
        "0:a:0?",

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

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-ar",
        "44100",

        "-ac",
        "2",

        "-movflags",
        "+faststart",

        "-avoid_negative_ts",
        "make_zero",

        str(output_file),
    ]

    try:

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
        )

    except subprocess.TimeoutExpired:

        return (
            None,
            "Video conversion timed out.",
        )

    except Exception as e:

        return (
            None,
            str(e),
        )

    if result.returncode != 0:

        return (
            None,
            result.stderr[-4000:],
        )

    if not output_file.exists():

        return (
            None,
            "FFmpeg output missing.",
        )

    output_video, output_audio = (
        get_streams(
            output_file
        )
    )

    if not output_video:

        return (
            None,
            "Output has no video.",
        )

    if audio and not output_audio:

        return (
            None,
            "Audio was lost during conversion.",
        )

    return (
        output_file,
        None,
    )


# ============================================================
# SEND IMAGE
# ============================================================

async def send_image(
    update,
    file,
):

    if (
        file.stat().st_size
        <= 10 * 1024 * 1024
    ):

        with open(
            file,
            "rb",
        ) as image:

            await update.message.reply_photo(
                photo=image
            )

    else:

        with open(
            file,
            "rb",
        ) as image:

            await update.message.reply_document(
                document=image,
                caption="🖼️ Image",
            )


# ============================================================
# SEND SVG
# ============================================================

async def send_svg(
    update,
    file,
):

    if (
        file.stat().st_size
        > MAX_FILE_SIZE
    ):

        raise RuntimeError(
            "SVG file is too large."
        )

    with open(
        file,
        "rb",
    ) as svg:

        await update.message.reply_document(
            document=svg,
            filename=file.name,
            caption="🎨 SVG",
        )


# ============================================================
# SEND GIF
# ============================================================

async def send_gif(
    update,
    file,
):

    if (
        file.stat().st_size
        > MAX_FILE_SIZE
    ):

        raise RuntimeError(
            "GIF file is too large."
        )

    with open(
        file,
        "rb",
    ) as gif:

        await update.message.reply_animation(
            animation=gif
        )


# ============================================================
# SEND AUDIO
# ============================================================

async def send_audio(
    update,
    file,
):

    if (
        file.stat().st_size
        > MAX_FILE_SIZE
    ):

        raise RuntimeError(
            "Audio file is too large."
        )

    with open(
        file,
        "rb",
    ) as audio:

        await update.message.reply_audio(
            audio=audio
        )


# ============================================================
# SEND DOCUMENT
# ============================================================

async def send_document(
    update,
    file,
):

    if (
        file.stat().st_size
        > MAX_FILE_SIZE
    ):

        raise RuntimeError(
            "File is too large."
        )

    with open(
        file,
        "rb",
    ) as document:

        await update.message.reply_document(
            document=document
        )


# ============================================================
# SEND VIDEO
# ============================================================

async def send_video(
    update,
    file,
):

    information = probe(
        file
    )

    if not information:

        return (
            False,
            "Could not inspect video.",
        )

    streams = information.get(
        "streams",
        [],
    )

    video_stream = next(
        (
            x
            for x in streams
            if x.get(
                "codec_type"
            ) == "video"
        ),
        None,
    )

    audio_stream = next(
        (
            x
            for x in streams
            if x.get(
                "codec_type"
            ) == "audio"
        ),
        None,
    )

    if not video_stream:

        return (
            False,
            "No video stream found.",
        )

    codec = video_stream.get(
        "codec_name"
    )

    pixel_format = video_stream.get(
        "pix_fmt"
    )

    height = int(
        video_stream.get(
            "height"
        )
        or 0
    )

    needs_conversion = (
        codec != "h264"
        or pixel_format != "yuv420p"
        or height > MAX_VIDEO_HEIGHT
        or file.stat().st_size
        > MAX_FILE_SIZE
    )

    final_file = file

    if needs_conversion:

        converted, error = (
            await asyncio.to_thread(
                convert_video,
                file,
            )
        )

        if not converted:

            return (
                False,
                error,
            )

        final_file = converted

    if (
        final_file.stat().st_size
        > MAX_FILE_SIZE
    ):

        return (
            False,
            "Final video is too large.",
        )

    final_video, final_audio = (
        get_streams(
            final_file
        )
    )

    if audio_stream and not final_audio:

        return (
            False,
            "The final video has no audio.",
        )

    with open(
        final_file,
        "rb",
    ) as video:

        await update.message.reply_video(
            video=video,
            supports_streaming=True,
            caption="🎬 Done",
        )

    return (
        True,
        None,
    )


# ============================================================
# SEND ANY MEDIA
# ============================================================

async def send_media_file(
    update,
    file,
):

    media_type = detect_media_type(
        file
    )

    logger.info(
        "Media type: %s | %s",
        media_type,
        file.name,
    )

    if media_type == "video":

        return await send_video(
            update,
            file,
        )

    if media_type == "audio":

        await send_audio(
            update,
            file,
        )

        return (
            True,
            None,
        )

    if media_type == "image":

        await send_image(
            update,
            file,
        )

        return (
            True,
            None,
        )

    if media_type == "svg":

        await send_svg(
            update,
            file,
        )

        return (
            True,
            None,
        )

    if media_type == "gif":

        await send_gif(
            update,
            file,
        )

        return (
            True,
            None,
        )

    await send_document(
        update,
        file,
    )

    return (
        True,
        None,
    )


# ============================================================
# STATUS
# ============================================================

async def send_status(
    update
):

    try:

        with open(
            LIGHTNING_GIF,
            "rb",
        ) as animation:

            return await update.message.reply_animation(
                animation=animation,
                caption=(
                    "Thanks for providing the link!"
                ),
            )

    except Exception:

        return await update.message.reply_text(
            "Thanks for providing the link!\n\n"
            "⚡ Processing..."
        )


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "👋 Universal Media Downloader\n\n"

        "Send me a media link.\n\n"

        "Supported:\n"
        "🎬 Video\n"
        "🎵 Audio\n"
        "🖼️ Photos\n"
        "🎨 SVG\n"
        "🎞️ GIF\n"
        "📚 Carousels\n"
        "📱 Reels / Shorts\n\n"

        "Works with Instagram, Facebook, "
        "YouTube, TikTok, X/Twitter and "
        "many other yt-dlp supported sites."
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

    text = (
        update.message.text
        or update.message.caption
        or ""
    )

    url = extract_url(
        text
    )

    if not url:

        await update.message.reply_text(
            "❌ Please send a valid media URL."
        )

        return

    logger.info(
        "Received URL: %s",
        url,
    )

    status = await send_status(
        update
    )

    folder = None

    try:

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.UPLOAD_VIDEO,
        )

        files, folder, error = (
            await asyncio.to_thread(
                download_media,
                url,
            )
        )

        # ====================================================
        # DOWNLOAD FAILED
        # ====================================================

        if error:

            try:
                await status.delete()
            except Exception:
                pass

            error_lower = error.lower()

            # ------------------------------------------------
            # Facebook
            # ------------------------------------------------

            if is_facebook(url):

                if (
                    "unsupported url"
                    in error_lower
                    or "login"
                    in error_lower
                    or "authentication"
                    in error_lower
                    or "private"
                    in error_lower
                    or "cookies"
                    in error_lower
                    or "empty"
                    in error_lower
                ):

                    await update.message.reply_text(
                        "❌ Facebook could not provide "
                        "this media.\n\n"

                        "The URL was converted from "
                        "Facebook's login redirect, "
                        "but Facebook is still requiring "
                        "access to the post.\n\n"

                        "Possible reasons:\n"
                        "• Login required\n"
                        "• Private/restricted post\n"
                        "• Expired Facebook cookies\n"
                        "• Facebook blocked the request\n"
                        "• Media no longer available\n\n"

                        "For media that requires your "
                        "Facebook login, configure valid "
                        "cookies in Railway using:\n\n"
                        "INSTAGRAM_COOKIES_B64"
                    )

                    return

            # ------------------------------------------------
            # Instagram
            # ------------------------------------------------

            if is_instagram(url):

                await update.message.reply_text(
                    "❌ Instagram could not provide "
                    "this media.\n\n"

                    "Possible reasons:\n"
                    "• Login required\n"
                    "• Private post\n"
                    "• Expired cookies\n"
                    "• Instagram blocked the request\n"
                    "• Media unavailable"
                )

                return

            # ------------------------------------------------
            # General error
            # ------------------------------------------------

            await update.message.reply_text(
                "❌ Download failed.\n\n"
                f"{error[-3000:]}"
            )

            return

        # ====================================================
        # DELETE STATUS
        # ====================================================

        try:

            await status.delete()

        except Exception:

            pass

        # ====================================================
        # SEND MEDIA
        # ====================================================

        total = len(
            files
        )

        sent = 0

        for index, file in enumerate(
            files,
            start=1,
        ):

            if not file.exists():
                continue

            logger.info(
                "Sending %d/%d: %s",
                index,
                total,
                file.name,
            )

            try:

                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action=ChatAction.UPLOAD_VIDEO,
                )

                ok, send_error = (
                    await send_media_file(
                        update,
                        file,
                    )
                )

                if ok:

                    sent += 1

                else:

                    await update.message.reply_text(
                        "⚠️ Could not send media.\n\n"
                        f"{send_error}"
                    )

            except Exception as e:

                logger.exception(
                    "Media sending error."
                )

                await update.message.reply_text(
                    "⚠️ One media file could not "
                    "be sent.\n\n"
                    f"{str(e)[:1500]}"
                )

        # ====================================================
        # RESULT
        # ====================================================

        if sent == 0:

            await update.message.reply_text(
                "❌ No media could be sent."
            )

        elif total > 1:

            await update.message.reply_text(
                f"✅ Done — {sent}/{total} "
                "media files sent."
            )

    except Exception as e:

        logger.exception(
            "Unexpected error."
        )

        try:

            await status.delete()

        except Exception:

            pass

        await update.message.reply_text(
            "❌ Something went wrong.\n\n"
            f"{str(e)[:3000]}"
        )

    finally:

        # ====================================================
        # CLEANUP
        # ====================================================

        if folder and folder.exists():

            try:

                await asyncio.to_thread(
                    shutil.rmtree,
                    folder,
                    True,
                )

            except Exception:

                pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "Telegram error",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is not configured."
        )

    create_lightning_gif()

    logger.info(
        "=========================================="
    )

    logger.info(
        "Universal Media Downloader"
    )

    logger.info(
        "yt-dlp: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "FFmpeg: %s",
        ffmpeg_available(),
    )

    logger.info(
        "Cookies configured: %s",
        bool(
            SOCIAL_COOKIES_B64
        ),
    )

    logger.info(
        "=========================================="
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.CaptionRegex(
                r"https?://"
            ),
            handle_message,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot is running..."
    )

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
