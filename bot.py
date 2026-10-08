import os
import re
import json
import base64
import shutil
import asyncio
import logging
import tempfile
import subprocess
import html as html_lib

from pathlib import Path
from urllib.parse import (
    urlsplit,
    urlunsplit,
    parse_qs,
    unquote,
)

import requests
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

# Optional cookies.txt encoded as Base64
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
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
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
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tiff",
}

SPECIAL_EXTENSIONS = {
    ".svg",
    ".gif",
}

ALL_MEDIA_EXTENSIONS = (
    VIDEO_EXTENSIONS
    | AUDIO_EXTENSIONS
    | IMAGE_EXTENSIONS
    | SPECIAL_EXTENSIONS
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "UniversalDownloader"
)


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
# LIGHTNING
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

        draw = ImageDraw.Draw(
            image
        )

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
            (20, 50),
            (12, 80),
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
# URL
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


def is_facebook(url):

    value = url.lower()

    return (
        "facebook.com" in value
        or "fb.watch" in value
    )


def is_instagram(url):

    return (
        "instagram.com"
        in url.lower()
    )


def is_direct_media_url(url):

    path = urlsplit(url).path.lower()

    return any(
        path.endswith(ext)
        for ext in ALL_MEDIA_EXTENSIONS
    )


# ============================================================
# NORMALIZE FACEBOOK URL
# ============================================================

def normalize_url(url):

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

                return normalize_url(
                    next_url
                )

        # ----------------------------------------------------
        # Facebook: preserve query
        # ----------------------------------------------------

        if is_facebook(url):

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
        # Other URLs
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
# COOKIE FILE
# ============================================================

def create_cookie_file():

    if not SOCIAL_COOKIES_B64:

        return None

    cookie_file = (
        BASE_DIR
        / "social_cookies.txt"
    )

    try:

        raw = (
            SOCIAL_COOKIES_B64
            .strip()
        )

        try:

            decoded = base64.b64decode(
                raw,
                validate=True,
            )

        except Exception:

            decoded = None

        if decoded:

            if decoded.startswith(
                b"\xef\xbb\xbf"
            ):

                decoded = decoded[3:]

            text = decoded.decode(
                "utf-8"
            )

        else:

            text = raw

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
            "Cookie error: %s",
            e,
        )

        return None


# ============================================================
# REQUEST SESSION
# ============================================================

def create_session():

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "*/*;q=0.8"
            ),
            "Accept-Language":
                "en-US,en;q=0.9",
        }
    )

    cookie_file = (
        create_cookie_file()
    )

    if cookie_file:

        try:

            with open(
                cookie_file,
                "r",
                encoding="utf-8",
                errors="ignore",
            ) as f:

                for line in f:

                    line = line.strip()

                    if (
                        not line
                        or line.startswith("#")
                    ):
                        continue

                    parts = line.split(
                        "\t"
                    )

                    if len(parts) < 7:
                        continue

                    domain = parts[0]
                    path = parts[2]
                    name = parts[5]
                    value = parts[6]

                    try:

                        session.cookies.set(
                            name,
                            value,
                            domain=domain,
                            path=path,
                        )

                    except Exception:
                        pass

        except Exception as e:

            logger.warning(
                "Cookie loading error: %s",
                e,
            )

    return session


# ============================================================
# DIRECT MEDIA DOWNLOAD
# ============================================================

def direct_download(
    url,
    folder,
    referer=None,
    filename_prefix="media",
):

    session = create_session()

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }

    if referer:

        headers["Referer"] = referer

    try:

        response = session.get(
            url,
            headers=headers,
            stream=True,
            timeout=60,
            allow_redirects=True,
        )

        response.raise_for_status()

        content_type = (
            response.headers
            .get(
                "content-type",
                "",
            )
            .lower()
        )

        # ----------------------------------------------------
        # Determine extension
        # ----------------------------------------------------

        ext = (
            Path(
                urlsplit(url).path
            ).suffix
            .lower()
        )

        if ext not in ALL_MEDIA_EXTENSIONS:

            if "video/mp4" in content_type:
                ext = ".mp4"

            elif "video/webm" in content_type:
                ext = ".webm"

            elif "image/jpeg" in content_type:
                ext = ".jpg"

            elif "image/png" in content_type:
                ext = ".png"

            elif "image/webp" in content_type:
                ext = ".webp"

            elif "image/gif" in content_type:
                ext = ".gif"

            elif "audio/mpeg" in content_type:
                ext = ".mp3"

            else:
                ext = ".mp4"

        file_path = (
            folder
            / f"{filename_prefix}{ext}"
        )

        total = 0

        with open(
            file_path,
            "wb",
        ) as output:

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):

                if not chunk:
                    continue

                total += len(chunk)

                if total > (
                    500 * 1024 * 1024
                ):

                    raise RuntimeError(
                        "Remote file is too large."
                    )

                output.write(chunk)

        if (
            file_path.exists()
            and file_path.stat().st_size > 100
        ):

            return file_path

    except Exception as e:

        logger.warning(
            "Direct download failed: %s",
            e,
        )

    return None


