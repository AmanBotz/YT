import os
import asyncio
import shutil
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
import yt_dlp
import ffmpeg

# Global asynchronous lock for download/disk operations.
download_lock = asyncio.Lock()
# Dictionary to store last progress update timestamp per message id.
progress_last_update = {}
# Dictionary to store user-specific cookies file paths.
user_cookies = {}

# Provided API credentials (API_ID as integer)
API_ID = 23288918
API_HASH = "fd2b1b2e0e6b2addf6e8031f15e511f2"
# Set your bot token here or via environment variable.
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN_HERE"

# Owner's Telegram ID (as integer) and default cookies file path.
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DEFAULT_COOKIE_FILE = os.getenv("DEFAULT_COOKIE")  # e.g., "cookies/owner_cookies.txt"

app = Client("yt_dlp_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------------------------
# Health Check Server
# ---------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_server():
    server_address = ("0.0.0.0", 8000)
    httpd = HTTPServer(server_address, HealthHandler)
    httpd.serve_forever()

# ---------------------------
# Utility Functions
# ---------------------------
def check_disk_space(required_bytes):
    """Check if available disk space is at least required_bytes."""
    total, used, free = shutil.disk_usage("/")
    return free >= required_bytes

def to_small_caps(text):
    """Convert letters to small caps using Unicode equivalents."""
    small_caps_map = {
        'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ',
        'f': 'ꜰ', 'g': 'ɢ', 'h': 'ʜ', 'i': 'ɪ', 'j': 'ᴊ',
        'k': 'ᴋ', 'l': 'ʟ', 'm': 'ᴍ', 'n': 'ɴ', 'o': 'ᴏ',
        'p': 'ᴘ', 'q': 'ǫ', 'r': 'ʀ', 's': 's', 't': 'ᴛ',
        'u': 'ᴜ', 'v': 'v', 'w': 'ᴡ', 'x': 'x', 'y': 'ʏ', 'z': 'ᴢ'
    }
    return "".join(small_caps_map.get(ch.lower(), ch) for ch in text)

def progress_callback(current, total, message, action="Downloading"):
    """Update progress message with an emoji progress bar, limited to one update every 10 seconds."""
    now = time.time()
    msg_id = message.message_id
    if msg_id not in progress_last_update or (now - progress_last_update[msg_id]) > 10:
        progress_last_update[msg_id] = now
        percent = (current / total) * 100 if total else 0
        bar = "🔵" * int(percent // 10) + "⚪" * (10 - int(percent // 10))
        # Schedule async edit without awaiting directly.
        asyncio.create_task(message.edit_text(f"{action}... {bar} {percent:.2f}%", parse_mode=ParseMode.HTML))

def get_formats(url, cookie_file=None):
    """Extract video/audio formats using yt-dlp, optionally using a cookies file."""
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
    }
    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return None, str(e)
    formats = info.get("formats", [])
    available = []
    for fmt in formats:
        format_id = fmt.get("format_id")
        ext = fmt.get("ext")
        resolution = fmt.get("resolution") or (f"{fmt.get('height', 'NA')}p" if fmt.get("height") else "audio")
        filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        filesize_mb = round(filesize / (1024 * 1024), 2) if filesize else "Unknown"
        available.append({
            "format_id": format_id,
            "ext": ext,
            "resolution": resolution,
            "filesize": filesize,
            "filesize_mb": filesize_mb
        })
    title = info.get("title", "Unknown Title")
    return {"formats": available, "title": title, "info": info}, None

# ---------------------------
# Bot Command Handlers
# ---------------------------
@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text(
        "Welcome to the yt-dlp Bot 🤖!<br>"
        "Use <b>/dl &lt;URL&gt;</b> to download a video from any supported site.<br>"
        "You can set your own cookies with <b>/setcookies</b> if needed, otherwise the default cookies will be used.",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("setcookies"))
async def set_cookies(client, message):
    user_id = message.from_user.id
    if len(message.command) > 1:
        # Use text provided after the command as cookie content.
        cookie_text = message.text.split(None, 1)[1]
        if not os.path.exists("cookies"):
            os.makedirs("cookies")
        cookie_file = f"cookies/cookies_{user_id}.txt"
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write(cookie_text)
        user_cookies[user_id] = cookie_file
        await message.reply_text("Your cookies have been set.", parse_mode=ParseMode.HTML)
    elif message.document:
        # Download the attached document as cookie file.
        if not os.path.exists("cookies"):
            os.makedirs("cookies")
        file_path = await message.download(file_name=f"cookies/cookies_{user_id}.txt")
        user_cookies[user_id] = file_path
        await message.reply_text("Your cookies file has been set.", parse_mode=ParseMode.HTML)
    else:
        await message.reply_text("Usage: /setcookies <cookie content> or send a cookie file.", parse_mode=ParseMode.HTML)

@app.on_message(filters.command("dl"))
async def dl_command(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: /dl <URL>", parse_mode=ParseMode.HTML)
        return
    url = parts[1].strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.reply_text("Please provide a valid URL.", parse_mode=ParseMode.HTML)
        return

    async with download_lock:
        if not check_disk_space(100 * 1024 * 1024):  # Ensure at least 100MB free
            await message.reply_text("System busy with downloads. Please wait a moment ⏳.", parse_mode=ParseMode.HTML)
            return

    # Determine which cookie file to use: user-specific if set, otherwise default.
    cookie_file = user_cookies.get(message.from_user.id, DEFAULT_COOKIE_FILE)
    result, error = get_formats(url, cookie_file=cookie_file)
    if error:
        if "login" in error.lower() or "authorization" in error.lower():
            await message.reply_text(
                "This URL requires login or authorization. Please set your cookies with /setcookies or provide a valid URL.",
                parse_mode=ParseMode.HTML
            )
        else:
            await message.reply_text(f"Error: {error}", parse_mode=ParseMode.HTML)
        return

    formats = result["formats"]
    title = result["title"]

    # Build inline buttons for each available format including file size.
    buttons = []
    for fmt in formats:
        label = f"{fmt['ext']} | {fmt['resolution']} | {fmt['filesize_mb']}MB"
        buttons.append([InlineKeyboardButton(label, callback_data=f"dl|{fmt['format_id']}|{url}")])
    reply_markup = InlineKeyboardMarkup(buttons)
    await message.reply_text(f"Select format for <b>{title}</b>:",
                             reply_markup=reply_markup,
                             parse_mode=ParseMode.HTML)

@app.on_callback_query(filters.regex(r"^dl\|"))
async def download_format(client, callback_query):
    data = callback_query.data.split("|")
    if len(data) < 3:
        await callback_query.answer("Invalid selection.")
        return
    format_id = data[1]
    url = data[2]
    await callback_query.answer("Download started.")
    progress_message = await callback_query.message.reply_text("Starting download... ⏳",
                                                                 parse_mode=ParseMode.HTML)

    # Determine cookie file for this request.
    cookie_file = user_cookies.get(callback_query.from_user.id, DEFAULT_COOKIE_FILE)

    async with download_lock:
        out_template = "downloads/%(id)s.%(ext)s"
        ydl_opts = {
            'format': format_id,
            'outtmpl': out_template,
            'progress_hooks': [lambda d: progress_callback(d.get("downloaded_bytes", 0),
                                                             d.get("total_bytes", 1),
                                                             progress_message,
                                                             action="Downloading")],
            'quiet': True,
            'no_warnings': True,
        }
        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            await progress_message.edit_text(f"Error during download: {str(e)}", parse_mode=ParseMode.HTML)
            return

    file_path = yt_dlp.YoutubeDL(ydl_opts).prepare_filename(info)
    # Process media: extract duration and generate thumbnail using ffmpeg
    try:
        probe = ffmpeg.probe(file_path)
        duration = float(probe['format']['duration'])
        thumbnail_path = f"{file_path}.jpg"
        (
            ffmpeg
            .input(file_path, ss=duration/2)
            .filter('scale', 320, -1)
            .output(thumbnail_path, vframes=1)
            .run(quiet=True, overwrite_output=True)
        )
    except Exception as e:
        await progress_message.edit_text(f"Error processing media: {str(e)}", parse_mode=ParseMode.HTML)
        return

    filesize_bytes = info.get("filesize") or info.get("filesize_approx") or 0
    filesize_mb = f"{round(filesize_bytes / (1024*1024), 2)}MB" if filesize_bytes else "Unknown"
    resolution = info.get("resolution") or (f"{info.get('height', 'NA')}p" if info.get("height") else "audio")
    caption = f"{info.get('title', 'No Title')}\n"
    blockquote = f"> {to_small_caps('size')}: {filesize_mb} | {to_small_caps('quality')}: {resolution}"
    caption += blockquote

    await progress_message.edit_text("Uploading... ⏳", parse_mode=ParseMode.HTML)
    try:
        if info.get("ext") in ["mp3", "m4a", "webm"]:
            await client.send_audio(
                chat_id=callback_query.message.chat.id,
                audio=file_path,
                thumb=thumbnail_path,
                caption=caption,
                progress=lambda current, total: progress_callback(current, total, progress_message, action="Uploading")
            )
        else:
            await client.send_video(
                chat_id=callback_query.message.chat.id,
                video=file_path,
                thumb=thumbnail_path,
                caption=caption,
                progress=lambda current, total: progress_callback(current, total, progress_message, action="Uploading")
            )
        await progress_message.delete()
    except Exception as e:
        await progress_message.edit_text(f"Error during upload: {str(e)}", parse_mode=ParseMode.HTML)
    finally:
        try:
            os.remove(file_path)
            os.remove(thumbnail_path)
        except Exception:
            pass

if __name__ == "__main__":
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
    if not os.path.exists("cookies"):
        os.makedirs("cookies")
    # Start health check server in a separate thread so Koyeb's TCP check on port 8000 passes.
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    app.run()
