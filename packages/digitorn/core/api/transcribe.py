"""POST /api/transcribe - voice → text for the Flutter mic button.

Contract (frozen client-side, see ``docs/voice_transcription_contract.md``):

    POST /api/transcribe
    Content-Type: multipart/form-data
    Authorization: Bearer <jwt>

    audio:    file    (required, .m4a / .webm / .wav, max 25 MB)
    language: string  (optional, BCP-47; None = auto-detect)
    app_id:   string  (optional, unused for now)

    200 OK:
        {"success": true, "data": {"text": "...", "language": "fr",
                                    "duration_ms": 3200, "confidence": 0.96}}
    413:  audio too large
    422:  audio too short / empty / unreadable / transcript empty
    500:  provider failure

Two providers:

* ``local`` (default) - `faster-whisper` in-process. Needs ffmpeg on
  PATH. Weights cached on first load. Thread-safe for inference.
* ``openai`` - calls the OpenAI ``whisper-1`` endpoint. Needs
  OPENAI_API_KEY.

**Privacy**: the audio file is held in memory + a short-lived tmpfile
deleted immediately after transcription. We log the user_id, size,
duration and language - **never the transcript**.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from digitorn.core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/transcribe", tags=["transcribe"])


# BUG-096: only decoders we're confident about. Anything else
# (image/*, application/*, x-executable, …) is rejected at the gate.
# The allowlist is narrow on purpose - adding a codec is a conscious
# decision, not an accident.
_ALLOWED_MIME = frozenset({
    "audio/webm", "audio/ogg", "audio/opus",
    "audio/mp3", "audio/mpeg",
    "audio/m4a", "audio/mp4", "audio/x-m4a",
    "audio/wav", "audio/x-wav", "audio/wave",
    "audio/flac", "audio/x-flac",
    "audio/aac",
    # A few HTML5 <input type=file> stacks report
    # ``application/octet-stream`` for .webm - keep it only when the
    # extension is one of our audio extensions (enforced below).
    "application/octet-stream",
})
_ALLOWED_EXT = frozenset({
    ".webm", ".ogg", ".opus", ".mp3", ".m4a", ".mp4",
    ".wav", ".flac", ".aac",
})

# BUG-088: zip-bomb-style audio bombs. The streamed upload is capped
# by ``max_audio_bytes`` BEFORE decode, but a valid-looking small file
# that expands to hundreds of MB after ffmpeg decode can still drown
# the process. Hard-cap the decoded PCM length via a timeout and the
# semaphore - but also reject files whose magic bytes don't match a
# known audio container, since most bombs rely on a spoofed header.
_AUDIO_MAGIC = (
    b"OggS",         # Ogg / Opus / Vorbis
    b"RIFF",         # WAV
    b"fLaC",         # FLAC
    b"\x1aE\xdf\xa3",  # Matroska / WebM (EBML)
    b"ID3",           # MP3 with ID3 tag
    b"\xff\xfb", b"\xff\xf3", b"\xff\xf2",  # MP3 frame sync
    b"ftyp",          # MP4/M4A (after 4-byte size prefix)
)


def _looks_like_audio(head: bytes) -> bool:
    """True when the first ~16 bytes match a known audio container."""
    if not head:
        return False
    # ``ftyp`` appears at offset 4 in MP4/M4A containers.
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return True
    for magic in _AUDIO_MAGIC:
        if head.startswith(magic):
            return True
    return False


# BUG-095: per-user + per-IP rate limit. Sliding window of 60 s.
_RATE_LIMIT_RPM = 30
_rate_window: dict[str, list[float]] = {}
_rate_lock = asyncio.Lock()


async def _check_rate_limit(user_id: str, ip: str) -> None:
    """Raise 429 if either the user or the IP exceeded ``_RATE_LIMIT_RPM``
    requests in the past minute. Both axes are enforced so a bucket
    burned by a single user doesn't lock the whole IP, and vice-versa.
    """
    now = time.monotonic()
    window_start = now - 60.0
    async with _rate_lock:
        for key in (f"u:{user_id}", f"ip:{ip}"):
            bucket = _rate_window.get(key)
            if bucket is None:
                bucket = []
                _rate_window[key] = bucket
            # Drop old entries and count what's left.
            _rate_window[key] = [t for t in bucket if t >= window_start]
            if len(_rate_window[key]) >= _RATE_LIMIT_RPM:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "rate_limited",
                        "limit_per_minute": _RATE_LIMIT_RPM,
                        "scope": key.split(":", 1)[0],
                        "retry_after": 60,
                    },
                    headers={"Retry-After": "60"},
                )
        _rate_window[f"u:{user_id}"].append(now)
        _rate_window[f"ip:{ip}"].append(now)


# ── Response envelope (matches the rest of the daemon) ────────────────


class TranscribeResponse(BaseModel):
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


# ── Lazy model loader (local provider) ────────────────────────────────


_model_lock = asyncio.Lock()
_model: Any = None
_model_error: Exception | None = None
# Semaphore guards concurrent inference on the single shared model.
# Sized from ``transcribe.max_concurrency`` at first use.
_inference_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _inference_semaphore
    if _inference_semaphore is None:
        cfg = get_settings().transcribe
        _inference_semaphore = asyncio.Semaphore(max(1, cfg.max_concurrency))
    return _inference_semaphore


async def preload_model() -> dict[str, Any]:
    """Eagerly load the Whisper model at daemon startup.

    Called by ``server.py`` lifespan when ``transcribe.preload=true``.
    Returns a small dict describing the outcome (for logs / telemetry).
    Never raises - a failed preload leaves the endpoint in lazy-load
    mode and is reported via ``/api/transcribe/health``.
    """
    cfg = get_settings().transcribe
    info: dict[str, Any] = {
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "preloaded": False,
        "reason": None,
    }
    if not cfg.enabled:
        info["reason"] = "transcribe.enabled=false"
        return info
    if cfg.provider != "local":
        info["reason"] = f"provider={cfg.provider} - nothing to preload"
        return info
    try:
        started = time.monotonic()
        await _get_local_model()
        info["preloaded"] = True
        info["model"] = cfg.model
        info["device"] = cfg.device
        info["compute_type"] = cfg.compute_type
        info["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        logger.info(
            "transcribe_model_preloaded model=%s device=%s compute=%s elapsed=%dms",
            cfg.model, cfg.device, cfg.compute_type, info["elapsed_ms"],
        )
    except Exception as exc:
        info["reason"] = f"{type(exc).__name__}: {exc}"
        logger.warning("transcribe_preload_failed: %s", exc)
    return info


async def _get_local_model() -> Any:
    """Load the faster-whisper model once per process. Thread-safe."""
    global _model, _model_error
    if _model is not None:
        return _model
    if _model_error is not None:
        raise _model_error
    async with _model_lock:
        if _model is not None:
            return _model
        cfg = get_settings().transcribe
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            _model_error = RuntimeError(
                "faster-whisper not installed. Install with "
                "`pip install faster-whisper` or set "
                "transcribe.provider=openai in config."
            )
            raise _model_error from exc
        try:
            device = cfg.device
            if device == "auto":
                # faster-whisper accepts "auto" directly
                device = "auto"
            _model = await asyncio.to_thread(
                WhisperModel,
                cfg.model,
                device=device,
                compute_type=cfg.compute_type,
            )
            logger.info(
                "whisper_model_loaded model=%s device=%s compute=%s",
                cfg.model, device, cfg.compute_type,
            )
            return _model
        except Exception as exc:
            _model_error = exc
            raise


async def _transcribe_local(
    audio_path: str, language: str | None, timeout_s: float,
) -> dict[str, Any]:
    """Run faster-whisper inference on a file, return normalised result.

    Serialises concurrent calls through ``_inference_semaphore`` to match
    ``transcribe.max_concurrency`` - prevents two requests from stepping
    on the same model instance when ``shared_instance=true``.
    """
    model = await _get_local_model()
    sem = _get_semaphore()

    def _run() -> dict[str, Any]:
        segments, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=1,
            vad_filter=True,
        )
        seg_list = list(segments)
        text = " ".join((s.text or "").strip() for s in seg_list).strip()
        # Duration / avg confidence from segments
        duration_ms = 0
        confidences: list[float] = []
        for s in seg_list:
            if getattr(s, "end", None) is not None:
                duration_ms = max(duration_ms, int(s.end * 1000))
            # faster-whisper sets avg_logprob per segment - map to [0,1]
            alp = getattr(s, "avg_logprob", None)
            if alp is not None:
                # avg_logprob is typically in [-1, 0]; clamp + shift
                conf = max(0.0, min(1.0, 1.0 + float(alp)))
                confidences.append(conf)
        confidence = (
            sum(confidences) / len(confidences) if confidences else None
        )
        return {
            "text": text,
            "language": getattr(info, "language", None) or language,
            "duration_ms": duration_ms or None,
            "confidence": confidence,
        }

    async with sem:
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s)


async def _resolve_openai_api_key(
    request: Request, app_id: str | None = None,
) -> str | None:
    """Fetch the OpenAI API key from the credentials store.

    Resolution order (4-scope + env fallback):
      1. per_app_per_user → 2. per_user → 3. per_app_shared → 4. system_wide
      5. ``OPENAI_API_KEY`` env var (final fallback for dev / legacy)

    Returns the key string or None if nothing is configured. The caller
    raises a 500 / logs an actionable error if None.
    """
    try:
        from digitorn.core.credentials.store import CredentialStore  # type: ignore
        store: CredentialStore | None = getattr(
            request.app.state, "credential_store", None,
        )
        if store is not None:
            uid = getattr(request.state, "user_id", None)
            if uid in ("system", "anonymous"):
                uid = None
            key = await store.resolve_field(
                provider_or_field="openai.api_key",
                user_id=uid,
                app_id=app_id,
            )
            if key:
                return key
    except Exception as exc:
        logger.warning("transcribe_credentials_lookup_failed: %s", exc)
    # Fallback: env var (useful for local dev + CI).
    return os.environ.get("OPENAI_API_KEY") or None


async def _transcribe_openai(
    audio_path: str, language: str | None, timeout_s: float,
    *, api_key: str,
) -> dict[str, Any]:
    """Call OpenAI's whisper-1 endpoint with an explicit API key."""
    try:
        import openai  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "openai package not installed. `pip install openai`."
        ) from exc
    if not api_key:
        raise RuntimeError(
            "OpenAI API key not found. Add it via the Digitorn credentials "
            "system (provider='openai', field='api_key'), or set the "
            "OPENAI_API_KEY env var."
        )

    def _run() -> dict[str, Any]:
        client = openai.OpenAI(api_key=api_key)
        with open(audio_path, "rb") as f:
            resp = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language=language or None,
                response_format="verbose_json",
            )
        text = (getattr(resp, "text", "") or "").strip()
        return {
            "text": text,
            "language": getattr(resp, "language", None) or language,
            "duration_ms": (
                int(float(resp.duration) * 1000)
                if getattr(resp, "duration", None) else None
            ),
            "confidence": None,
        }

    return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s)


