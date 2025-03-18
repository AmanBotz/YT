import os
import io
import asyncio
import shutil
import time
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
import yt_dlp
import ffmpeg
from pyrogram.errors import MessageNotModified

# Global variable to hold the main event loop.
MAIN_LOOP = None

# Global asynchronous lock for download/disk operations.
download_lock = asyncio.Lock()
# Store last progress update timestamp per message.
progress_last_update = {}
# Store user-specific cookies file paths.
user_cookies = {}
# Map unique tokens (32-character hex) to download request details.
download_requests = {}

# Provided API credentials (API_ID as integer)
API_ID = 23288918
API_HASH = "fd2b1b2e0e6b2addf6e8031f15e511f2"
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN_HERE"

# Owner's Telegram ID (as integer) and default cookies file path.
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DEFAULT_COOKIE_FILE = os.getenv("DEFAULT_COOKIE")  # e.g., "cookies/owner_cookies.txt"

app = Client("yt_dlp_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------------------------
# Health Check Server
# ---------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
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
    total, used, free = shutil.disk_usage("/")
    return free >= required_bytes

def to_small_caps(text):
    small_caps_map = {
        'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ',
        'f': 'ꜰ', 'g': 'ɢ', 'h': 'ʜ', 'i': 'ɪ', 'j': 'ᴊ',
        'k': 'ᴋ', 'l': 'ʟ', 'm': 'ᴍ', 'n': 'ɴ', 'o': 'ᴏ',
        'p': 'ᴘ', 'q': 'ǫ', 'r': 'ʀ', 's': 's', 't': 'ᴛ',
        'u': 'ᴜ', 'v': 'v', 'w': 'ᴡ', 'x': 'x', 'y': 'ʏ', 'z': 'ᴢ'
    }
    return "".join(small_caps_map.get(ch.lower(), ch) for ch in text)

async def safe_edit_text(message, text):
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML)
    except MessageNotModified:
        pass
    except Exception:
        pass

def progress_callback(current, total, message, action="Downloading"):
    now = time.time()
    msg_id = getattr(message, "message_id", None) or getattr(message, "id", None) or id(message)
    if msg_id not in progress_last_update or (now - progress_last_update[msg_id]) > 10:
        progress_last_update[msg_id] = now
        percent = (current / total) * 100 if total else 0
        bar = "🔵" * int(percent // 10) + "⚪" * (10 - int(percent // 10))
        coro = safe_edit_text(message, f"{action}... {bar} {percent:.2f}%")
        MAIN_LOOP.call_soon_threadsafe(asyncio.create_task, coro)

def get_formats(url, cookie_file=None):
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
        "You can set your own cookies with <b>/setcookies</b> if needed; otherwise, the default cookies will be used.",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("setcookies"))
async def set_cookies(client, message):
    user_id = message.from_user.id
    if len(message.command) > 1:
        cookie_text = message.text.split(None, 1)[1]
        if not os.path.exists("cookies"):
            os.makedirs("cookies")
        cookie_file = f"cookies/cookies_{user_id}.txt"
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write(cookie_text)
        user_cookies[user_id] = cookie_file
        await message.reply_text("Your cookies have been set.", parse_mode=ParseMode.HTML)
    elif message.document:
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
        if not check_disk_space(100 * 1024 * 1024):
            await message.reply_text("System busy with downloads. Please wait a moment ⏳.", parse_mode=ParseMode.HTML)
            return

    cookie_file = user_cookies.get(message.from_user.id, DEFAULT_COOKIE_FILE)
    result, error = get_formats(url, cookie_file=cookie_file)
    if error:
        if "login" in error.lower() or "authorization" in error.lower():
            await message.reply_text(
                "This URL requires login/authorization. Please set your cookies with /setcookies or provide a valid URL.",
                parse_mode=ParseMode.HTML
            )
        else:
            await message.reply_text(f"Error: {error}", parse_mode=ParseMode.HTML)
        return

    formats = result["formats"]
    title = result["title"]

    if not formats:
        await message.reply_text("No available formats found.", parse_mode=ParseMode.HTML)
        return

    # Limit to the first 10 formats.
    formats = formats[:10]

    # Build inline keyboard with two buttons per row.
    buttons = []
    row = []
    for i, fmt in enumerate(formats):
        token = uuid.uuid4().hex  # 32-character token
        download_requests[token] = {
            "format_id": fmt["format_id"],
            "url": url,
            "cookie_file": cookie_file
        }
        label = f"{fmt['ext']} | {fmt['resolution']} | {fmt['filesize_mb']}MB"
        row.append(InlineKeyboardButton(label, callback_data=token))
        if (i + 1) % 2 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    reply_markup = InlineKeyboardMarkup(buttons)
    await message.reply_text(f"Select format for <b>{title}</b>:",
                             reply_markup=reply_markup,
                             parse_mode=ParseMode.HTML)

