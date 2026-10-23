"""Keep inline Chat images below the upstream image-token admission budget.

The upstream limit is an image-input budget, not the model's full context
window.  This module estimates that budget from the base64 payload
(``len(base64) / 40``), retains the original image by content hash, and
replaces older images in place when a ``view_image`` tool is available.

The caller owns the payload and should invoke :func:`apply_image_budget`
immediately before sending the Chat request upstream.  Mutation is deliberately
limited to image and text blocks inside ``payload["messages"]``; message and
tool ordering is never changed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import io
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image, ImageOps


_DATA_IMAGE_PREFIX = re.compile(r"^data:image/[a-z0-9.+-]+;base64,", re.IGNORECASE)
_PLACEHOLDER_NOTE = (
    "historical image omitted from the inline budget; "
    "call view_image with this path if details are required"
)

# The first tuple is the tier selected from user-turn distance.  Later tuples
# are progressively more aggressive fallbacks for payloads without recall.
_PROFILES: tuple[tuple[int, int], ...] = (
    (1600, 90),
    (1280, 84),
    (1024, 76),
    (900, 70),
    (720, 65),
    (640, 55),
    (480, 45),
    (400, 38),
    (320, 30),
    (256, 24),
    (192, 18),
    (128, 12),
)
_FORCE_PROFILES: tuple[tuple[int, int], ...] = ((96, 10), (64, 8))

_FORMAT_EXTENSIONS = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
    "BMP": "bmp",
    "TIFF": "tiff",
}


@dataclass(frozen=True)
class ImageBudgetStats:
    image_count: int
    compressible_count: int
    estimated_before: int
    estimated_after: int
    compressed: int
    placeholders: int


@dataclass
class _ImageRecord:
    block: dict[str, Any]
    position: int
    distance: int
    mime: str
    encoded: str
    estimated: int
    width: int = 0
    height: int = 0
    sha256: str = ""
    original_path: Path | None = None
    image: Image.Image | None = None
    level: int = 0
    prepared: bool = False


def apply_image_budget(
    payload: dict[str, Any],
    *,
    force_aggressive: bool = False,
    data_dir: str | os.PathLike[str] | None = None,
) -> ImageBudgetStats:
    """Apply the image budget to a Chat payload in place.

    Only ``data:image/*;base64`` image blocks are considered.  Remote image URLs
    and all non-image content are preserved.  If the aggregate estimate is below
    the trigger, this function does not decode images or touch the filesystem.

    ``force_aggressive`` lowers the target to roughly 65 percent of its normal
    value.  Placeholders are still emitted only when the payload declares a
    ``view_image`` tool; force mode does not create an unusable reference.
    """

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ImageBudgetStats(0, 0, 0, 0, 0, 0)

    records = _collect_records(messages)
    image_count = _count_image_blocks(messages)
    estimated_before = sum(record.estimated for record in records)
    compressible_count = len(records)

    trigger, target, hard, ttl_days, max_bytes = _read_settings()
    profiles = _PROFILES + _FORCE_PROFILES if force_aggressive else _PROFILES
    if force_aggressive:
        target = max(1, int(round(target * 0.65)))
    target = min(target, hard)

    if not records or (estimated_before < trigger and not force_aggressive):
        return ImageBudgetStats(
            image_count=image_count,
            compressible_count=compressible_count,
            estimated_before=estimated_before,
            estimated_after=estimated_before,
            compressed=0,
            placeholders=0,
        )

    root = _resolve_data_dir(data_dir)
    try:
        _cleanup(root, ttl_days=ttl_days, max_bytes=max_bytes)
        _ensure_cache_dirs(root)
    except OSError:
        # A read-only volume, full disk, or misconfigured data path must not
        # turn an otherwise valid image request into a 500.
        return ImageBudgetStats(
            image_count=image_count,
            compressible_count=compressible_count,
            estimated_before=estimated_before,
            estimated_after=estimated_before,
            compressed=0,
            placeholders=0,
        )

    current = estimated_before
    compressed = 0
    placeholders = 0
    oldest_first = sorted(records, key=lambda record: (-record.distance, record.position))

    # First give every image at most its age-appropriate profile.  Each changed
    # image drops the aggregate immediately, so a mid-list image and newer ones
    # remain untouched whenever the older tiers already satisfy the target.
    for record in oldest_first:
        if current <= target:
            break
        if not _prepare(record, root):
            continue
        record.level = _profile_index(record.distance)
        changed = _compress_record(record, root, profiles[record.level])
        if changed:
            current -= record.estimated
            current += changed
            record.estimated = changed
            compressed += 1

    has_view_image = (
        _has_view_image_tool(payload.get("tools"))
        and _placeholders_enabled()
    )

    if current > target and has_view_image:
        for record in oldest_first:
            if current <= target:
                break
            if not _prepare(record, root) or record.original_path is None:
                continue
            placeholder = _placeholder_for(record)
            record.block.clear()
            record.block.update({"type": "text", "text": placeholder})
            current -= record.estimated
            record.estimated = 0
            placeholders += 1
    elif current > target:
        # Without a recall tool, a path-only placeholder would hide information.
        # Continue down the deterministic quality ladder old-to-new instead.
        for record in oldest_first:
            if not record.prepared:
                record.level = _profile_index(record.distance)

        progressed = True
        while current > target and progressed:
            progressed = False
            for record in oldest_first:
                if current <= target:
                    break
                if record.level >= len(profiles) - 1:
                    continue
                if not _prepare(record, root):
                    continue
                record.level += 1
                changed = _compress_record(record, root, profiles[record.level])
                if changed is None or changed >= record.estimated:
                    continue
                current -= record.estimated
                current += changed
                record.estimated = changed
                compressed += 1
                progressed = True

    return ImageBudgetStats(
        image_count=image_count,
        compressible_count=compressible_count,
        estimated_before=estimated_before,
        estimated_after=current,
        compressed=compressed,
        placeholders=placeholders,
    )


def _read_settings() -> tuple[int, int, int, int, int]:
    trigger = _env_int(
        ("WB_IMAGE_BUDGET_TRIGGER", "IMAGE_BUDGET_TRIGGER", "WB_IMAGE_TRIGGER_TOKENS"),
        75_000,
    )
    target = _env_int(
        ("WB_IMAGE_BUDGET_TARGET", "IMAGE_BUDGET_TARGET", "WB_IMAGE_TARGET_TOKENS"),
        65_000,
    )
    hard = _env_int(
        ("WB_IMAGE_BUDGET_HARD", "IMAGE_BUDGET_HARD", "WB_IMAGE_HARD_TOKENS"),
        90_000,
    )
    ttl_days = _env_int(
        ("WB_IMAGE_BUDGET_TTL_DAYS", "IMAGE_BUDGET_TTL_DAYS"),
        30,
        minimum=1,
    )
    max_mb = _env_int(
        ("WB_IMAGE_BUDGET_CACHE_MB", "IMAGE_BUDGET_CACHE_MB"),
        2048,
        minimum=1,
    )
    trigger = max(1, trigger)
    target = max(1, target)
    hard = max(1, hard)
    return trigger, target, hard, ttl_days, max_mb * 1024 * 1024


def _env_int(
    names: Iterable[str],
    default: int,
    *,
    minimum: int | None = None,
) -> int:
    for name in names:
        value = os.environ.get(name)
        if value is None or value.strip() == "":
            continue
        try:
            parsed = int(value)
        except ValueError:
            continue
        return max(minimum, parsed) if minimum is not None else parsed
    return default


def _resolve_data_dir(data_dir: str | os.PathLike[str] | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    configured = os.environ.get("WB_IMAGE_BUDGET_DATA_DIR")
    if configured:
        return Path(configured)
    # Import lazily so this focused module can be tested without starting the
    # full manager service or reading any runtime secrets.
    from server import config

    return Path(config.DATA_DIR)


def _collect_records(messages: list[Any]) -> list[_ImageRecord]:
    last_user_ordinal = -1
    owner_by_message: list[int] = []
    for message_index, message in enumerate(messages):
        if (
            isinstance(message, Mapping)
            and message.get("role") == "user"
            and not _is_lifted_tool_image_message(messages, message_index)
        ):
            last_user_ordinal += 1
        owner_by_message.append(last_user_ordinal)

    records: list[_ImageRecord] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            parsed = _data_image_url(block)
            if parsed is None:
                continue
            mime, encoded = parsed
            owner = owner_by_message[message_index]
            distance = max(0, last_user_ordinal - owner)
            records.append(
                _ImageRecord(
                    block=block,
                    position=message_index * 1_000_000 + block_index,
                    distance=distance,
                    mime=mime,
                    encoded=encoded,
                    estimated=_estimate_tokens(encoded),
                )
            )
    return records


def _is_lifted_tool_image_message(messages: list[Any], message_index: int) -> bool:
    """Recognize the image-only user message emitted after a tool result.

    ``responses.py`` cannot put structured images in a Chat ``tool`` message, so
    it lifts them into a user message immediately after the whole tool group.
    Despite the role, that message belongs to the current real user turn and
    must not advance the age ordinal.
    """

    if message_index <= 0:
        return False
    message = messages[message_index]
    previous = messages[message_index - 1]
    if not isinstance(message, Mapping) or not isinstance(previous, Mapping):
        return False
    if message.get("role") != "user" or previous.get("role") != "tool":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(
            isinstance(block, Mapping) and block.get("type") == "image_url"
            for block in content
        )
    )


def _count_image_blocks(messages: list[Any]) -> int:
    count = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "image_url":
                count += 1
    return count


def _data_image_url(block: Any) -> tuple[str, str] | None:
    if not isinstance(block, Mapping) or block.get("type") != "image_url":
        return None
    image_url = block.get("image_url")
    value = image_url if isinstance(image_url, str) else None
    if isinstance(image_url, Mapping):
        candidate = image_url.get("url")
        value = candidate if isinstance(candidate, str) else None
    if not value:
        return None
    match = _DATA_IMAGE_PREFIX.match(value)
    if match is None:
        return None
    header = value[5 : match.end() - 1]
    mime = header.split(";", 1)[0].lower()
    return mime, value[match.end() :]


def _estimate_tokens(encoded: str) -> int:
    return (len(encoded) + 39) // 40


def _profile_index(distance: int) -> int:
    if distance <= 0:
        return 0
    if distance <= 3:
        return 1
    if distance <= 8:
        return 2
    if distance <= 12:
        return 3
    return 4


def _prepare(record: _ImageRecord, root: Path) -> bool:
    if record.prepared:
        return record.image is not None
    try:
        raw = base64.b64decode(record.encoded, validate=True)
    except (binascii.Error, ValueError):
        return False
    if not raw:
        return False
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            image.load()
            width, height = image.size
            image_format = (source.format or "").upper()
    except (Image.DecompressionBombError, OSError, ValueError):
        return False

    digest = hashlib.sha256(raw).hexdigest()
    extension = _source_extension(image_format, record.mime)
    original_path = root / "image_refs" / f"{digest}.{extension}"
    try:
        _atomic_write(original_path, raw)
    except OSError:
        return False

    record.width = width
    record.height = height
    record.sha256 = digest
    record.original_path = original_path
    record.image = image
    record.prepared = True
    return True


def _source_extension(image_format: str, mime: str) -> str:
    extension = _FORMAT_EXTENSIONS.get(image_format)
    if extension:
        return extension
    _, slash, subtype = mime.partition("/")
    candidate = subtype.lower() if slash else ""
    if candidate == "jpeg":
        return "jpg"
    if re.fullmatch(r"[a-z0-9]{1,12}", candidate):
        return candidate
    return "bin"


def _compress_record(
    record: _ImageRecord,
    root: Path,
    profile: tuple[int, int],
) -> int | None:
    if record.image is None or not record.sha256:
        return None
    max_edge, quality = profile
    profile_name = f"e{max_edge}_q{quality}"
    cache_path = root / "image_cache" / record.sha256 / f"{profile_name}.jpg"
    try:
        if cache_path.is_file():
            compressed = cache_path.read_bytes()
            try:
                os.utime(cache_path, None)
            except OSError:
                pass
        else:
            compressed = _render_jpeg(record.image, max_edge, quality)
            _atomic_write(cache_path, compressed)
    except (OSError, ValueError):
        return None

    encoded = base64.b64encode(compressed).decode("ascii")
    estimated = _estimate_tokens(encoded)
    if estimated >= record.estimated:
        return None
    _set_data_image_url(record.block, f"data:image/jpeg;base64,{encoded}")
    return estimated


def _render_jpeg(image: Image.Image, max_edge: int, quality: int) -> bytes:
    width, height = image.size
    scale = min(1.0, max_edge / max(width, height))
    resized = image
    if scale < 1.0:
        target = (max(1, round(width * scale)), max(1, round(height * scale)))
        resized = image.resize(target, Image.Resampling.LANCZOS)

    if resized.mode in ("RGBA", "LA") or "transparency" in resized.info:
        rgba = resized.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, "white")
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        resized = canvas
    elif resized.mode != "RGB":
        resized = resized.convert("RGB")

    output = io.BytesIO()
    resized.save(
        output,
        format="JPEG",
        quality=quality,
        subsampling=1 if quality >= 88 else 2,
    )
    return output.getvalue()


def _set_data_image_url(block: dict[str, Any], url: str) -> None:
    value = block.get("image_url")
    if isinstance(value, dict):
        value["url"] = url
    else:
        block["image_url"] = {"url": url}


def _placeholder_for(record: _ImageRecord) -> str:
    assert record.original_path is not None
    path = html.escape(str(record.original_path), quote=True)
    note = html.escape(_PLACEHOLDER_NOTE, quote=True)
    return (
        f'<image_ref id="{record.sha256}" path="{path}" '
        f'width="{record.width}" height="{record.height}" note="{note}" />'
    )


def _has_view_image_tool(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        if isinstance(function, Mapping) and function.get("name") == "view_image":
            return True
        if tool.get("type") == "function" and tool.get("name") == "view_image":
            return True
    return False


def _placeholders_enabled() -> bool:
    value = os.environ.get("WB_IMAGE_BUDGET_PLACEHOLDERS", "auto").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    try:
        from server.services.updater import in_container

        return not in_container()
    except Exception:  # noqa: BLE001
        return False


def _ensure_cache_dirs(root: Path) -> None:
    (root / "image_refs").mkdir(parents=True, exist_ok=True)
    (root / "image_cache").mkdir(parents=True, exist_ok=True)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=".tmp-", suffix=".part", dir=str(path.parent)
        )
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _cleanup(root: Path, *, ttl_days: int, max_bytes: int) -> None:
    """Remove expired cache entries, then enforce the combined size cap."""

    candidates = _cache_files(root)
    if not candidates:
        return
    now = time.time()
    ttl_seconds = ttl_days * 24 * 60 * 60
    remaining: list[tuple[float, int, Path]] = []
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        if now - stat.st_mtime > ttl_seconds:
            _unlink_quietly(path)
            continue
        remaining.append((stat.st_mtime, stat.st_size, path))

    total = sum(size for _, size, _ in remaining)
    if total <= max_bytes:
        return

    # Cache files are regenerable, so evict them before originals.
    eviction_order = sorted(
        remaining,
        key=lambda item: (0 if "image_cache" in item[2].parts else 1, item[0]),
    )
    for _, size, path in eviction_order:
        if total <= max_bytes:
            break
        _unlink_quietly(path)
        total -= size


def _cache_files(root: Path) -> list[Path]:
    files: list[Path] = []
    refs = root / "image_refs"
    cache = root / "image_cache"
    try:
        files.extend(path for path in refs.iterdir() if path.is_file())
    except OSError:
        pass
    try:
        for directory in cache.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            files.extend(path for path in directory.iterdir() if path.is_file())
    except OSError:
        pass
    return files


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return
    try:
        path.parent.rmdir()
    except OSError:
        pass


__all__ = ["ImageBudgetStats", "apply_image_budget"]
