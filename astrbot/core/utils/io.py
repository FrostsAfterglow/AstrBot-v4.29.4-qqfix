import base64
import html
import inspect
import logging
import os
import shutil
import socket
import ssl
import time
import uuid
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlparse

import aiohttp
import certifi
import psutil
from PIL import Image

from .astrbot_path import get_astrbot_temp_path

logger = logging.getLogger("astrbot")

# ---------------------------------------------------------------------------
# 下载请求头（v4.29.4-QQfix）
# QQ 的图片 / 群文件 CDN（gchat.qpic.cn、qfile.qq.com、multimedia.nt.qq.com.cn 等）
# 对没有浏览器 UA 的请求会直接返回 400，导致「QQ 文件/图片获取失败」。
# ---------------------------------------------------------------------------
DEFAULT_DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 需要携带 Referer 的腾讯系域名（按后缀匹配）
_TENCENT_REFERER_HOSTS = (
    "qpic.cn",
    "qq.com",
    "qq.com.cn",
    "gtimg.cn",
    "weixin.qq.com",
)

_QQ_REFERER_CANDIDATES = (
    "https://qun.qq.com/",
    "https://im.qq.com/",
    "https://qfile.qq.com/",
)


def normalize_download_url(url: str) -> str:
    """清洗平台回传的下载链接。

    OneBot / NapCat / Lagrange 有时会给出 HTML 转义过的链接（``&amp;``）、
    被引号包裹的链接，或带零宽字符的链接；直接请求会得到 400。
    """
    if not isinstance(url, str):
        return url
    cleaned = html.unescape(url)
    cleaned = cleaned.replace("\\/", "/").replace("\\u0026", "&")
    cleaned = cleaned.replace("\u200b", "").replace("\ufeff", "")
    cleaned = cleaned.strip().strip('"').strip("'").strip()
    # 去掉链接内部混入的换行/制表符（签名参数会被截断）
    cleaned = "".join(cleaned.splitlines()).strip()
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    return cleaned


def _is_tencent_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in _TENCENT_REFERER_HOSTS)


