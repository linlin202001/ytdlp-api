"""
yt-dlp Video Download API
部署在 Railway 上，为 n8n 工作流提供视频下载服务
主要用途：处理 Cobalt 不支持的抖音/TikTok 链接
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

app = FastAPI(title="yt-dlp API", version="1.0.0")

DOWNLOAD_DIR = "/tmp/ytdlp_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 文件最长保留 10 分钟（n8n 下载完就不需要了）
FILE_MAX_AGE = 600


def cleanup_old_files():
    """定期清理过期的下载文件，防止磁盘占满"""
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


# 启动清理线程
threading.Thread(target=cleanup_old_files, daemon=True).start()


@app.get("/health")
async def health():
    """健康检查端点"""
    return {"status": "ok", "service": "yt-dlp-api"}


@app.post("/")
async def parse_video(request: Request):
    """
    解析并下载视频
    请求格式（与 Cobalt API 兼容）:
    {
        "url": "https://...",
        "videoQuality": "1080"  // 可选
    }
    
    返回格式（与 Cobalt 兼容）:
    {
        "status": "tunnel",
        "url": "https://this-server/file/xxx",
        "filename": "video_title.mp4"
    }
    """
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

    ydl_opts = {
        "format": f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 3,
        "concurrent_fragment_downloads": 4,
        # 抖音/TikTok 可能需要的选项
        "extractor_args": {"tiktok": {"api_hostname": "api22-normal-c-alisg.tiktokv.com"}},
        # 限制文件大小 500MB，防止超大文件撑爆磁盘
        "max_filesize": 500 * 1024 * 1024,
    }

    logger.info(f"Processing URL: {url}")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename_on_disk = ydl.prepare_filename(info)

            # yt-dlp merge 后扩展名可能变化，查找实际文件
            if not os.path.exists(filename_on_disk):
                base = os.path.splitext(filename_on_disk)[0]
                for ext in [".mp4", ".webm", ".mkv", ".m4a", ".mp3"]:
                    candidate = base + ext
                    if os.path.exists(candidate):
                        filename_on_disk = candidate
                        break

            if not os.path.exists(filename_on_disk):
                return JSONResponse(
                    {
                        "status": "error",
                        "error": {"code": "file_not_found"},
                        "text": "Download completed but file not found on disk",
                    }
                )

            title = info.get("title", "video")
            ext = os.path.splitext(filename_on_disk)[1] or ".mp4"
            # 清理文件名中的特殊字符
            safe_title = "".join(c for c in title if c.isalnum() or c in " _-\u4e00-\u9fa5").strip()[:80]
            if not safe_title:
                safe_title = "video"

            actual_filename = os.path.basename(filename_on_disk)
            file_size = os.path.getsize(filename_on_disk)

            # 构建下载 URL
            base_url = str(request.base_url).rstrip("/")
            download_url = f"{base_url}/file/{actual_filename}"

            logger.info(f"Success: {safe_title}{ext} ({file_size} bytes)")

            return JSONResponse(
                {
                    "status": "tunnel",
                    "url": download_url,
                    "filename": f"{safe_title}{ext}",
                }
            )

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"Download error for {url}: {error_msg}")
        return JSONResponse(
            {
                "status": "error",
                "error": {"code": "download_failed"},
                "text": error_msg[:200],
            }
        )
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Unexpected error for {url}: {error_msg}")
        return JSONResponse(
            {
                "status": "error",
                "error": {"code": "unexpected_error"},
                "text": error_msg[:200],
            }
        )


@app.get("/file/{filename}")
async def serve_file(filename: str):
    """提供下载文件（n8n 从这里拉取视频）"""
    safe = os.path.basename(filename)
    path = os.path.join(DOWNLOAD_DIR, safe)
    if os.path.exists(path):
        return FileResponse(path, media_type="video/mp4", filename=safe)
    return JSONResponse({"error": "file_not_found"}, status_code=404)
