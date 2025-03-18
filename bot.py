import os
import asyncio
import shutil
import subprocess
from pyrofork import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp
import ffmpeg

# Global asynchronous lock for download/disk operations.
download_lock = asyncio.Lock()

# Provided API credentials (API_ID as integer)
API_ID = 23288918
API_HASH = "fd2b1b2e0e6b2addf6e8031f15e511f2"
# Set your bot token here or via an environment variable.
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN_HERE"

app = Client("yt_dlp_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------------------------
# Utility Functions
# ---------------------------
def check_disk_space(required_bytes):
    """Check if available disk space is at least required_bytes."""
    total, used, free = shutil.disk_usage("/")
    return free >= required_bytes

def to_small_caps(text):
    """Convert letters to small caps using available Unicode equivalents."""
    small_caps_map = {
        'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ',
        'f': 'ꜰ', 'g': 'ɢ', 'h': 'ʜ', 'i': 'ɪ', 'j': 'ᴊ',
        'k': 'ᴋ', 'l': 'ʟ', 'm': 'ᴍ', 'n': 'ɴ', 'o': 'ᴏ',
        'p': 'ᴘ', 'q': 'ǫ', 'r': 'ʀ', 's': 's', 't': 'ᴛ',
        'u': 'ᴜ', 'v': 'v', 'w': 'ᴡ', 'x': 'x', 'y': 'ʏ', 'z': 'ᴢ'
    }
    return "".join(small_caps_map.get(ch.lower(), ch) for ch in text)

def progress_callback(current, total, message, action="Downloading"):
    """Update progress message with an emoji-based progress bar."""
    percent = (current / total) * 100 if total else 0
    bar = "🔵" * int(percent // 10) + "⚪" * (10 - int(percent // 10))
    try:
        message.edit_text(f"{action}... {bar} {percent:.2f}%")
    except Exception:
        pass

def get_formats(url):
    """Extract video/audio formats using yt-dlp."""
    ydl_opts = {
        'skip_download': True,
        'quiet': True,
        'no_warnings': True,
    }
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
# Bot Handlers
# ---------------------------
@app.on_message(filters.command("start"))
def start(client, message):
    message.reply_text(
        "Welcome to the yt-dlp Bot 🤖!\n"
        "Send me a video URL from any supported site, then choose your desired format.\n"
        "You'll see real-time progress updates, and your media will be sent with proper metadata!"
    )

@app.on_message(filters.text & ~filters.command("start"))
def handle_url(client, message):
    url = message.text.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        message.reply_text("Please provide a valid URL.")
        return

    # Check disk space with our global lock to ensure safe operations (minimum 100MB free)
    with download_lock:
        if not check_disk_space(100 * 1024 * 1024):
            message.reply_text("System busy with downloads. Please wait a moment ⏳.")
            return

    result, error = get_formats(url)
    if error:
        if "login" in error.lower() or "authorization" in error.lower():
            message.reply_text("This URL requires login or authorization. Please provide a valid URL.")
        else:
            message.reply_text(f"Error: {error}")
        return

    formats = result["formats"]
    title = result["title"]

    # Build inline buttons for each available format including file size in MB.
    buttons = []
    for fmt in formats:
        label = f"{fmt['ext']} | {fmt['resolution']} | {fmt['filesize_mb']}MB"
        buttons.append([InlineKeyboardButton(label, callback_data=f"dl|{fmt['format_id']}|{url}")])
    reply_markup = InlineKeyboardMarkup(buttons)
    message.reply_text(f"Select format for *{title}*:", reply_markup=reply_markup, parse_mode="markdown")

@app.on_callback_query(filters.regex(r"^dl\|"))
def download_format(client, callback_query):
    data = callback_query.data.split("|")
    if len(data) < 3:
        callback_query.answer("Invalid selection.")
        return
    format_id = data[1]
    url = data[2]
    callback_query.answer("Download started.")
    progress_message = callback_query.message.reply_text("Starting download... ⏳")

    with download_lock:
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
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            progress_message.edit_text(f"Error during download: {str(e)}")
            return

    file_path = ydl.prepare_filename(info)
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
        progress_message.edit_text(f"Error processing media: {str(e)}")
        return

    filesize_bytes = info.get("filesize") or info.get("filesize_approx") or 0
    filesize_mb = f"{round(filesize_bytes / (1024*1024), 2)}MB" if filesize_bytes else "Unknown"
    resolution = info.get("resolution") or (f"{info.get('height', 'NA')}p" if info.get("height") else "audio")
    caption = f"{info.get('title', 'No Title')}\n"
    blockquote = f"> {to_small_caps('size')}: {filesize_mb} | {to_small_caps('quality')}: {resolution}"
    caption += blockquote

    progress_message.edit_text("Uploading... ⏳")
    try:
        if info.get("ext") in ["mp3", "m4a", "webm"]:
            client.send_audio(
                chat_id=callback_query.message.chat.id,
                audio=file_path,
                thumb=thumbnail_path,
                caption=caption,
                progress=lambda current, total: progress_callback(current, total, progress_message, action="Uploading")
            )
        else:
            client.send_video(
                chat_id=callback_query.message.chat.id,
                video=file_path,
                thumb=thumbnail_path,
                caption=caption,
                progress=lambda current, total: progress_callback(current, total, progress_message, action="Uploading")
            )
        progress_message.delete()
    except Exception as e:
        progress_message.edit_text(f"Error during upload: {str(e)}")
    finally:
        try:
            os.remove(file_path)
            os.remove(thumbnail_path)
        except Exception:
            pass

if __name__ == "__main__":
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
    app.run()
