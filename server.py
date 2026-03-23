"""
yt-dlp Video Download API
部署在 Railway 上，为 n8n 工作流提供视频下载服务
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
import yt_dlp
import os
import uuid
import time
import threading
import glob
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="yt-dlp API", version="1.1.0")

DOWNLOAD_DIR = "/tmp/ytdlp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_MAX_AGE = 600

# cookies.txt 和 server.py 在同一目录
COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
if os.path.exists(COOKIES_PATH):
    logger.info(f"Found cookies.txt at {COOKIES_PATH}")
else:
    logger.warning("cookies.txt not found!")
    COOKIES_PATH = None


def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
                if os.path.isfile(f) and (now - os.path.getmtime(f)) > FILE_MAX_AGE:
                    os.remove(f)
                    logger.info(f"Cleaned up: {f}")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        time.sleep(60)


threading.Thread(target=cleanup_old_files, daemon=True).start()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "yt-dlp-api",
        "version": "1.1.0",
        "cookies": COOKIES_PATH is not None,
    }


@app.post("/")
async def parse_video(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"status": "error", "error": {"code": "invalid_json"}, "text": "Invalid JSON body"},
            status_code=400,
        )

    url = data.get("url", "").strip()
    if not url:
        return JSONResponse(
            {"status": "error", "error": {"code": "no_url"}, "text": "No URL provided"},
            status_code=400,
        )

    quality = data.get("videoQuality", "1080")
    file_id = str(uuid.uuid4())[:12]
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

    is_douyin = "douyin.com" in url
    is_tiktok = "tiktok.com" in url

    ydl_opts = {
        "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 3,
        "concurrent_fragment_downloads": 4,
        "max_filesize": 500 * 1024 * 1024,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    }

    if (is_douyin or is_tiktok) and COOKIES_PATH:
        ydl_opts["cookiefile"] = COOKIES_PATH
        logger.info(f"Using cookies for {'Douyin' if is_douyin else 'TikTok'}")

    if is_douyin:
        ydl_opts["http_headers"]["Referer"] = "https://www.douyin.com/"
    elif is_tiktok:
        ydl_opts["http_headers"]["Referer"] = "https://www.tiktok.com/"

    logger.info(f"Processing URL: {url}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename_on_disk = ydl.prepare_filename(info)

            if not os.path.exists(filename_on_disk):
                base = os.path.splitext(filename_on_disk)[0]
                for ext in [".mp4", ".webm", ".mkv", ".m4a", ".mp3"]:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        filename_on_disk = candidate
                        break

            if not os.path.exists(filename_on_disk):
                return JSONResponse({
                    "status": "error",
                    "error": {"code": "file_not_found"},
                    "text": "Download completed but file not found on disk",
                })

            title = info.get("title", "video")
            ext = os.path.splitext(filename_on_disk)[1] or ".mp4"
            safe_title = "".join(
                c for c in title
                if c.isalnum() or c in " _-" or ('\u4e00' <= c <= '\u9fa5')
            ).strip()[:80]
            if not safe_title:
                safe_title = "video"

            actual_filename = os.path.basename(filename_on_disk)
            file_size = os.path.getsize(filename_on_disk)

            base_url = str(request.base_url).rstrip("/")
            download_url = f"{base_url}/file/{actual_filename}"

            logger.info(f"Success: {safe_title}{ext} ({file_size} bytes)")

            return JSONResponse({
                "status": "tunnel",
                "url": download_url,
                "filename": f"{safe_title}{ext}",
            })

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"Download error for {url}: {error_msg}")
        return JSONResponse({
            "status": "error",
            "error": {"code": "download_failed"},
            "text": error_msg[:200],
        })
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Unexpected error for {url}: {error_msg}")
        return JSONResponse({
            "status": "error",
            "error": {"code": "unexpected_error"},
            "text": error_msg[:200],
        })


@app.get("/file/{filename}")
async def serve_file(filename: str):
    safe = os.path.basename(filename)
    path = os.path.join(DOWNLOAD_DIR, safe)
    if os.path.exists(path):
        return FileResponse(path, media_type="video/mp4", filename=safe)
    return JSONResponse({"error": "file_not_found"}, status_code=404)