# ============================================================
# FACEBOOK MEDIA URL PARSER
# ============================================================

def clean_candidate(
    value
):

    if not value:
        return None

    value = html_lib.unescape(
        value
    )

    value = (
        value
        .replace("\\/", "/")
        .replace("\\u0025", "%")
        .replace("\\u0026", "&")
        .replace("\\u003D", "=")
        .replace("\\u003d", "=")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
    )

    # Decode repeated URL encoding
    for _ in range(2):

        try:

            if "%2F" in value or "%3A" in value:

                value = unquote(
                    value
                )

            else:

                break

        except Exception:

            break

    value = value.strip(
        "\"' "
    )

    if not value.startswith(
        "http"
    ):

        return None

    return value


def extract_facebook_urls(
    html
):

    if not html:

        return []

    text = html_lib.unescape(
        html
    )

    # Normalize escaped JSON
    text = (
        text
        .replace("\\/", "/")
        .replace("\\u0025", "%")
        .replace("\\u0026", "&")
        .replace("\\u003D", "=")
        .replace("\\u003d", "=")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
    )

    found = []

    # ========================================================
    # Known Facebook fields
    # ========================================================

    patterns = [

        r'"hd_src"\s*:\s*"([^"]+)"',

        r'"sd_src"\s*:\s*"([^"]+)"',

        r'"playback_url"\s*:\s*"([^"]+)"',

        r'"browser_native_hd_url"\s*:\s*"([^"]+)"',

        r'"browser_native_sd_url"\s*:\s*"([^"]+)"',

        r'"progressive_url"\s*:\s*"([^"]+)"',

        r'"video_url"\s*:\s*"([^"]+)"',

        r'"videoUrl"\s*:\s*"([^"]+)"',

        r'"playable_url"\s*:\s*"([^"]+)"',

        r'"playable_url_quality_hd"\s*:\s*"([^"]+)"',

        r'"playable_url_quality_sd"\s*:\s*"([^"]+)"',

        r'"source_url"\s*:\s*"([^"]+)"',

        r'"src"\s*:\s*"([^"]+\.mp4[^"]*)"',
    ]

    for pattern in patterns:

        try:

            matches = re.findall(
                pattern,
                text,
                re.IGNORECASE,
            )

        except Exception:

            matches = []

        for value in matches:

            value = clean_candidate(
                value
            )

            if value:

                found.append(
                    value
                )

    # ========================================================
    # Generic MP4 URL detection
    # ========================================================

    generic_patterns = [

        r'https?://[^"\']+?\.mp4[^"\']*',

        r'https?://[^"\']+?\.mp4%3F[^"\']*',

        r'https?://[^"\']+?fbcdn\.net[^"\']*',

    ]

    for pattern in generic_patterns:

        try:

            matches = re.findall(
                pattern,
                text,
                re.IGNORECASE,
            )

        except Exception:

            matches = []

        for value in matches:

            value = clean_candidate(
                value
            )

            if value:

                found.append(
                    value
                )

    # ========================================================
    # JSON recursive search
    # ========================================================

    def recursive_search(
        obj
    ):

        if isinstance(
            obj,
            dict,
        ):

            for key, value in obj.items():

                key_lower = str(
                    key
                ).lower()

                if (
                    isinstance(
                        value,
                        str,
                    )
                    and (
                        "video"
                        in key_lower
                        or "playable"
                        in key_lower
                        or "progressive"
                        in key_lower
                        or "source"
                        in key_lower
                    )
                ):

                    candidate = (
                        clean_candidate(
                            value
                        )
                    )

                    if (
                        candidate
                        and (
                            ".mp4"
                            in candidate.lower()
                            or "video"
                            in candidate.lower()
                            or "fbcdn"
                            in candidate.lower()
                        )
                    ):

                        found.append(
                            candidate
                        )

                recursive_search(
                    value
                )

        elif isinstance(
            obj,
            list,
        ):

            for item in obj:

                recursive_search(
                    item
                )

    # --------------------------------------------------------
    # Extract script JSON blocks
    # --------------------------------------------------------

    scripts = re.findall(
        r"<script[^>]*>(.*?)</script>",
        text,
        re.IGNORECASE | re.DOTALL,
    )

    for script in scripts:

        script = script.strip()

        if not script:
            continue

        try:

            data = json.loads(
                script
            )

            recursive_search(
                data
            )

        except Exception:

            pass

    # ========================================================
    # Deduplicate
    # ========================================================

    final = []

    seen = set()

    for value in found:

        if (
            value
            and value not in seen
        ):

            seen.add(value)

            final.append(
                value
            )

    return final