# ── Endpoint ──────────────────────────────────────────────────────────


@router.post("", response_model=TranscribeResponse)
@router.post("/", response_model=TranscribeResponse)
async def transcribe(
    request: Request,
    audio: UploadFile = File(..., description="Audio file (.m4a / .webm / .wav)."),
    # BUG-097: cap the free-form form fields so a 20 kB ``language``
    # can't be used as a DoS memory amplifier. ``language`` is a
    # BCP-47 tag (at most ~10 chars realistically); ``app_id`` is
    # identifier-safe, also short.
    language: str | None = Form(default=None, max_length=64, description="BCP-47 hint (fr, en-US). Auto-detect if omitted."),
    app_id: str | None = Form(default=None, max_length=64, description="Current app_id (for future vocab biasing)."),
) -> TranscribeResponse:
    """Transcribe an uploaded audio blob to text.

    See the module docstring for the full contract and error codes.
    """
    cfg = get_settings().transcribe
    if not cfg.enabled:
        # Treat "disabled" the same as "not implemented" so the client's
        # 404 fallback path kicks in (attaches audio to next message).
        raise HTTPException(status_code=404, detail="transcription disabled")

    # BUG-086 follow-up: refuse anonymous callers even though the auth
    # middleware drops the loopback bypass for this path. Defence-in-
    # depth: if someone puts the daemon behind a permissive reverse
    # proxy later, this check still fires.
    user_id = getattr(request.state, "user_id", None)
    if not user_id or user_id == "anonymous":
        raise HTTPException(status_code=401, detail="Authentication required")

    # BUG-095: rate limit by user_id + by IP. Cheap sliding window.
    client_host = getattr(getattr(request, "client", None), "host", "") or "0.0.0.0"
    await _check_rate_limit(user_id, client_host)

    # BUG-087: an empty filename used to crash at ``os.path.splitext``
    # with a confusing 500. Reject explicitly.
    raw_filename = (audio.filename or "").strip()
    if not raw_filename:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "missing_filename",
                "message": "The uploaded file has no name. Set a filename like 'recording.webm'.",
            },
        )

    # BUG-096: reject dangerous filenames at the gate - path traversal
    # and null bytes used to be silently accepted by the tempfile path.
    if (
        "\x00" in raw_filename
        or "/" in raw_filename
        or "\\" in raw_filename
        or ".." in raw_filename
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_filename",
                "message": "Filename must not contain path separators or null bytes.",
            },
        )

    # BUG-096: MIME allowlist. ``content_type`` may be missing or lying
    # (browsers sometimes send octet-stream), so we verify the file
    # extension too - either must be in the allowlist.
    ctype = (audio.content_type or "").split(";", 1)[0].strip().lower()
    ext = os.path.splitext(raw_filename)[1].lower()
    if ctype and ctype not in _ALLOWED_MIME:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "unsupported_mime",
                "got": ctype,
                "allowed": sorted(_ALLOWED_MIME),
            },
        )
    if ext and ext not in _ALLOWED_EXT:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "unsupported_extension",
                "got": ext,
                "allowed": sorted(_ALLOWED_EXT),
            },
        )
    if not ctype and not ext:
        raise HTTPException(
            status_code=415,
            detail="Audio file must have a recognised MIME type or extension.",
        )

    # Read the audio with a hard size cap. Read cap+1 so we can detect
    # "exactly at the limit + one byte" and reject.
    cap = cfg.max_audio_bytes
    content = await audio.read(cap + 1)
    size = len(content)

    if size > cap:
        logger.info(
            "transcribe_rejected user=%s reason=too_large size=%d cap=%d",
            user_id, size, cap,
        )
        raise HTTPException(status_code=413, detail=f"Audio too large (max {cap // 1024 // 1024} MB)")
    if size < cfg.min_audio_bytes:
        logger.info(
            "transcribe_rejected user=%s reason=too_short size=%d",
            user_id, size,
        )
        raise HTTPException(status_code=422, detail="Audio too short or empty")

    # BUG-088: magic-byte sanity check. Most zip / decompression bombs
    # present a non-audio header; rejecting them here means ffmpeg
    # never gets a chance to expand them into hundreds of MB of PCM.
    if not _looks_like_audio(content[:16]):
        logger.info(
            "transcribe_rejected user=%s reason=bad_magic size=%d filename=%r",
            user_id, size, raw_filename,
        )
        raise HTTPException(
            status_code=415,
            detail={
                "error": "not_an_audio_file",
                "message": (
                    "File header does not look like a known audio "
                    "container (Ogg/WebM/WAV/MP3/FLAC/MP4). Re-encode "
                    "and retry."
                ),
            },
        )

    # Preserve the original extension so the decoder picks the right
    # demuxer (.m4a, .webm, .wav, .mp3, .ogg…).
    filename = raw_filename
    suffix = ext or ".m4a"

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(content)
        tmp.flush()
        tmp.close()
        started = time.monotonic()
        try:
            if cfg.provider == "openai":
                api_key = await _resolve_openai_api_key(request, app_id=app_id)
                if not api_key:
                    logger.warning(
                        "transcribe_openai_key_missing user=%s",
                        user_id,
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "OpenAI API key not configured. Add it under "
                            "provider='openai' in the credentials system."
                        ),
                    )
                result = await _transcribe_openai(
                    tmp.name, language, cfg.timeout_seconds,
                    api_key=api_key,
                )
            else:
                result = await _transcribe_local(
                    tmp.name, language, cfg.timeout_seconds,
                )
        except asyncio.TimeoutError:
            logger.warning(
                "transcribe_timeout user=%s size=%d timeout=%.1f",
                user_id, size, cfg.timeout_seconds,
            )
            raise HTTPException(status_code=500, detail="Transcription timed out")
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception(
                "transcribe_provider_failed user=%s provider=%s: %s",
                user_id, cfg.provider, exc,
            )
            # BUG-085: classify the failure so the client gets an
            # actionable error instead of "RuntimeError". We do NOT
            # echo arbitrary exception text (could leak paths,
            # credentials, or internal state) - only a stable
            # category + a curated human message.
            name = type(exc).__name__
            text = str(exc).lower()
            if "ffmpeg" in text or "decode" in text or name in (
                "DecodeError", "ValueError",
            ):
                classified = "audio_decode_failed"
                human = (
                    "The audio file could not be decoded. Re-record "
                    "or convert to WAV/MP3 and retry."
                )
                status = 422
            elif "connect" in text or "timeout" in text or name in (
                "ConnectionError", "APITimeoutError", "APIConnectionError",
            ):
                classified = "provider_unavailable"
                human = (
                    "The transcription provider is unreachable. "
                    "Retry in a moment."
                )
                status = 503
            elif "api" in text and ("key" in text or "auth" in text):
                classified = "provider_auth_failed"
                human = (
                    "The transcription provider rejected the API "
                    "key. Reconfigure the openai credential."
                )
                status = 502
            else:
                classified = "transcription_failed"
                human = (
                    "Transcription failed with an unexpected error. "
                    "Check the daemon logs for the full traceback."
                )
                status = 500
            raise HTTPException(
                status_code=status,
                detail={"error": classified, "message": human},
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        text = (result.get("text") or "").strip()
        if not text:
            logger.info(
                "transcribe_empty user=%s size=%d elapsed=%dms",
                user_id, size, elapsed_ms,
            )
            raise HTTPException(status_code=422, detail="Transcription returned empty text")

        logger.info(
            "transcribe_ok user=%s size=%d lang=%s duration_ms=%s elapsed=%dms",
            user_id, size,
            result.get("language"),
            result.get("duration_ms"),
            elapsed_ms,
        )

        payload: dict[str, Any] = {"text": text}
        if result.get("language"):
            payload["language"] = result["language"]
        if result.get("duration_ms"):
            payload["duration_ms"] = result["duration_ms"]
        if result.get("confidence") is not None:
            payload["confidence"] = round(float(result["confidence"]), 4)

        return TranscribeResponse(success=True, data=payload)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@router.get("/health")
async def transcribe_health(request: Request) -> dict[str, Any]:
    """Report transcribe subsystem status (probes model availability).

    For ``provider=openai`` the API key is resolved via the credentials
    store (per-user → per-app → system → env-var fallback). The response
    does NOT expose the key itself - only whether it's reachable.

    BUG-094: anonymous callers get a minimal ``{enabled, ready}`` view
    so a drive-by scanner can't fingerprint the provider / model /
    device / preload state. Authenticated callers keep the full
    diagnostics view used by the dashboard.
    """
    cfg = get_settings().transcribe
    uid = getattr(request.state, "user_id", None)
    is_anon = not uid or uid == "anonymous"

    if is_anon:
        # Narrow view: just whether the endpoint is usable at all.
        return {"enabled": cfg.enabled, "ready": cfg.enabled}

    info: dict[str, Any] = {
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "model": cfg.model if cfg.provider == "local" else "whisper-1",
        "preload": cfg.preload,
        "model_loaded": _model is not None,
        "max_concurrency": cfg.max_concurrency,
        "ready": False,
        "error": None,
    }
    if not cfg.enabled:
        return info
    try:
        if cfg.provider == "openai":
            api_key = await _resolve_openai_api_key(request)
            info["ready"] = bool(api_key)
            if not info["ready"]:
                info["error"] = (
                    "OpenAI API key not configured (credentials store / "
                    "OPENAI_API_KEY env var both empty)"
                )
        else:
            # Don't actually load the model for health - just check import.
            try:
                import faster_whisper  # type: ignore  # noqa: F401
                info["ready"] = True
            except ImportError:
                info["error"] = "faster-whisper not installed"
    except Exception as exc:
        info["error"] = str(exc)
    return info
