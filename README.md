
# Universal Media Downloader — Railway

## Files
- `bot.py`: Telegram bot and download logic
- `requirements.txt`: Python dependencies
- `Dockerfile`: Python, Deno and FFmpeg build
- `.dockerignore`: excludes local environments and secrets

## Deploy
1. Put all files in the root of your GitHub repository.
2. Connect the repository to Railway.
3. Ensure Railway uses the included Dockerfile.
4. Add `BOT_TOKEN` in Railway Variables.
5. Deploy and inspect the startup logs.

The start command is `python bot.py`.

Optional YouTube authentication:
- Variable: `YOUTUBE_COOKIES_B64`
- Value: Base64-encoded Netscape-format `cookies.txt`

Treat cookies like passwords. Use only accounts and content you are
authorized to access.

YouTube can still deny requests from a hosting IP or require verification.
No code change can guarantee access to every video.