def build_download_headers(url: str, referer: str | None = None) -> dict:
    """构造浏览器风格的下载请求头。

    默认带浏览器 UA；腾讯系域名自动附带 Referer（qun.qq.com）。
    传入空字符串表示**不发送** Referer，用于兼容拒绝外站 Referer 的 CDN。
    """
    headers = {
        "User-Agent": DEFAULT_DOWNLOAD_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
    elif referer is None and _is_tencent_host(url):
        headers["Referer"] = _QQ_REFERER_CANDIDATES[0]
    return headers


def download_header_candidates(url: str) -> list[dict]:
    """返回按优先级排列的请求头组合，用于 400/403 时重试。"""
    if not _is_tencent_host(url):
        return [build_download_headers(url)]
    candidates = [build_download_headers(url, ref) for ref in _QQ_REFERER_CANDIDATES]
    # 最后再试一次「无 Referer」，兼容会拒绝外站 Referer 的 CDN
    candidates.append(build_download_headers(url, ""))
    return candidates


def _safe_url_for_log(url: str) -> str:
    """Return a URL summary that omits query strings and fragments.

    Args:
        url: URL that may contain signed query parameters.

    Returns:
        A short description suitable for logs.
    """

    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        filename = Path(unquote(parsed.path or "")).name
        suffix = f" file={filename!r}" if filename else ""
        return f"{parsed.scheme} URL host={parsed.netloc!r}{suffix} len={len(url)}"
    return f"URL len={len(url)}"


def on_error(func, path, exc_info) -> None:
    """A callback of the rmtree function."""
    import stat

    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        raise exc_info[1]


def remove_dir(file_path: str) -> bool:
    if not os.path.lexists(file_path):
        return True
    if os.path.isfile(file_path) or os.path.islink(file_path):
        os.remove(file_path)
    else:
        shutil.rmtree(file_path, onerror=on_error)
    return True


def ensure_dir(dir_path: str | Path) -> None:
    """确保目录存在。如果路径处存在非目录的文件或损坏的符号链接，则先将其删除。"""
    p = Path(dir_path)
    if (p.exists() or p.is_symlink()) and not p.is_dir():
        logger.warning(
            f"Path {p} exists but is not a directory; removing it before creating "
            "the directory."
        )
        try:
            if p.is_dir():
                shutil.rmtree(p, onerror=on_error)
            else:
                p.unlink()
        except Exception as e:
            logger.error(f"Failed to remove conflicting path {p}: {e!s}")
            raise RuntimeError(f"Could not remove conflicting path {p}: {e!s}") from e

    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create directory {p}: {e!s}")
        raise RuntimeError(f"Could not create directory {p}: {e!s}") from e


def port_checker(port: int, host: str = "localhost") -> bool:
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sk.settimeout(1)
    try:
        sk.connect((host, port))
        sk.close()
        return True
    except Exception:
        sk.close()
        return False


def save_temp_img(img: Image.Image | bytes) -> str:
    temp_dir = get_astrbot_temp_path()
    # 获得时间戳
    timestamp = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    p = os.path.join(temp_dir, f"io_temp_img_{timestamp}.jpg")

    if isinstance(img, Image.Image):
        cast(Image.Image, img).save(p)
    else:
        with open(p, "wb") as f:
            f.write(img)
    return p


async def _download_once(
    url: str,
    path: str | None,
    headers: dict,
    *,
    post: bool = False,
    post_data: dict | None = None,
    timeout: int | float = 120,
    insecure: bool = False,
) -> str | None:
    """执行一次下载请求；成功时返回路径（无 path 时返回临时图片路径）。"""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(
        ssl=(False if insecure else ssl_context),
    )
    async with aiohttp.ClientSession(
        trust_env=True,
        connector=connector,
    ) as session:
        request = session.post(url, json=post_data) if post else session.get(url)
        async with request as resp:
            if resp.status in (400, 403):
                logger.warning(
                    "下载被拒绝(HTTP %s)，尝试更换请求头重试: %s",
                    resp.status,
                    _safe_url_for_log(url),
                )
                return None
            if resp.status != 200:
                logger.warning(
                    "图片下载失败(HTTP %s): %s",
                    resp.status,
                    _safe_url_for_log(url),
                )
                return None
            data = await resp.read()
            if not path:
                return save_temp_img(data)
            with open(path, "wb") as f:
                f.write(data)
            return path


async def download_image_by_url(
    url: str,
    post: bool = False,
    post_data: dict | None = None,
    path: str | None = None,
) -> str:
    """下载图片, 返回 path（会带浏览器请求头，兼容 QQ 图片 CDN）。"""
    url = normalize_download_url(url)
    last_error: Exception | None = None
    for headers in download_header_candidates(url):
        try:
            result = await _download_once(
                url,
                path,
                headers,
                post=post,
                post_data=post_data,
            )
            if result is not None:
                return result
            last_error = DownloadFileHTTPError(
                f"Failed to download image from {_safe_url_for_log(url)}"
            )
        except (aiohttp.ClientConnectorSSLError,
                aiohttp.ClientConnectorCertificateError):
            logger.warning(
                f"SSL certificate verification failed for {_safe_url_for_log(url)}. "
                "Disabling SSL verification (CERT_NONE) as a fallback. "
                "This is insecure and exposes the application to man-in-the-middle attacks."
            )
            result = await _download_once(
                url,
                path,
                headers,
                post=post,
                post_data=post_data,
                insecure=True,
            )
            if result is not None:
                return result
            last_error = DownloadFileHTTPError(
                f"Failed to download image from {_safe_url_for_log(url)}"
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            break
    raise last_error or DownloadFileHTTPError(
        f"Failed to download image from {_safe_url_for_log(url)}"
    )


async def _emit_download_progress(progress_callback, payload: dict) -> None:
    if not progress_callback:
        return
    result = progress_callback(payload)
    if inspect.isawaitable(result):
        await result


class DownloadFileHTTPError(RuntimeError):
    """Raised when a file download returns an unsuccessful HTTP status."""


def _raise_for_download_status(resp, url: str) -> None:
    if resp.status == 200:
        return
    logger.error(
        "Failed to download file from %s. HTTP status code: %s",
        _safe_url_for_log(url),
        resp.status,
    )
    raise DownloadFileHTTPError(
        "Failed to download file from "
        f"{_safe_url_for_log(url)}. HTTP status code: {resp.status}"
    )


async def _download_response_to_file(
    resp,
    file_obj,
    url: str,
    show_progress: bool,
    progress_callback,
    show_downloading_label: bool = True,
) -> None:
    """Write a successful download response to a local file.

    Args:
        resp: aiohttp response object to read from.
        file_obj: Open writable binary file object.
        url: Source URL used for progress events and sanitized errors.
        show_progress: Whether to print progress to stdout.
        progress_callback: Optional callback for progress payloads.
        show_downloading_label: Whether to use the standard download heading.

    """

    total_size = int(resp.headers.get("content-length", 0))
    downloaded_size = 0
    start_time = time.time()
    if show_progress:
        if show_downloading_label:
            print(
                f"Downloading: {_safe_url_for_log(url)} | "
                f"Size: {total_size / 1024:.2f} KB"
            )
        else:
            print(f"Size: {total_size / 1024:.2f} KB | URL: {_safe_url_for_log(url)}")
    await _emit_download_progress(
        progress_callback,
        {
            "url": url,
            "downloaded": 0,
            "total": total_size,
            "percent": 0,
            "speed": 0,
        },
    )
    while True:
        chunk = await resp.content.read(8192)
        if not chunk:
            break
        file_obj.write(chunk)
        downloaded_size += len(chunk)
        elapsed_time = time.time() - start_time if time.time() - start_time > 0 else 1
        speed = downloaded_size / 1024 / elapsed_time  # KB/s
        percent = downloaded_size / total_size if total_size > 0 else 0
        await _emit_download_progress(
            progress_callback,
            {
                "url": url,
                "downloaded": downloaded_size,
                "total": total_size,
                "percent": percent,
                "speed": speed,
            },
        )
        if show_progress:
            print(
                f"\rProgress: {percent:.2%} Speed: {speed:.2f} KB/s",
                end="",
            )
    await _emit_download_progress(
        progress_callback,
        {
            "url": url,
            "downloaded": downloaded_size,
            "total": total_size,
            "percent": 1,
            "speed": 0,
        },
    )


async def download_file(
    url: str,
    path: str,
    show_progress: bool = False,
    progress_callback=None,
    allow_insecure_ssl_fallback: bool = True,
) -> None:
    """Download a remote file to a local path.

    会带浏览器 User-Agent / Referer，并在 QQ CDN 返回 400/403 时
    自动更换 Referer 重试（修复 QQ 图片、群文件下载失败）。

    Args:
        url: Remote URL to download.
        path: Local destination path.
        show_progress: Whether to print progress to stdout.
        progress_callback: Optional callback for progress payloads.
        allow_insecure_ssl_fallback: Whether certificate failures may retry with
            TLS certificate verification disabled.

    Returns:
        None.
    """

    url = normalize_download_url(url)
    last_error: Exception | None = None

    async def _attempt(headers: dict, *, insecure: bool) -> bool:
        """返回 True 表示成功，False 表示被 CDN 以 400/403 拒绝。"""
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(
            ssl=(False if insecure else ssl_context),
        )
        async with aiohttp.ClientSession(
            trust_env=True,
            connector=connector,
        ) as session:
            timeout = 120 if insecure else 1800
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status in (400, 403):
                    logger.warning(
                        "下载被拒绝(HTTP %s)，尝试更换请求头重试: %s",
                        resp.status,
                        _safe_url_for_log(url),
                    )
                    return False
                _raise_for_download_status(resp, url)
                with open(path, "wb") as f:
                    await _download_response_to_file(
                        resp,
                        f,
                        url,
                        show_progress,
                        progress_callback,
                        show_downloading_label=not insecure,
                    )
                return True

    for headers in download_header_candidates(url):
        try:
            if await _attempt(headers, insecure=False):
                if show_progress:
                    print()
                return
            last_error = DownloadFileHTTPError(
                f"Failed to download file from {_safe_url_for_log(url)}"
            )
        except (aiohttp.ClientConnectorSSLError,
                aiohttp.ClientConnectorCertificateError):
            if not allow_insecure_ssl_fallback:
                raise
            logger.warning(
                f"SSL certificate verification failed for {_safe_url_for_log(url)}. "
                "Falling back to unverified connection (CERT_NONE). "
                "This is insecure and exposes the application to man-in-the-middle attacks. "
                "Please investigate certificate issues with the remote server."
            )
            if await _attempt(headers, insecure=True):
                if show_progress:
                    print()
                return
            last_error = DownloadFileHTTPError(
                f"Failed to download file from {_safe_url_for_log(url)}"
            )

    raise last_error or DownloadFileHTTPError(
        f"Failed to download file from {_safe_url_for_log(url)}"
    )


def file_to_base64(file_path: str) -> str:
    with open(file_path, "rb") as f:
        data_bytes = f.read()
        base64_str = base64.b64encode(data_bytes).decode()
    return "base64://" + base64_str


def get_local_ip_addresses():
    net_interfaces = psutil.net_if_addrs()
    network_ips = []

    for interface, addrs in net_interfaces.items():
        for addr in addrs:
            if addr.family == socket.AF_INET:  # 使用 socket.AF_INET 代替 psutil.AF_INET
                network_ips.append(addr.address)

    return network_ips
