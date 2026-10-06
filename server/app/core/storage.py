"""素材存储：本地磁盘（``LOCAL_STORAGE_DIR``，经 ``/media/{key}`` 暴露）或 S3 兼容对象存储（boto3）。

- ``get_storage()``：``STORAGE_MODE=local`` 或 ``OSS_ENDPOINT`` 为空 → ``LocalStorage``，否则 ``S3Storage``；
- ``public_url_for(key)``：``PUBLIC_BASE_URL + /media/{key}``（local）或 ``OSS_PUBLIC_BASE_URL + /{key}``（oss）；
- ``probe_image_size(data)``：只解析文件头（PNG IHDR / JPEG SOF / WebP VP8·VP8L·VP8X，另含 GIF 逻辑屏幕尺寸），无第三方依赖；
- ``sniff_media_type(head)``：按魔数识别 PNG / JPEG / WebP / GIF / MP4 / MOV。

存储键形如 ``media/images/2026/10/<uuid hex>.png``（``build_storage_key``）。
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import struct
import tempfile
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import IO, Any, BinaryIO, Union

from app.core.config import settings

StorageData = Union[bytes, bytearray, memoryview, str, os.PathLike, BinaryIO]

MIME_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}
EXTENSION_MIMES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
VIDEO_MIME_TYPES = frozenset({"video/mp4", "video/quicktime"})

SNIFF_HEAD_BYTES = 64
_COPY_CHUNK = 1024 * 1024


class InvalidStorageKey(ValueError):
    """存储键非法（空、绝对路径、``..`` 穿越、反斜杠或控制字符）。"""


def validate_storage_key(key: str) -> str:
    """校验并返回规范的存储键（POSIX 相对路径）；非法抛 ``InvalidStorageKey``（``/media/{key}`` 返回 400）。"""
    if not isinstance(key, str) or not key or len(key) > 255:
        raise InvalidStorageKey("非法的存储键")
    if key.startswith("/") or "\\" in key or any(ord(ch) < 32 for ch in key):
        raise InvalidStorageKey("非法的存储键")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts) or ":" in parts[0]:
        raise InvalidStorageKey("非法的存储键")
    return key


def extension_for(mime_type: str | None) -> str | None:
    return MIME_EXTENSIONS.get((mime_type or "").lower())


def guess_content_type(key: str) -> str:
    ext = PurePosixPath(key).suffix.lower().lstrip(".")
    if ext in EXTENSION_MIMES:
        return EXTENSION_MIMES[ext]
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


def build_storage_key(category: str, ext: str, now: datetime | None = None) -> str:
    """``media/{category}/{yyyy}/{mm}/{uuid4 hex}.{ext}``（``category`` ∈ ``images`` / ``videos`` / ``uploads``）。"""
    now = now or datetime.now(timezone.utc)
    ext = ext.lower().lstrip(".")
    return f"media/{category}/{now:%Y}/{now:%m}/{uuid.uuid4().hex}.{ext}"


def public_url_for(key: str) -> str:
    """按**当前**配置生成素材公网 URL（转存成功时落库，之后修改配置不改写历史行）。"""
    key = validate_storage_key(key)
    return f"{settings.media_public_base}/{key}"


# ---------------------------------------------------------------- 后端


class StorageBackend(ABC):
    """存储后端接口。``data`` 可为 bytes、本地文件路径（local 以 ``os.replace`` 移入）或二进制文件对象。"""

    mode: str = ""

    @abstractmethod
    def save(self, key: str, data: StorageData, content_type: str | None = None) -> str:
        """写入对象并返回规范化后的 key。"""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """删除对象；不存在返回 ``False``。"""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def open(self, key: str) -> IO[bytes]:
        """以二进制只读流打开对象；不存在抛 ``FileNotFoundError``。调用方负责关闭。"""

    @abstractmethod
    def size(self, key: str) -> int | None: ...

    def read(self, key: str) -> bytes:
        with self.open(key) as fh:
            return fh.read()

    def public_url(self, key: str) -> str:
        return public_url_for(key)


class LocalStorage(StorageBackend):
    mode = "local"

    def __init__(self, root: str | os.PathLike | None = None) -> None:
        self.root = Path(root) if root is not None else settings.local_storage_path
        self.root = self.root.resolve()

    def path_for(self, key: str) -> Path:
        """存储键对应的本地绝对路径（保证位于 ``root`` 内）。"""
        key = validate_storage_key(key)
        path = (self.root / key).resolve()
        if path != self.root and self.root not in path.parents:
            raise InvalidStorageKey("非法的存储键")
        return path

    def save(self, key: str, data: StorageData, content_type: str | None = None) -> str:
        key = validate_storage_key(key)
        dest = self.path_for(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (str, os.PathLike)):
            src = Path(data)
            try:
                os.replace(src, dest)
            except OSError:  # 跨文件系统：复制后删除源文件
                shutil.copyfile(src, dest)
                src.unlink(missing_ok=True)
            return key
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                if isinstance(data, (bytes, bytearray, memoryview)):
                    fh.write(data)
                else:
                    shutil.copyfileobj(data, fh, _COPY_CHUNK)
            os.replace(tmp_name, dest)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return key

    def delete(self, key: str) -> bool:
        path = self.path_for(key)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def open(self, key: str) -> IO[bytes]:
        path = self.path_for(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.open("rb")

    def size(self, key: str) -> int | None:
        path = self.path_for(key)
        return path.stat().st_size if path.is_file() else None


class S3Storage(StorageBackend):
    """S3 兼容对象存储（阿里 OSS / 七牛 / MinIO 均走 S3 API）。"""

    mode = "oss"

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        bucket: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        client: Any = None,
    ) -> None:
        self.bucket = bucket if bucket is not None else settings.oss_bucket
        if not self.bucket:
            raise ValueError("OSS_BUCKET 未配置")
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url if endpoint_url is not None else (settings.oss_endpoint or None),
                region_name=region_name if region_name is not None else (settings.oss_region or None),
                aws_access_key_id=access_key if access_key is not None else (settings.oss_access_key or None),
                aws_secret_access_key=secret_key if secret_key is not None else (settings.oss_secret_key or None),
                config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
            )
        self.client = client

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        response = getattr(exc, "response", None) or {}
        code = str((response.get("Error") or {}).get("Code", ""))
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

    def save(self, key: str, data: StorageData, content_type: str | None = None) -> str:
        key = validate_storage_key(key)
        content_type = content_type or guess_content_type(key)
        if isinstance(data, (str, os.PathLike)):
            self.client.upload_file(str(data), self.bucket, key, ExtraArgs={"ContentType": content_type})
            Path(data).unlink(missing_ok=True)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            self.client.put_object(Bucket=self.bucket, Key=key, Body=bytes(data), ContentType=content_type)
        else:
            self.client.upload_fileobj(data, self.bucket, key, ExtraArgs={"ContentType": content_type})
        return key

    def delete(self, key: str) -> bool:
        key = validate_storage_key(key)
        if not self.exists(key):
            return False
        self.client.delete_object(Bucket=self.bucket, Key=key)
        return True

    def exists(self, key: str) -> bool:
        key = validate_storage_key(key)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:  # noqa: BLE001 - botocore ClientError
            if self._is_not_found(exc):
                return False
            raise

    def open(self, key: str) -> IO[bytes]:
        key = validate_storage_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            if self._is_not_found(exc):
                raise FileNotFoundError(key) from exc
            raise
        return response["Body"]

    def size(self, key: str) -> int | None:
        key = validate_storage_key(key)
        try:
            return int(self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        except Exception as exc:  # noqa: BLE001
            if self._is_not_found(exc):
                return None
            raise


_storage_lock = threading.Lock()
_storage: StorageBackend | None = None
_storage_signature: tuple[Any, ...] | None = None


def _signature() -> tuple[Any, ...]:
    if settings.use_local_storage:
        return ("local", str(settings.local_storage_path))
    return ("oss", settings.oss_endpoint, settings.oss_region, settings.oss_bucket, settings.oss_access_key)


def get_storage() -> StorageBackend:
    """当前配置对应的存储后端（进程内单例，配置变化时重建）。"""
    global _storage, _storage_signature
    signature = _signature()
    with _storage_lock:
        if _storage is None or _storage_signature != signature:
            _storage = LocalStorage() if signature[0] == "local" else S3Storage()
            _storage_signature = signature
        return _storage


def reset_storage() -> None:
    """测试用：丢弃缓存的后端实例。"""
    global _storage, _storage_signature
    with _storage_lock:
        _storage = None
        _storage_signature = None


# ---------------------------------------------------------------- 文件头解析

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD9)})
_HEIF_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"})


def _valid_size(width: int, height: int) -> tuple[int, int] | None:
    if width > 0 and height > 0:
        return width, height
    return None


def _probe_png(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return _valid_size(width, height)


def _probe_jpeg(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            i += 1  # 容错：跳过非标记字节
            continue
        while i < n and data[i] == 0xFF:  # 填充字节
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker in _JPEG_STANDALONE:
            continue
        if marker in (0xD9, 0xDA):  # EOI / SOS：尺寸应已出现
            return None
        if i + 2 > n:
            return None
        (length,) = struct.unpack(">H", data[i:i + 2])
        if length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if i + 7 > n:
                return None
            height, width = struct.unpack(">HH", data[i + 3:i + 7])
            return _valid_size(width, height)
        i += length
    return None


def _probe_webp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return _valid_size(width, height)
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        return _valid_size((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return _valid_size(width, height)
    return None


def probe_image_size(data: bytes | bytearray | memoryview | None) -> tuple[int, int] | None:
    """解析图片宽高 ``(width, height)``；无法识别或文件头不完整返回 ``None``。"""
    if not data:
        return None
    data = bytes(data)
    try:
        if data.startswith(_PNG_SIGNATURE):
            return _probe_png(data)
        if data.startswith(b"\xff\xd8"):
            return _probe_jpeg(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _probe_webp(data)
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
            return _valid_size(width, height)
    except (struct.error, IndexError):
        return None
    return None


def sniff_media_type(head: bytes | bytearray | memoryview | None) -> str | None:
    """按魔数识别 MIME：``image/png`` / ``image/jpeg`` / ``image/webp`` / ``image/gif`` / ``video/mp4`` / ``video/quicktime``；未识别返回 ``None``。"""
    if not head:
        return None
    head = bytes(head[:SNIFF_HEAD_BYTES])
    if head.startswith(_PNG_SIGNATURE):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIF_BRANDS:
            return None
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    return None
