def download_video(url: str, temp_dir: str) -> tuple[Path | None, str | None]:
    """
    Download a single video using yt-dlp.
    Supports Instagram authentication through an optional cookies file.
    """

    output_template = str(
        Path(temp_dir) / "download_%(id)s.%(ext)s"
    )

    is_instagram = "instagram.com" in url.lower()

    ydl_opts = {
        "outtmpl": output_template,

        "format": (
            "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/"
            "best[ext=mp4][height<=720]/"
            "best[height<=720]/"
            "best"
        ),

        "merge_output_format": "mp4",

        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "restrictfilenames": True,

        "max_filesize": MAX_FILE_SIZE,

        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 5,

        "socket_timeout": 30,

        "continuedl": True,
        "overwrites": True,

        "concurrent_fragment_downloads": 4,

        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    # Instagram sometimes requires an authenticated session.
    # Put an exported cookies.txt file in the project root.
    if is_instagram:
        cookies_file = Path("cookies.txt")

        if cookies_file.exists():
            ydl_opts["cookiefile"] = str(cookies_file)
            logging.info("Using Instagram cookies.")
        else:
            logging.warning(
                "Instagram cookies.txt not found. "
                "Attempting unauthenticated download."
            )

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                url,
                download=True,
            )

            if not info:
                return None, "yt-dlp could not extract video information."

        downloaded_file = find_downloaded_file(temp_dir)

        if not downloaded_file:
            return None, "No downloadable video file was created."

        return downloaded_file, None

    except yt_dlp.utils.DownloadError as error:
        error_text = str(error)

        logging.error(
            "yt-dlp download failed for %s: %s",
            url,
            error_text,
        )

        return None, error_text

    except Exception as error:
        logging.exception("Unexpected download error")

        return None, (
            f"{type(error).__name__}: {error}"
        )