# ============================================================
# FACEBOOK HTML
# ============================================================

def facebook_html_download(
    url,
    folder,
):

    logger.info(
        "Facebook HTML fallback."
    )

    session = create_session()

    # ========================================================
    # URLs to try
    # ========================================================

    urls = [
        url
    ]

    parsed = urlsplit(
        url
    )

    path = parsed.path

    # --------------------------------------------------------
    # www.facebook.com -> m.facebook.com
    # --------------------------------------------------------

    if "www.facebook.com" in parsed.netloc:

        mobile = urlunsplit(
            (
                parsed.scheme,
                "m.facebook.com",
                parsed.path,
                parsed.query,
                "",
            )
        )

        urls.append(
            mobile
        )

    # --------------------------------------------------------
    # www -> mbasic
    # --------------------------------------------------------

    if "www.facebook.com" in parsed.netloc:

        mbasic = urlunsplit(
            (
                parsed.scheme,
                "mbasic.facebook.com",
                parsed.path,
                parsed.query,
                "",
            )
        )

        urls.append(
            mbasic
        )

    # ========================================================
    # Try each page
    # ========================================================

    for page_url in urls:

        try:

            logger.info(
                "Facebook page: %s",
                page_url,
            )

            response = session.get(
                page_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept":
                        "text/html,"
                        "application/xhtml+xml,"
                        "*/*;q=0.8",
                },
                timeout=30,
                allow_redirects=True,
            )

            logger.info(
                "HTTP %s -> %s",
                response.status_code,
                response.url,
            )

            if response.status_code != 200:
                continue

            page = response.text

            # ------------------------------------------------
            # Check login
            # ------------------------------------------------

            if (
                "/login/" in response.url.lower()
            ):

                logger.warning(
                    "Facebook returned login page."
                )

                continue

            # ------------------------------------------------
            # Extract media
            # ------------------------------------------------

            media_urls = (
                extract_facebook_urls(
                    page
                )
            )

            logger.info(
                "Found %d Facebook candidate URLs.",
                len(media_urls),
            )

            # ------------------------------------------------
            # Download candidates
            # ------------------------------------------------

            for index, media_url in enumerate(
                media_urls,
                start=1,
            ):

                file_path = (
                    direct_download(
                        media_url,
                        folder,
                        referer=response.url,
                        filename_prefix=(
                            f"facebook_{index}_"
                        ),
                    )
                )

                if file_path:

                    logger.info(
                        "Facebook direct media downloaded."
                    )

                    return [
                        file_path
                    ]

        except Exception as e:

            logger.warning(
                "Facebook page error: %s",
                e,
            )

    return None


# ============================================================
# FACEBOOK YT-DLP
# ============================================================

def facebook_ytdlp(
    url,
    folder,
):

    output = str(
        folder
        / "facebook_%(id)s.%(ext)s"
    )

    cookie_file = (
        create_cookie_file()
    )

    options = {

        "format":
            "bv*+ba/b",

        "outtmpl":
            output,

        "noplaylist":
            True,

        "quiet":
            False,

        "no_warnings":
            False,

        "retries":
            2,

        "fragment_retries":
            2,

        "socket_timeout":
            30,

        "merge_output_format":
            "mp4",

        "http_headers":
            {
                "User-Agent":
                    USER_AGENT,
            },
    }

    if cookie_file:

        options[
            "cookiefile"
        ] = cookie_file

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            ydl.extract_info(
                url,
                download=True,
            )

        files = find_media_files(
            folder
        )

        if files:

            return files

    except Exception as e:

        logger.warning(
            "Facebook yt-dlp failed: %s",
            e,
        )

    return None


# ============================================================
# FACEBOOK DOWNLOAD
# ============================================================

