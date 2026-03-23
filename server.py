"""
yt-dlp Video Download API v3
- 抖音: 第三方 API（tikwm.com + 备用方案）
- TikTok/其他: yt-dlp
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
import yt_dlp
import os
import re
import uuid
import time
import threading
import glob
import logging
import urllib.request
import urllib.error
import urllib.parse
import json
import ssl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="yt-dlp API", version="3.0.0")

DOWNLOAD_DIR = "/tmp/ytdlp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_MAX_AGE = 600

COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
if not os.path.exists(COOKIES_PATH):
    COOKIES_PATH = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

# 忽略 SSL 证书验证（某些第三方 API 证书不规范）
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
                if os.path.isfile(f) and (now - os.path.getmtime(f)) > FILE_MAX_AGE:
                    os.remove(f)
        except Exception:
            pass
        time.sleep(60)


threading.Thread(target=cleanup_old_files, daemon=True).start()


def http_get_json(url, headers=None, timeout=15):
    """发 GET 请求，返回 JSON"""
    hdrs = {**HEADERS, **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
    return json.loads(resp.read().decode("utf-8", errors="ignore"))


def http_post_json(url, data, headers=None, timeout=15):
    """发 POST 请求，返回 JSON"""
    hdrs = {**HEADERS, "Content-Type": "application/json", **(headers or {})}
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
    return json.loads(resp.read().decode("utf-8", errors="ignore"))


def download_file(url, filepath, timeout=120):
    """下载文件到本地"""
    req = urllib.request.Request(url, headers=HEADERS)
    resp = urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)
    with open(filepath, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
    return os.path.getsize(filepath)


# ========== 第三方 API 方法 ==========

def try_tikwm(url):
    """方法1: tikwm.com API（支持抖音和TikTok）"""
    try:
        api_url = "https://www.tikwm.com/api/"
        data = http_post_json(api_url, {
            "url": url,
            "hd": 1,
        }, timeout=20)

        if data.get("code") != 0:
            return None, None, f"tikwm error: {data.get('msg', 'unknown')}"

        video_data = data.get("data", {})
        title = video_data.get("title", "douyin_video")

        # 优先用 HD
        video_url = video_data.get("hdplay") or video_data.get("play")
        if not video_url:
            return None, title, "tikwm: no video URL"

        logger.info(f"[tikwm] Got video URL for: {title[:30]}")
        return video_url, title, None

    except Exception as e:
        logger.error(f"[tikwm] Failed: {e}")
        return None, None, f"tikwm: {str(e)[:100]}"


def try_dlpanda(url):
    """方法2: 尝试 iesdouyin 国际接口"""
    try:
        # 先解析短链接获取视频 ID
        req = urllib.request.Request(url, headers=HEADERS)
        resp = urllib.request.urlopen(req, timeout=15, context=SSL_CTX)
        final_url = resp.url

        # 提取视频 ID
        video_id = None
        for pattern in [r'/video/(\d+)', r'/note/(\d+)', r'modal_id=(\d+)', r'(\d{15,})']:
            match = re.search(pattern, final_url)
            if match:
                video_id = match.group(1)
                break

        if not video_id:
            match = re.search(r'(\d{15,})', url)
            if match:
                video_id = match.group(1)

        if not video_id:
            return None, None, "Could not extract video ID"

        # 尝试 iesdouyin 接口
        api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_id}"
        data = http_get_json(api_url, timeout=15)

        items = data.get("item_list", [])
        if not items:
            return None, None, "iesdouyin: empty response"

        item = items[0]
        title = item.get("desc", "douyin_video")
        video_info = item.get("video", {})

        # 获取视频 URL
        play_addr = video_info.get("play_addr", {})
        url_list = play_addr.get("url_list", [])
        if url_list:
            video_url = url_list[0].replace("playwm", "play")
            logger.info(f"[iesdouyin] Got video URL for: {title[:30]}")
            return video_url, title, None

        return None, title, "iesdouyin: no play URL"

    except Exception as e:
        logger.error(f"[iesdouyin] Failed: {e}")
        return None, None, f"iesdouyin: {str(e)[:100]}"


def download_douyin(url):
    """尝试所有方法下载抖音视频"""
    errors = []

    # 方法1: tikwm
    video_url, title, err = try_tikwm(url)
    if video_url:
        filepath = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex[:12]}.mp4")
        try:
            size = download_file(video_url, filepath)
            if size > 1000:
                return filepath, title, None
            os.remove(filepath)
            errors.append("tikwm: file too small")
        except Exception as e:
            errors.append(f"tikwm download: {e}")
            if os.path.exists(filepath):
                os.remove(filepath)
    else:
        errors.append(err or "tikwm: unknown error")

    # 方法2: iesdouyin
    video_url, title, err = try_dlpanda(url)
    if video_url:
        filepath = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex[:12]}.mp4")
        try:
            size = download_file(video_url, filepath)
            if size > 1000:
                return filepath, title, None
            os.remove(filepath)
            errors.append("iesdouyin: file too small")
        except Exception as e:
            errors.append(f"iesdouyin download: {e}")
            if os.path.exists(filepath):
                os.remove(filepath)
    else:
        errors.append(err or "iesdouyin: unknown error")

    return None, None, " | ".join(errors)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "yt-dlp-api", "version": "3.0.0"}


@app.post("/")
async def parse_video(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"status": "error", "error": {"code": "invalid_json"}, "text": "Invalid JSON"},
            status_code=400,
        )

    url = data.get("url", "").strip()
    if not url:
        return JSONResponse(
            {"status": "error", "error": {"code": "no_url"}, "text": "No URL"},
            status_code=400,
        )

    quality = data.get("videoQuality", "1080")
    is_douyin = "douyin.com" in url

    # ========== 抖音: 第三方 API ==========
    if is_douyin:
        filepath, title, error = download_douyin(url)

        if not filepath:
            return JSONResponse({
                "status": "error",
                "error": {"code": "douyin_failed"},
                "text": error or "All douyin methods failed",
            })

        safe_title = "".join(
            c for c in (title or "video")
            if c.isalnum() or c in " _-" or ('\u4e00' <= c <= '\u9fa5')
        ).strip()[:80] or "video"

        base_url = str(request.base_url).rstrip("/")
        actual_filename = os.path.basename(filepath)

        return JSONResponse({
            "status": "tunnel",
            "url": f"{base_url}/file/{actual_filename}",
            "filename": f"{safe_title}.mp4",
        })

    # ========== 其他平台: yt-dlp ==========
    file_id = str(uuid.uuid4())[:12]
    output_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")

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
        "http_headers": HEADERS,
    }

    if "tiktok.com" in url and COOKIES_PATH:
        ydl_opts["cookiefile"] = COOKIES_PATH
        ydl_opts["http_headers"] = {**HEADERS, "Referer": "https://www.tiktok.com/"}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename_on_disk = ydl.prepare_filename(info)

            if not os.path.exists(filename_on_disk):
                base = os.path.splitext(filename_on_disk)[0]
                for ext in [".mp4", ".webm", ".mkv", ".m4a", ".mp3"]:
                    if os.path.exists(base + ext):
                        filename_on_disk = base + ext
                        break

            if not os.path.exists(filename_on_disk):
                return JSONResponse({
                    "status": "error",
                    "error": {"code": "file_not_found"},
                    "text": "File not found after download",
                })

            title = info.get("title", "video")
            ext = os.path.splitext(filename_on_disk)[1] or ".mp4"
            safe_title = "".join(
                c for c in title
                if c.isalnum() or c in " _-" or ('\u4e00' <= c <= '\u9fa5')
            ).strip()[:80] or "video"

            base_url = str(request.base_url).rstrip("/")
            actual_filename = os.path.basename(filename_on_disk)

            return JSONResponse({
                "status": "tunnel",
                "url": f"{base_url}/file/{actual_filename}",
                "filename": f"{safe_title}{ext}",
            })

    except yt_dlp.utils.DownloadError as e:
        return JSONResponse({
            "status": "error",
            "error": {"code": "download_failed"},
            "text": str(e)[:200],
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": {"code": "unexpected_error"},
            "text": str(e)[:200],
        })


@app.get("/file/{filename}")
async def serve_file(filename: str):
    safe = os.path.basename(filename)
    path = os.path.join(DOWNLOAD_DIR, safe)
    if os.path.exists(path):
        return FileResponse(path, media_type="video/mp4", filename=safe)
    return JSONResponse({"error": "file_not_found"}, status_code=404)
