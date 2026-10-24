"""Bounded model fallback for exhausted model/account pools.

The fallback chain is intentionally narrow.  A model is tried next only when
the upstream explicitly reports a rate limit or an empty account pool; other
4xx/5xx responses are returned unchanged.  Configuration and response parsing
live here so Chat, Responses and Anthropic use exactly the same policy.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import math
import re
import threading
import time

MAX_FALLBACKS = 3
MAX_ATTEMPTS = 4
MAX_MODEL_LENGTH = 128
DEFAULT_EXHAUSTED_COOLDOWN = 120
MAX_LOCAL_EXHAUSTED_COOLDOWN = 900

_MODEL_RE = re.compile(r'^[!-~]+$')
_DATE_RE = re.compile(
    r'20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?'
    r'(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}|'
    r'\s*UTC[+-]\d{1,2}(?::?\d{2})?)?',
    re.IGNORECASE,
)
_SECONDS_RE = re.compile(
    r'(?:after|wait|retry(?:\s+in)?|reset(?:\s+(?:in|after))?|请在|等待|重试)'
    r'\s*(\d{1,6})\s*(?:seconds?|secs?|s|秒)',
    re.IGNORECASE,
)
_RATE_TEXT_RE = re.compile(
    r'(?:频率限制|限流|too many requests|rate[\s_-]*limit(?:ed|_exceeded)?)',
    re.IGNORECASE,
)

# (upstream base URL, actual model) -> monotonic deadline.  A confirmed
# model/pool exhaustion is remembered long enough to stop a client's immediate
# retry loop from scanning every account again.  This is process-local, matching
# the project's default single-process deployment.
_exhausted_until: dict[tuple[str, str], float] = {}
_exhausted_lock = threading.Lock()


def scope_for(base_url: object, api_key: object) -> str:
    """Identify one upstream credential scope without retaining the secret.

    The same address can host several isolated account pools selected by API
    key.  Keying cooldowns only by URL would let one pool's 429 suppress a
    healthy sibling pool for up to fifteen minutes.
    """
    base = str(base_url or '').strip().rstrip('/')
    credential = hashlib.sha256(
        str(api_key or '').encode('utf-8'),
    ).hexdigest()
    return f'{base}|{credential}'


def _model_name(raw: object, *, label: str = '模型名') -> str:
    name = str(raw).strip() if isinstance(raw, str) else ''
    if not name:
        raise ValueError(f'{label}不能为空')
    if len(name) > MAX_MODEL_LENGTH:
        raise ValueError(f'{label}过长（最多 {MAX_MODEL_LENGTH} 字符）')
    if name == '*' or not _MODEL_RE.fullmatch(name):
        raise ValueError(f'{label}必须是可见 ASCII 字符且不能包含空白')
    return name


def validate(raw: object) -> dict[str, list[str]]:
    """Validate and normalize a ``{primary: [fallback, ...]}`` setting."""
    if not isinstance(raw, dict):
        raise ValueError('模型降级链必须是一个对象')
    clean: dict[str, list[str]] = {}
    for raw_primary, raw_fallbacks in raw.items():
        primary = _model_name(raw_primary, label='主模型名')
        if not isinstance(raw_fallbacks, list):
            raise ValueError(f'模型 {primary} 的降级链必须是数组')
        if not raw_fallbacks:
            raise ValueError(f'模型 {primary} 的降级链不能为空数组')
        if len(raw_fallbacks) > MAX_FALLBACKS:
            raise ValueError(
                f'模型 {primary} 最多配置 {MAX_FALLBACKS} 个降级模型'
            )
        seen: set[str] = set()
        fallbacks: list[str] = []
        for value in raw_fallbacks:
            candidate = _model_name(value, label=f'模型 {primary} 的降级模型名')
            if candidate == primary:
                raise ValueError(f'模型 {primary} 不能降级到自身')
            if candidate in seen:
                continue
            seen.add(candidate)
            fallbacks.append(candidate)
        if not fallbacks:
            raise ValueError(f'模型 {primary} 的降级链没有有效模型')
        clean[primary] = fallbacks
    return clean


def load(raw: object) -> dict[str, list[str]]:
    """Return a safe setting; malformed persisted data disables fallback."""
    try:
        return validate(raw)
    except (TypeError, ValueError):
        return {}


def chain_for(
    primary: str | None,
    mapped: str | None,
    *,
    table: object,
    key: dict,
    model_map: object = None,
) -> list[str]:
    """Return the bounded physical model sequence for one client request.

    ``primary`` is the client-visible name and ``mapped`` is the model-map
    target.  The actual target is preferred when both are configured, so an
    alias still resolves through the chain for the real upstream model.

    Every fallback must pass the key's realm and whitelist before it can be
    considered.  Whitelist checks deliberately use the configured candidate
    name (the same client-visible semantics as request authorization), while
    realm checks use the final model-map target that will actually be sent to
    the upstream.
    """
    from .. import keysvc
    from . import modelmap

    if not primary and not mapped:
        return []
    config = load(table)
    if not config:
        return []

    effective = mapped or primary or ''
    fallbacks = config.get(effective)
    if fallbacks is None and primary and primary != effective:
        fallbacks = config.get(primary)
    if not fallbacks:
        return []

    want_realm = keysvc._norm_realm(key.get('realm'))
    out: list[str] = []
    seen_actual: set[str] = set()
    # `mapped` is already the final result of the one allowed model-map lookup.
    # Applying `model_map` again would turn an exact alias target back into a
    # wildcard target and silently send a different physical model.
    first_actual = mapped or effective
    seen_actual.add(first_actual)
    for candidate in fallbacks[:MAX_FALLBACKS]:
        if not keysvc.model_allowed(key, candidate):
            continue
        actual = modelmap.target_for(model_map, candidate) or candidate
        if not actual or actual in seen_actual:
            continue
        got_realm = 'global' if actual.strip().lower().startswith('global:') else 'cn'
        if want_realm and got_realm != want_realm:
            continue
        seen_actual.add(actual)
        out.append(actual)
        if len(out) >= MAX_ATTEMPTS - 1:
            break
    return out


def build_plan(
    primary: str | None,
    mapped: str | None,
    *,
    table: object,
    key: dict,
    model_map: object = None,
) -> list[str]:
    """Return at most four actual upstream model names."""
    first = mapped or modelmap.target_for(model_map, primary) or primary
    if not first:
        return []
    return ([first] + chain_for(
        primary, mapped, table=table, key=key, model_map=model_map
    ))[:MAX_ATTEMPTS]


def _walk_error(data: object) -> tuple[list[object], list[str]]:
    """Collect error codes and human-readable text without trusting shape."""
    codes: list[object] = []
    texts: list[str] = []
    stack = [data]
    seen: set[int] = set()
    while stack:
        value = stack.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, Mapping):
            for name in ('code', 'status', 'error_code'):
                if name in value:
                    codes.append(value[name])
            for name in ('message', 'msg', 'detail', 'error_description'):
                item = value.get(name)
                if isinstance(item, str) and item.strip():
                    texts.append(item.strip())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str) and value.strip():
            texts.append(value.strip())
    return codes, texts


def is_fallback_trigger(
    status: int,
    data: object = None,
    text: str | None = None,
) -> bool:
    """Whether an upstream response means this model/account pool is exhausted."""
    codes, texts = _walk_error(data)
    if text and text.strip():
        texts.append(text.strip())
    joined = '\n'.join(texts)
    normalized_codes = {str(code).strip().lower() for code in codes if code is not None}

    if status == 429:
        return (
            '6004' in normalized_codes
            or 'rate_limit_exceeded' in normalized_codes
            or bool(re.search(
                r'["\']?(?:error_)?code["\']?\s*[:=]\s*["\']?'
                r'(?:6004|rate_limit_exceeded)\b',
                joined,
                re.IGNORECASE,
            ))
            or bool(_RATE_TEXT_RE.search(joined))
        )
    if status == 503:
        return 'no_healthy_account' in normalized_codes or any(
            'no_healthy_account' in text.lower() or 'no healthy account' in text.lower()
            for text in texts
        )
    return False


def _header(headers: object, name: str) -> str:
    if not isinstance(headers, Mapping) and not hasattr(headers, 'items'):
        return ''
    wanted = name.lower()
    try:
        for key, value in headers.items():  # type: ignore[union-attr]
            if str(key).lower() == wanted and value is not None:
                return str(value).strip()
    except (AttributeError, TypeError, ValueError):
        return ''
    return ''


def _safe_header_value(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 128 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return ''
    return value


def _seconds_until(raw: str, *, now: datetime | None = None) -> int | None:
    match = _SECONDS_RE.search(raw)
    if match:
        return max(0, min(int(match.group(1)), 86_400))
    match = _DATE_RE.search(raw)
    if not match:
        return None
    stamp = match.group(0)
    stamp = re.sub(r'^(\d{4}-\d{2}-\d{2})\s+', r'\1T', stamp)
    if stamp.endswith('Z'):
        stamp = stamp[:-1] + '+00:00'
    # 腾讯 6004 文案使用 `2026-10-24 20:35:49 UTC+8`。datetime.fromisoformat
    # 不认 `UTC+8`，且若只截到秒后按 UTC 解析，会整整多算 8 小时。
    stamp = re.sub(
        r'\s*UTC([+-])(\d{1,2})(?::?(\d{2}))?$',
        lambda m: f'{m.group(1)}{int(m.group(2)):02d}:{m.group(3) or "00"}',
        stamp,
        flags=re.IGNORECASE,
    )
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0, min(int((when - current).total_seconds()), 86_400))


def retry_after_seconds(
    headers: object,
    data: object = None,
    text: str | None = None,
    *,
    now: datetime | None = None,
) -> int | None:
    """Return the upstream's retry delay when it is safe and unambiguous."""
    raw = _safe_header_value(_header(headers, 'retry-after'))
    if raw.isdigit():
        return max(0, min(int(raw), 86_400))
    if raw:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            return max(
                0,
                min(int((when - current).total_seconds()), 86_400),
            )

    retry_ms = _safe_header_value(_header(headers, 'retry-after-ms'))
    if retry_ms.isdigit():
        return max(0, min((int(retry_ms) + 999) // 1000, 86_400))

    _, texts = _walk_error(data)
    if text:
        texts.append(text)
    return _seconds_until('\n'.join(texts), now=now)


def failure_headers(
    headers: object,
    data: object = None,
    text: str | None = None,
) -> dict[str, str]:
    """Build safe response headers for the final exhausted/failure response."""
    out: dict[str, str] = {}
    retry_after = _safe_header_value(_header(headers, 'retry-after'))
    retry_ms = _safe_header_value(_header(headers, 'retry-after-ms'))
    ratelimit_reset = _safe_header_value(_header(headers, 'x-ratelimit-reset'))
    if retry_after:
        out['Retry-After'] = retry_after
    if retry_ms:
        out['retry-after-ms'] = retry_ms
    if ratelimit_reset:
        out['x-ratelimit-reset'] = ratelimit_reset

    if not retry_after:
        parsed = retry_after_seconds(headers, data, text)
        if parsed is not None:
            out['Retry-After'] = str(parsed)
    out['X-Should-Retry'] = 'false'
    return out


def note_exhausted(
    scope: str,
    model: str,
    headers: object = None,
    data: object = None,
    text: str | None = None,
    *,
    clock=None,
    allow_default: bool = True,
) -> int:
    """Remember a confirmed exhausted model and return its cooldown seconds."""
    now = (clock or time.monotonic)()
    seconds = retry_after_seconds(headers, data, text)
    if seconds is None:
        if not allow_default:
            # A bare 429/503 without a reset hint should close this request, but
            # should not poison later independent requests.  The default
            # cooldown is only useful when a configured fallback lets us skip
            # this model for the next attempt.
            return 0
        seconds = DEFAULT_EXHAUSTED_COOLDOWN
    # Keep retry-storm suppression bounded: a new account or a manually cleared
    # upstream cooldown must become usable again within 15 minutes without a
    # panel restart, even when the textual reset window is many hours away.
    seconds = max(1, min(int(seconds), MAX_LOCAL_EXHAUSTED_COOLDOWN))
    key = (str(scope or ''), str(model or ''))
    with _exhausted_lock:
        _exhausted_until[key] = max(_exhausted_until.get(key, 0.0), now + seconds)
    return seconds


def available_plan(
    plan: list[str],
    scope: str,
    *,
    clock=None,
) -> tuple[list[str], int]:
    """Filter locally cooled models; if none remain return earliest retry delay.

    The returned delay is only meaningful for an empty ready list.  Keeping the
    original model order means a healthy fallback can be used immediately while
    the exhausted primary cools down.
    """
    now = (clock or time.monotonic)()
    ready: list[str] = []
    waits: list[float] = []
    with _exhausted_lock:
        for model in plan:
            key = (str(scope or ''), str(model or ''))
            deadline = _exhausted_until.get(key, 0.0)
            if deadline <= now:
                _exhausted_until.pop(key, None)
                ready.append(model)
            else:
                waits.append(deadline - now)
        # Expired entries for unrelated models should not accumulate forever.
        if len(_exhausted_until) > 1024:
            for key, deadline in list(_exhausted_until.items()):
                if deadline <= now:
                    _exhausted_until.pop(key, None)
    retry_after = max(1, math.ceil(min(waits))) if waits and not ready else 0
    return ready, retry_after


def reset_exhaustion_state() -> None:
    """Clear process-local model cooldowns (tests and explicit operator resets)."""
    with _exhausted_lock:
        _exhausted_until.clear()


def local_cooldown(model: str | None, seconds: int) -> tuple[str, dict[str, str]]:
    """Message and headers for an exhausted chain that is still cooling."""
    wait = max(1, int(seconds or 1))
    return (
        f'模型 {model or "当前模型"} 的候选链都在限流冷却中，预计 {wait} 秒后可重试',
        {
            'Retry-After': str(wait),
            'X-Should-Retry': 'false',
            'Cache-Control': 'no-store',
        },
    )


async def read_error_body(response: object) -> bytes:
    """Read a pre-stream error body across httpx and test doubles."""
    aread = getattr(response, 'aread', None)
    if callable(aread):
        try:
            value = await aread()
            if isinstance(value, bytes):
                return value
            if isinstance(value, str):
                return value.encode()
            return bytes(value)
        except Exception:  # noqa: BLE001
            pass
    iterator = getattr(response, 'aiter_bytes', None)
    if not callable(iterator):
        text = getattr(response, 'text', '')
        return text.encode() if isinstance(text, str) else b''
    chunks: list[bytes] = []
    try:
        async for chunk in iterator():
            if isinstance(chunk, bytes):
                chunks.append(chunk)
            elif isinstance(chunk, str):
                chunks.append(chunk.encode())
    except Exception:  # noqa: BLE001
        return b''.join(chunks)
    return b''.join(chunks)
