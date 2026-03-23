"""
yt-dlp Video Download API
- 抖音: 自定义下载（绕过 yt-dlp）
- TikTok/其他: yt-dlp + cookies
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="yt-dlp API", version="2.0.0")

DOWNLOAD_DIR = "/tmp/ytdlp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FILE_MAX_AGE = 600

COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
if os.path.exists(COOKIES_PATH):
    logger.info(f"Found cookies.txt at {COOKIES_PATH}")
else:
    logger.warning("cookies.txt not found!")
    COOKIES_PATH = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.douyin.com/",
}


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


def resolve_douyin_url(short_url):
    """解析抖音短链接，获取真实 URL 和视频 ID"""
    try:
        req = urllib.request.Request(short_url, headers=HEADERS, method="GET")
        # 不自动跟随重定向，手动获取 Location
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
        # 用简单方式：直接请求，让它重定向
        req2 = urllib.request.Request(short_url, headers=HEADERS)
        resp = urllib.request.urlopen(req2, timeout=15)
        final_url = resp.url
        logger.info(f"Resolved URL: {final_url}")

        # 从 URL 中提取视频 ID
        # 格式: https://www.douyin.com/video/7496849887033691392
        match = re.search(r'/video/(\d+)', final_url)
        if match:
            return match.group(1), final_url

        # 也可能是 note 格式
        match = re.search(r'/note/(\d+)', final_url)
        if match:
            return match.group(1), final_url

        # 尝试从短链接本身提取
        match = re.search(r'(\d{15,})', final_url)
        if match:
            return match.group(1), final_url

        return None, final_url
    except Exception as e:
        logger.error(f"Failed to resolve URL: {e}")
        return None, short_url


def get_douyin_video(video_id):
    """通过抖音 Web API 获取视频信息"""
    # 方法1: 使用抖音网页端 API
    api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={video_id}&aid=1128&version_name=23.5.0&device_platform=android&os_version=2333"

    try:
        req = urllib.request.Request(api_url, headers={
            **HEADERS,
            "Accept": "application/json",
        })
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))

        aweme = data.get("aweme_detail", {})
        if not aweme:
            return None, None, "No aweme_detail in response"

        title = aweme.get("desc", "douyin_video")

        # 获取无水印视频 URL
        video_info = aweme.get("video", {})

        # 尝试 play_addr
        play_addr = video_info.get("play_addr", {})
        url_list = play_addr.get("url_list", [])
        if url_list:
            video_url = url_list[0]
            # 替换水印域名
            video_url = video_url.replace("playwm", "play")
            return video_url, title, None

        # 尝试 bit_rate 列表
        bit_rate_list = video_info.get("bit_rate", [])
        if bit_rate_list:
            best = max(bit_rate_list, key=lambda x: x.get("bit_rate", 0))
            play_addr2 = best.get("play_addr", {})
            url_list2 = play_addr2.get("url_list", [])
            if url_list2:
                return url_list2[0], title, None

        return None, title, "No video URL found in response"

    except Exception as e:
        logger.error(f"Douyin API method 1 failed: {e}")
        return None, None, str(e)


def get_douyin_video_v2(video_id):
    """备用方法: 通过页面解析获取视频"""
    page_url = f"https://www.douyin.com/video/{video_id}"
    try:
        req = urllib.request.Request(page_url, headers=HEADERS)
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="ignore")

        # 从页面中提取 SSR 数据
        match = re.search(
            r'<script id="RENDER_DATA"[^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        if match:
            raw = urllib.parse.unquote(match.group(1))
            data = json.loads(raw)

            # 遍历查找视频信息
            for key, val in data.items():
                if isinstance(val, dict):
                    aweme = val.get("aweme", {}).get("detail", {})
                    if not aweme:
                        # 有时候在 awemeDetail 里
                        aweme = val.get("awemeDetail", {})
                    if aweme:
                        title = aweme.get("desc", "douyin_video")
                        video_info = aweme.get("video", {})
                        play_addr = video_info.get("play_addr", {})
                        url_list = play_addr.get("url_list", [])
                        if url_list:
                            video_url = url_list[0].replace("playwm", "play")
                            return video_url, title, None

        # 尝试直接从 HTML 中提取视频 URL
        match = re.search(r'"playApi"\s*:\s*"(https?://[^"]+)"', html)
        if match:
            video_url = match.group(1).replace("\\u002F", "/")
            return video_url, "douyin_video", None

        return None, None, "Could not extract video from page"

    except Exception as e:
        logger.error(f"Douyin page parse failed: {e}")
        return None, None, str(e)


def download_douyin(url, quality="1080"):
    """完整的抖音下载流程"""
    logger.info(f"[Douyin] Processing: {url}")

    # Step 1: 解析短链接
    video_id, resolved_url = resolve_douyin_url(url)
    if not video_id:
        return None, None, f"Could not extract video ID from {resolved_url}"

    logger.info(f"[Douyin] Video ID: {video_id}")

    # Step 2: 尝试 API 获取视频 URL
    video_url, title, error = get_douyin_video(video_id)

    # Step 3: 如果 API 失败，尝试页面解析
    if not video_url:
        logger.info(f"[Douyin] API failed ({error}), trying page parse...")
        video_url, title, error = get_douyin_video_v2(video_id)

    if not video_url:
        return None, None, f"All methods failed: {error}"

    logger.info(f"[Douyin] Got video URL, downloading...")

    # Step 4: 下载视频
    file_id = str(uuid.uuid4())[:12]
    filepath = os.path.join(DOWNLOAD_DIR, f"{file_id}.mp4")

    try:
        req = urllib.request.Request(video_url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://www.douyin.com/",
        })
        resp = urllib.request.urlopen(req, timeout=120)

        with open(filepath, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)

        file_size = os.path.getsize(filepath)
        if file_size < 1000:
            # 文件太小，可能是错误页面
            os.remove(filepath)
            return None, title, f"Downloaded file too small ({file_size} bytes), likely an error"

        logger.info(f"[Douyin] Downloaded: {filepath} ({file_size} bytes)")
        return filepath, title or "douyin_video", None

    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        return None, title, f"Download failed: {str(e)}"


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "yt-dlp-api",
        "version": "2.0.0",
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
    is_douyin = "douyin.com" in url

    # ========== 抖音: 自定义下载 ==========
    if is_douyin:
        filepath, title, error = download_douyin(url, quality)

        if not filepath:
            return JSONResponse({
                "status": "error",
                "error": {"code": "douyin_failed"},
                "text": error or "Unknown douyin error",
            })

        safe_title = "".join(
            c for c in (title or "video")
            if c.isalnum() or c in " _-" or ('\u4e00' <= c <= '\u9fa5')
        ).strip()[:80] or "video"

        actual_filename = os.path.basename(filepath)
        base_url = str(request.base_url).rstrip("/")
        download_url = f"{base_url}/file/{actual_filename}"

        return JSONResponse({
            "status": "tunnel",
            "url": download_url,
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
        "http_headers": {
            "User-Agent": HEADERS["User-Agent"],
            "Accept": HEADERS["Accept"],
            "Accept-Language": HEADERS["Accept-Language"],
        },
    }

    if "tiktok.com" in url and COOKIES_PATH:
        ydl_opts["cookiefile"] = COOKIES_PATH
        ydl_opts["http_headers"]["Referer"] = "https://www.tiktok.com/"

    logger.info(f"Processing URL with yt-dlp: {url}")

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
            ).strip()[:80] or "video"

            actual_filename = os.path.basename(filename_on_disk)
            base_url = str(request.base_url).rstrip("/")
            download_url = f"{base_url}/file/{actual_filename}"

            logger.info(f"Success: {safe_title}{ext}")

            return JSONResponse({
                "status": "tunnel",
                "url": download_url,
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