def download_facebook(
    url,
    folder,
):

    # --------------------------------------------------------
    # FIRST: HTML fallback
    # --------------------------------------------------------

    files = (
        facebook_html_download(
            url,
            folder,
        )
    )

    if files:

        return files, None

    # --------------------------------------------------------
    # SECOND: yt-dlp
    # --------------------------------------------------------

    files = (
        facebook_ytdlp(
            url,
            folder,
        )
    )

    if files:

        return files, None

    return (
        None,
        "Facebook did not expose downloadable media "
        "to the bot. The post may require login, "
        "be private/restricted, or Facebook may "
        "have changed its response format."
    )


# ============================================================
# GENERAL YT-DLP
# ============================================================

def download_ytdlp(
    url,
    folder,
):

    output = str(
        folder
        / "%(autonumber)03d_%(id)s.%(ext)s"
    )

    options = {

        "format":
            "bv*+ba/b",

        "outtmpl":
            output,

        "noplaylist":
            False,

        "quiet":
            False,

        "no_warnings":
            False,

        "retries":
            3,

        "fragment_retries":
            3,

        "socket_timeout":
            30,

        "concurrent_fragment_downloads":
            4,

        "merge_output_format":
            "mp4",

        "http_headers":
            {
                "User-Agent":
                    USER_AGENT,

                "Accept":
                    "*/*",

                "Accept-Language":
                    "en-US,en;q=0.9",
            },
    }

    if is_instagram(url):

        cookie_file = (
            create_cookie_file()
        )

        if cookie_file:

            options[
                "cookiefile"
            ] = cookie_file

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            ydl.extract_info(
                url,
                download=True,
            )

        files = find_media_files(
            folder
        )

        if files:

            return (
                files,
                None,
            )

        return (
            None,
            "No media file was produced.",
        )

    except Exception as e:

        return (
            None,
            str(e),
        )


# ============================================================
# UNIVERSAL DOWNLOAD
# ============================================================

def download_media(url):

    url = normalize_url(
        url
    )

    folder = Path(
        tempfile.mkdtemp(
            prefix="download_",
            dir=BASE_DIR,
        )
    )

    logger.info(
        "URL: %s",
        url,
    )

    # ========================================================
    # DIRECT MEDIA
    # ========================================================

    if is_direct_media_url(url):

        file_path = (
            direct_download(
                url,
                folder,
                referer=url,
                filename_prefix="direct_",
            )
        )

        if file_path:

            return (
                [file_path],
                folder,
                None,
            )

    # ========================================================
    # FACEBOOK
    # ========================================================

    if is_facebook(url):

        files, error = (
            download_facebook(
                url,
                folder,
            )
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
            error,
        )

    # ========================================================
    # EVERYTHING ELSE
    # ========================================================

    files, error = (
        download_ytdlp(
            url,
            folder,
        )
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
        error,
    )


# ============================================================
# FFPROBE
# ============================================================

def get_probe(
    file_path
):

    try:

        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
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


def get_streams(
    file_path
):

    data = get_probe(
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
            x
            for x in streams
            if x.get(
                "codec_type"
            ) == "video"
        ),
        None,
    )

    audio = next(
        (
            x
            for x in streams
            if x.get(
                "codec_type"
            ) == "audio"
        ),
        None,
    )

    return video, audio


# ============================================================
# CONVERT VIDEO
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
            "No video stream.",
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

    except Exception as e:

        return (
            None,
            str(e),
        )

    if result.returncode != 0:

        return (
            None,
            result.stderr[-3000:],
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
            "Audio was lost.",
        )

    return (
        output_file,
        None,
    )


# ============================================================
# MEDIA TYPE
# ============================================================

def media_type(
    file
):

    ext = (
        Path(file)
        .suffix
        .lower()
    )

    if ext in VIDEO_EXTENSIONS:
        return "video"

    if ext in AUDIO_EXTENSIONS:
        return "audio"

    if ext in IMAGE_EXTENSIONS:
        return "image"

    if ext == ".svg":
        return "svg"

    if ext == ".gif":
        return "gif"

    video, audio = get_streams(
        file
    )

    if video:
        return "video"

    if audio:
        return "audio"

    return "document"


# ============================================================
# SEND VIDEO
# ============================================================

