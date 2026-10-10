
# Telegram Media Downloader Bot

A Python Telegram bot for downloading supported media URLs and returning
files through Telegram. Uses yt-dlp and supports optional authorized
cookies and Docker deployment.

## Features

- Telegram commands: /start, /help, /status
- Downloads media from sites supported by the installed yt-dlp version
- Supports direct file URLs
- Handles uploaded documents, photos, videos, audio and voice messages
- Optional Base64-encoded Netscape cookies.txt files
- Preserves available audio/video streams without intentional re-encoding
- Configurable download limit
- Docker image includes FFmpeg

## Files

- bot.py
- Dockerfile
- README.md
- requirements.txt
- .env.example
- .gitignore

## Requirements

- Python 3.12 or compatible
- Telegram bot token from @BotFather
- FFmpeg for non-Docker environments
- Internet access

## Run locally on macOS or Linux

Create a virtual environment:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Set your bot token:

    export BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"

Start the bot:

    python bot.py

## Windows PowerShell

    py -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    $env:BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
    python bot.py

## Docker

Build:

    docker build -t telegram-media-bot .

Run:

    docker run --rm \
      -e BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN" \
      telegram-media-bot

Use your hosting provider's environment-variable or secret manager
for production credentials.

## Configuration

- BOT_TOKEN: Required Telegram bot token.
- MAX_FILE_MB: Maximum download size; defaults to 45.
- DOWNLOAD_TIMEOUT: Download timeout in seconds; defaults to 180.
- LOG_LEVEL: Logging level; defaults to INFO.
- WORK_DIR: Temporary working directory.

## Optional cookies

Cookie variables use Base64-encoded Netscape cookies.txt files.
Only use accounts and cookies you are authorized to use.

Supported variable names:

- YOUTUBE_COOKIES_B64
- INSTAGRAM_COOKIES_B64
- FACEBOOK_COOKIES_B64
- SNAPCHAT_COOKIES_B64
- X_COOKIES_B64
- LINKEDIN_COOKIES_B64
- PINTEREST_COOKIES_B64
- REDDIT_COOKIES_B64
- TELEGRAM_COOKIES_B64
- WHATSAPP_COOKIES_B64
- MESSENGER_COOKIES_B64
- SHARECHAT_COOKIES_B64
- MOJ_COOKIES_B64
- JOSH_COOKIES_B64
- CHINGARI_COOKIES_B64
- ARATTAI_COOKIES_B64
- SANDES_COOKIES_B64
- KOO_COOKIES_B64

On macOS/Linux, encode an exported cookies.txt file:

    base64 < cookies.txt | tr -d '\n'

Add the result to the matching environment variable in your hosting
dashboard. Never commit real tokens or cookies to GitHub.

Cookies do not guarantee platform support or successful downloads.
Some services require official APIs or do not expose downloadable media.

## Supported direct file extensions

PDF, DOC, DOCX, TXT, RTF, JPG, JPEG, PNG, GIF, SVG, HEIC, HEIF,
MP3, WAV, M4A, MP4, MOV, AVI, MKV, WEBM, ZIP, RAR, APK, IPA, EXE, DMG.

The direct-file handler expects an actual file URL, not a webpage that
merely displays a file.

## Social platform support

YouTube, YouTube Shorts, Instagram, Facebook, X, Reddit, Pinterest
and other sites may work when supported by the current yt-dlp release.

WhatsApp, Snapchat, LinkedIn, Messenger, Telegram and regional apps
may require other tools, official APIs, or may not be supported.

Private, DRM-protected, paywalled, or access-controlled content is not
bypassed.

## Troubleshooting

### No downloadable formats

Update yt-dlp:

    pip install --upgrade yt-dlp

Confirm the URL works in a browser and that the site is supported.

### Invalid cookies

Export a fresh Netscape-format cookie file from your own account.
Encode it and update the relevant hosting secret.

### Missing original audio

Some source videos have no audio. This bot requests the best available
video and audio streams, but cannot recover audio that the source or
extractor does not provide.

### File too large

Reduce MAX_FILE_MB or use a supported upload setup with a larger limit.
Telegram and the hosting provider may enforce their own limits.

## Security

Keep bot tokens and cookies private. Rotate them if exposed.
Download only media you own or are authorized to save.