@app.on_callback_query(filters.create(lambda _, __, query: len(query.data) == 32))
async def download_format(client, callback_query):
    token = callback_query.data
    req = download_requests.pop(token, None)
    if not req:
        await callback_query.answer("Request expired or invalid.")
        return

    format_id = req["format_id"]
    url = req["url"]
    cookie_file = req["cookie_file"]

    await callback_query.answer("Download started.")
    progress_message = await callback_query.message.reply_text("Starting download... ⏳", parse_mode=ParseMode.HTML)

    async with download_lock:
        out_template = "downloads/%(id)s.%(ext)s"
        ydl_opts = {
            "format": format_id,
            "outtmpl": out_template,
            "progress_hooks": [lambda d: progress_callback(d.get("downloaded_bytes", 0),
                                                             d.get("total_bytes", 1),
                                                             progress_message,
                                                             action="Downloading")],
            "quiet": True,
            "no_warnings": True,
        }
        if cookie_file:
            ydl_opts["cookiefile"] = cookie_file
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            await progress_message.edit_text(f"Error during download: {str(e)}", parse_mode=ParseMode.HTML)
            return

    file_path = yt_dlp.YoutubeDL(ydl_opts).prepare_filename(info)
    try:
        probe = ffmpeg.probe(file_path)
        duration_ffmpeg = float(probe["format"]["duration"])
    except Exception:
        duration_ffmpeg = 0
    duration_info = info.get("duration", 0)
    duration_sec = duration_ffmpeg if duration_ffmpeg > 0 else duration_info
    duration_str = time.strftime('%H:%M:%S', time.gmtime(duration_sec))
    
    thumbnail_path = f"{file_path}.jpg"
    try:
        # Extract a thumbnail at half the video duration.
        (
            ffmpeg
            .input(file_path, ss=duration_sec/2)
            .filter("scale", 320, -1)
            .output(thumbnail_path, vframes=1)
            .run(quiet=True, overwrite_output=True)
        )
    except Exception as e:
        await progress_message.edit_text(f"Error processing media: {str(e)}", parse_mode=ParseMode.HTML)
        return

    # Load thumbnail into a BytesIO object if valid.
    thumb_bytes = None
    if os.path.exists(thumbnail_path) and os.path.getsize(thumbnail_path) > 0:
        try:
            with open(thumbnail_path, "rb") as f:
                thumb_data = f.read()
            if thumb_data:
                thumb_bytes = io.BytesIO(thumb_data)
                thumb_bytes.name = os.path.basename(thumbnail_path)
        except Exception:
            thumb_bytes = None

    filesize_bytes = info.get("filesize") or info.get("filesize_approx") or 0
    filesize_mb = f"{round(filesize_bytes / (1024*1024), 2)}MB" if filesize_bytes else "Unknown"
    resolution = info.get("resolution") or (f"{info.get('height', 'NA')}p" if info.get("height") else "audio")
    caption = f"{info.get('title', 'No Title')}\n"
    caption += f"<pre>> SIZE: {filesize_mb} | QUALITY: {resolution} | DURATION: {duration_str}</pre>"

    await progress_message.edit_text("Uploading... ⏳", parse_mode=ParseMode.HTML)
    try:
        if info.get("ext") in ["mp3", "m4a", "webm"]:
            if thumb_bytes:
                await client.send_audio(
                    chat_id=callback_query.message.chat.id,
                    audio=file_path,
                    thumb=thumb_bytes,
                    caption=caption,
                    supports_streaming=True,
                    progress=lambda current, total: progress_callback(current, total, progress_message, action="Uploading")
                )
                thumb_bytes.close()
            else:
                await client.send_audio(
                    chat_id=callback_query.message.chat.id,
                    audio=file_path,
                    caption=caption,
                    supports_streaming=True,
                    progress=lambda current, total: progress_callback(current, total, progress_message, action="Uploading")
                )
        else:
            if thumb_bytes:
                await client.send_video(
                    chat_id=callback_query.message.chat.id,
                    video=file_path,
                    thumb=thumb_bytes,
                    caption=caption,
                    supports_streaming=True,
                    progress=lambda current, total: progress_callback(current, total, progress_message, action="Uploading")
                )
                thumb_bytes.close()
            else:
                await client.send_video(
                    chat_id=callback_query.message.chat.id,
                    video=file_path,
                    caption=caption,
                    supports_streaming=True,
                    progress=lambda current, total: progress_callback(current, total, progress_message, action="Uploading")
                )
        await progress_message.delete()
    except Exception as e:
        await progress_message.edit_text(f"Error during upload: {str(e)}", parse_mode=ParseMode.HTML)
    finally:
        try:
            os.remove(file_path)
            if os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)
        except Exception:
            pass

if __name__ == "__main__":
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
    if not os.path.exists("cookies"):
        os.makedirs("cookies")
    MAIN_LOOP = asyncio.get_event_loop()
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    app.run()
