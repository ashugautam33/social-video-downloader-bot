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
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
COOKIE_B64 = os.getenv("INSTAGRAM_COOKIES_B64")
MAX_SIZE = 45 * 1024 * 1024
MAX_DIM = 720

HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.watch")
EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".ts"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def get_url(text):
    m = re.search(r"https?://[^\s]+", text or "", re.I)
    return m.group(0).rstrip(").,]}>'\"") if m else None


def supported(url):
    return any(x in url.lower() for x in HOSTS)


def instagram(url):
    return "instagram.com" in url.lower()


def cookies(temp):
    if COOKIE_B64:
        try:
            p = Path(temp) / "cookies.txt"
            p.write_bytes(base64.b64decode(COOKIE_B64, validate=True))
            if p.stat().st_size:
                return str(p)
        except Exception:
            pass
    p = Path("cookies.txt")
    return str(p) if p.exists() and p.stat().st_size else None


def ffmpeg():
    try:
        return "ffmpeg" if subprocess.run(
            ["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10
        ).returncode == 0 else None
    except Exception:
        return None


def media_info(src):
    result = {"video": False, "audio": False, "w": 0, "h": 0, "codec": ""}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams",
             "-of", "default=noprint_wrappers=1", str(src)],
            capture_output=True, text=True, timeout=30
        )
        stream = {}
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                stream[k] = v

        # Use a second probe for reliable stream detection.
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "default=noprint_wrappers=1", str(src)],
            capture_output=True, text=True, timeout=30
        )
        for line in r.stdout.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "codec_name":
                result["codec"] = v
            elif k == "width":
                result["w"] = int(v)
            elif k == "height":
                result["h"] = int(v)
        result["video"] = bool(r.stdout.strip())

        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name",
             "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=30
        )
        result["audio"] = bool(r.stdout.strip())
    except Exception:
        pass
    return result


def convert(src, temp):
    exe = ffmpeg()
    if not exe:
        return None, "FFmpeg is not installed."

    info = media_info(src)
    if not info["video"]:
        return None, "Downloaded file has no video stream."

    out = Path(temp) / "final.mp4"

    # Small H.264 MP4: remux without re-encoding.
    if (src.suffix.lower() == ".mp4" and info["codec"] == "h264"
            and info["w"] and info["h"] and max(info["w"], info["h"]) <= MAX_DIM):
        cmd = [exe, "-y", "-i", str(src), "-map", "0:v:0", "-c:v", "copy"]
        if info["audio"]:
            cmd += ["-map", "0:a:0?", "-c:a", "copy"]
        cmd += ["-movflags", "+faststart", str(out)]
    else:
        vf = "scale=w='min(720,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease"
        cmd = [
            exe, "-y", "-i", str(src),
            "-map", "0:v:0", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "26", "-pix_fmt", "yuv420p", "-r", "30"
        ]
        if info["audio"]:
            cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-ac", "2"]
        cmd += ["-movflags", "+faststart", str(out)]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode or not out.exists() or out.stat().st_size == 0:
            return None, "FFmpeg failed:\n" + r.stderr[-2500:]
        return out, None
    except subprocess.TimeoutExpired:
        return None, "FFmpeg conversion timed out."
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def ydl_options(temp, cookie=None):
    o = {
        "outtmpl": str(Path(temp) / "video_%(id)s.%(ext)s"),
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "max_filesize": MAX_SIZE,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "http_headers": {"User-Agent": UA},
        "quiet": True,
    }
    if cookie:
        o["cookiefile"] = cookie
    if "instagram.com" in (o.get("http_headers", {}).get("Referer", "")):
        pass
    return o


def find_video(temp):
    files = [p for p in Path(temp).iterdir()
             if p.is_file() and p.suffix.lower() in EXTS and p.stat().st_size > 0]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def download(url, temp):
    attempts = []
    opts = ydl_options(temp)

    if instagram(url):
        opts["http_headers"].update({
            "Referer": "https://www.instagram.com/",
            "Origin": "https://www.instagram.com/"
        })

    for cookie in ([None, cookies(temp)] if instagram(url) else [None]):
        # Clean old downloads so another attempt cannot select the wrong file.
        for p in Path(temp).iterdir():
            if p.is_file() and p.suffix.lower() in EXTS:
                try: p.unlink()
                except OSError: pass

        if cookie:
            opts["cookiefile"] = cookie
        else:
            opts.pop("cookiefile", None)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
            src = find_video(temp)
            if src:
                return convert(src, temp)
            attempts.append("No video file was created.")
        except Exception as e:
            attempts.append(str(e))

        if not instagram(url):
            break

    error = "\n\n".join(attempts)
    low = error.lower()

    if "no video formats found" in low:
        error = "No downloadable video format was provided by the platform."
    elif any(x in low for x in ("login required", "authentication", "sign in", "cookies")):
        error = "Authentication is required. Your Instagram cookies may be expired."
    elif "private" in low:
        error = "This video is private or your account cannot access it."
    elif "429" in low or "rate limit" in low:
        error = "The platform is rate-limiting the bot. Try again later."
    elif "403" in low or "forbidden" in low:
        error = "Access was denied by the platform."

    return None, error[:3500]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Social Video Downloader\n\n"
        "Send an Instagram, YouTube, Shorts, TikTok or Facebook video URL.\n\n"
        "🎬 Output: H.264 MP4 • Max 720p • Max 45 MB"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = get_url(update.message.text)
    if not url:
        await update.message.reply_text("❌ Please send a valid video URL.")
        return

    if not supported(url):
        await update.message.reply_text("❌ Unsupported platform.")
        return

    status = await update.message.reply_text("⏳ Downloading video...")
    try:
        await context.bot.send_chat_action(
            update.effective_chat.id, ChatAction.UPLOAD_VIDEO
        )

        with tempfile.TemporaryDirectory(prefix="video_") as temp:
            video, error = await asyncio.to_thread(download, url, temp)

            if not video:
                await status.edit_text("❌ Download failed.\n\n" + (error or "Unknown error."))
                return

            if video.stat().st_size > MAX_SIZE:
                await status.edit_text("❌ Final video is larger than 45 MB.")
                return

            await status.edit_text("📤 Sending video...")
            with video.open("rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption="✅ Downloaded • MP4 • H.264",
                    supports_streaming=True,
                    read_timeout=180,
                    write_timeout=180,
                    connect_timeout=30,
                    pool_timeout=30,
                )

            try:
                await status.delete()
            except Exception:
                pass

    except Exception as e:
        await status.edit_text(f"❌ Error: {type(e).__name__}: {e}")


async def error_handler(update, context):
    print("Bot error:", context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing in Railway Variables.")

    if not ffmpeg():
        print("WARNING: FFmpeg not found.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))
    app.add_error_handler(error_handler)

    print("🤖 Bot is running...", flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