async def send_video(
    update,
    file
):

    video, audio = get_streams(
        file
    )

    if not video:

        return False, (
            "No video stream."
        )

    codec = video.get(
        "codec_name"
    )

    pix_fmt = video.get(
        "pix_fmt"
    )

    height = int(
        video.get(
            "height"
        ) or 0
    )

    needs_conversion = (
        codec != "h264"
        or pix_fmt != "yuv420p"
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

            return False, error

        final_file = converted

    if (
        final_file.stat().st_size
        > MAX_FILE_SIZE
    ):

        return False, (
            "Video is too large."
        )

    _, final_audio = get_streams(
        final_file
    )

    if audio and not final_audio:

        return False, (
            "The final video has no audio."
        )

    with open(
        final_file,
        "rb",
    ) as video_file:

        await update.message.reply_video(
            video=video_file,
            supports_streaming=True,
            caption="🎬 Done",
        )

    return True, None


# ============================================================
# SEND MEDIA
# ============================================================

async def send_media(
    update,
    file
):

    kind = media_type(
        file
    )

    logger.info(
        "Sending %s: %s",
        kind,
        file.name,
    )

    if kind == "video":

        return await send_video(
            update,
            file,
        )

    if kind == "image":

        if file.stat().st_size <= (
            10 * 1024 * 1024
        ):

            with open(
                file,
                "rb",
            ) as f:

                await update.message.reply_photo(
                    photo=f
                )

        else:

            with open(
                file,
                "rb",
            ) as f:

                await update.message.reply_document(
                    document=f
                )

        return True, None

    if kind == "gif":

        with open(
            file,
            "rb",
        ) as f:

            await update.message.reply_animation(
                animation=f
            )

        return True, None

    if kind == "svg":

        with open(
            file,
            "rb",
        ) as f:

            await update.message.reply_document(
                document=f,
                filename=file.name,
            )

        return True, None

    if kind == "audio":

        with open(
            file,
            "rb",
        ) as f:

            await update.message.reply_audio(
                audio=f
            )

        return True, None

    with open(
        file,
        "rb",
    ) as f:

        await update.message.reply_document(
            document=f
        )

    return True, None


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
        ) as f:

            return await update.message.reply_animation(
                animation=f,
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

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "👋 Universal Media Downloader\n\n"

        "Send me a link.\n\n"

        "🎬 Video\n"
        "🎵 Audio\n"
        "🖼️ Image\n"
        "🎨 SVG\n"
        "🎞️ GIF\n"
        "📚 Carousel\n\n"

        "Instagram • Facebook • YouTube • "
        "TikTok • X/Twitter • many other sites"
    )


# ============================================================
# MESSAGE
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
            "❌ Send a valid media link."
        )

        return

    status = await send_status(
        update
    )

    folder = None

    try:

        files, folder, error = (
            await asyncio.to_thread(
                download_media,
                url,
            )
        )

        if error:

            try:
                await status.delete()
            except Exception:
                pass

            if is_facebook(url):

                await update.message.reply_text(
                    "❌ Facebook could not provide "
                    "downloadable media.\n\n"

                    "The bot tried:\n"
                    "• Direct media extraction\n"
                    "• Mobile Facebook page\n"
                    "• Basic Facebook page\n"
                    "• yt-dlp\n\n"

                    "Facebook may require login or "
                    "the post may be restricted."
                )

            else:

                await update.message.reply_text(
                    "❌ Download failed.\n\n"
                    f"{error[-2500:]}"
                )

            return

        try:
            await status.delete()
        except Exception:
            pass

        sent = 0

        for file in files:

            if not file.exists():
                continue

            try:

                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id,
                    action=ChatAction.UPLOAD_DOCUMENT,
                )

                ok, error = (
                    await send_media(
                        update,
                        file,
                    )
                )

                if ok:

                    sent += 1

                elif error:

                    await update.message.reply_text(
                        "⚠️ " + error
                    )

            except Exception as e:

                logger.exception(
                    "Send error"
                )

                await update.message.reply_text(
                    "⚠️ Could not send media:\n"
                    f"{str(e)[:1000]}"
                )

        if sent == 0:

            await update.message.reply_text(
                "❌ Media was found but "
                "could not be sent."
            )

        elif len(files) > 1:

            await update.message.reply_text(
                f"✅ {sent} media files sent."
            )

    except Exception as e:

        logger.exception(
            "Handler error"
        )

        try:
            await status.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Error:\n"
            f"{str(e)[:2500]}"
        )

    finally:

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
            "BOT_TOKEN is missing."
        )

    create_lightning_gif()

    logger.info(
        "===================================="
    )

    logger.info(
        "Universal Media Downloader"
    )

    logger.info(
        "yt-dlp: %s",
        yt_dlp.version.__version__,
    )

    logger.info(
        "FFmpeg available: %s",
        shutil.which("ffmpeg")
        is not None,
    )

    logger.info(
        "Cookies configured: %s",
        bool(
            SOCIAL_COOKIES_B64
        ),
    )

    logger.info(
        "===================================="
    )

    app = (
        Application.builder()
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
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.CaptionRegex(
                r"https?://"
            ),
            handle_message,
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "Bot running..."
    )

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
