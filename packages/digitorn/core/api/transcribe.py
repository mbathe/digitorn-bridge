"""POST /api/transcribe - voice → text for the chat mic button."""

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


# narrow MIME allowlist; adding a codec is a conscious decision.
_ALLOWED_MIME = frozenset({
    "audio/webm", "audio/ogg", "audio/opus",
    "audio/mp3", "audio/mpeg",
    "audio/m4a", "audio/mp4", "audio/x-m4a",
    "audio/wav", "audio/x-wav", "audio/wave",
    "audio/flac", "audio/x-flac",
    "audio/aac",
    # Some HTML5 stacks report octet-stream for .webm; the extension check below gates it.
    "application/octet-stream",
})
_ALLOWED_EXT = frozenset({
    ".webm", ".ogg", ".opus", ".mp3", ".m4a", ".mp4",
    ".wav", ".flac", ".aac",
})

# magic-byte guard against decompression bombs; bombs typically rely on a spoofed header.
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
    if not head:
        return False
    # `ftyp` appears at offset 4 in MP4/M4A containers.
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return True
    for magic in _AUDIO_MAGIC:
        if head.startswith(magic):
            return True
    return False


# per-user + per-IP sliding-window rate limit; amortized sweep prevents the bucket dict from growing forever.
_RATE_LIMIT_RPM = 30
_rate_window: dict[str, list[float]] = {}
_rate_lock = asyncio.Lock()
_RATE_SWEEP_EVERY = 200
_rate_check_count = 0


async def _check_rate_limit(user_id: str, ip: str) -> None:
    """Raise 429 if either the user or the IP exceeded `_RATE_LIMIT_RPM`"""
    now = time.monotonic()
    window_start = now - 60.0
    global _rate_check_count
    async with _rate_lock:
        _rate_check_count += 1
        if _rate_check_count >= _RATE_SWEEP_EVERY:
            _rate_check_count = 0
            stale_keys = [
                k for k, bucket in _rate_window.items()
                if not any(t >= window_start for t in bucket)
            ]
            for k in stale_keys:
                _rate_window.pop(k, None)

        for key in (f"u:{user_id}", f"ip:{ip}"):
            bucket = _rate_window.get(key, [])
            pruned = [t for t in bucket if t >= window_start]
            if len(pruned) >= _RATE_LIMIT_RPM:
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
            _rate_window[key] = pruned
        _rate_window[f"u:{user_id}"].append(now)
        _rate_window[f"ip:{ip}"].append(now)


class TranscribeResponse(BaseModel):
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


_model_lock = asyncio.Lock()
_model: Any = None
_model_error: Exception | None = None
# Semaphore guards concurrent inference on the single shared model.
# Sized from `transcribe.max_concurrency` at first use.
_inference_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _inference_semaphore
    if _inference_semaphore is None:
        cfg = get_settings().transcribe
        _inference_semaphore = asyncio.Semaphore(max(1, cfg.max_concurrency))
    return _inference_semaphore


async def preload_model() -> dict[str, Any]:
    """Eagerly load the Whisper model at daemon startup."""
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
    """Run faster-whisper inference on a file, return normalised result."""
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

        if not text:
            segments, info = model.transcribe(
                audio_path,
                language=language,
                beam_size=1,
                vad_filter=False,
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
    """Fetch the OpenAI API key from the credentials store."""
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


async def _transcribe_gateway(
    audio_path: str,
    language: str | None,
    timeout_s: float,
    *,
    user_jwt: str,
    alias: str,
    app_id: str | None = None,
) -> dict[str, Any]:
    """Forward the audio to digitorn-gateway's /v1/audio/transcriptions."""
    try:
        import httpx  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "httpx package not installed. `pip install httpx`."
        ) from exc

    from digitorn.core.config import get_settings
    gw_base = get_settings().runtime.gateway_base_url.rstrip("/")
    # gateway_base_url ends with /v1; the audio endpoint sits at /v1/audio/transcriptions.
    if gw_base.endswith("/v1"):
        url = f"{gw_base}/audio/transcriptions"
    else:
        url = f"{gw_base}/v1/audio/transcriptions"

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    form = {
        "model": alias,
        "response_format": "verbose_json",
    }
    if language:
        # Whisper rejects BCP-47 region suffix ("fr-FR"); strip to ISO-639-1
        form["language"] = language.split("-")[0].lower()

    headers: dict[str, str] = {"Authorization": f"Bearer {user_jwt}"}
    if app_id:
        headers["X-Digitorn-App-Id"] = app_id

    async with httpx.AsyncClient(timeout=timeout_s) as http:
        files = {"file": (os.path.basename(audio_path), audio_bytes, "application/octet-stream")}
        resp = await http.post(
            url,
            data=form,
            files=files,
            headers=headers,
        )
        if resp.status_code >= 400:
            # Surface the gateway's structured error verbatim. Caller maps
            # to HTTP for the client.
            try:
                detail = resp.json()
            except Exception:
                detail = {"detail": resp.text[:500]}
            raise RuntimeError(
                f"gateway_transcription_failed status={resp.status_code} {detail}"
            )
        body = resp.json()

    text = (body.get("text") or "").strip()
    return {
        "text": text,
        "language": body.get("language") or language,
        "duration_ms": (
            int(float(body["duration"]) * 1000)
            if body.get("duration") is not None
            else None
        ),
        "confidence": None,
    }


def _normalize_language(raw: str | None) -> str | None:
    """Coerce a BCP-47 / browser locale tag into the ISO-639-1 form"""
    if not raw:
        return None
    primary = raw.split("-", 1)[0].split("_", 1)[0].strip().lower()
    # Whisper accepts 2-letter codes; longer or empty → drop the hint
    # and let the model auto-detect rather than fail the whole call.
    if not primary or len(primary) > 3:
        return None
    return primary


@router.post("", response_model=TranscribeResponse)
@router.post("/", response_model=TranscribeResponse)
async def transcribe(
    request: Request,
    audio: UploadFile = File(..., description="Audio file (.m4a / .webm / .wav)."),
    # cap form fields so large strings can't be used as DoS memory amplifiers.
    language: str | None = Form(default=None, max_length=64, description="BCP-47 hint (fr, en-US). Auto-detect if omitted."),
    app_id: str | None = Form(default=None, max_length=64, description="Current app_id (for future vocab biasing)."),
) -> TranscribeResponse:
    """Transcribe an uploaded audio blob to text."""
    cfg = get_settings().transcribe
    if not cfg.enabled:
        raise HTTPException(status_code=404, detail="transcription disabled")

    # normalise BCP-47 -> ISO-639-1 once so every provider branch sees a clean 2-letter tag.
    language = _normalize_language(language)

    # defence-in-depth refusal of anonymous callers even if a future reverse proxy weakens auth.
    user_id = getattr(request.state, "user_id", None)
    if not user_id or user_id == "anonymous":
        raise HTTPException(status_code=401, detail="Authentication required")

    client_host = getattr(getattr(request, "client", None), "host", "") or "0.0.0.0"
    await _check_rate_limit(user_id, client_host)

    raw_filename = (audio.filename or "").strip()
    if not raw_filename:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "missing_filename",
                "message": "The uploaded file has no name. Set a filename like 'recording.webm'.",
            },
        )

    # reject path traversal / null bytes at the gate before they reach the tempfile path.
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

    # MIME may be missing or lying; verify the file extension as a second gate.
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

    # magic-byte guard so ffmpeg never decodes a decompression bomb.
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

    filename = raw_filename
    suffix = ext or ".m4a"

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(content)
        tmp.flush()
        tmp.close()
        started = time.monotonic()
        try:
            from digitorn.core.config import get_settings as _gs
            _gw_enabled = getattr(_gs().runtime, "gateway_enabled", True)
            if cfg.provider in ("gateway", "openai") and _gw_enabled:
                if cfg.provider == "openai":
                    logger.info(
                        "transcribe_openai_routed_via_gateway user=%s "
                        "(operator opted in 'openai'; the gateway handles "
                        "the OpenAI credential transparently). To bypass "
                        "the gateway set runtime.gateway_enabled=false.",
                        user_id,
                    )
                auth_hdr = request.headers.get("authorization") or ""
                user_jwt = auth_hdr.removeprefix("Bearer ").removeprefix("bearer ").strip()
                if not user_jwt:
                    raise HTTPException(
                        status_code=401,
                        detail="missing bearer token for gateway forwarding",
                    )
                result = await _transcribe_gateway(
                    tmp.name, language, cfg.timeout_seconds,
                    user_jwt=user_jwt, alias=cfg.gateway_model,
                    app_id=app_id,
                )
            elif cfg.provider == "openai":
                # direct-OpenAI fallback when the operator disabled the gateway (air-gapped / local-only deploys).
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
            # classify the failure with a stable category + curated message; never echo raw exception text (may leak secrets).
            name = type(exc).__name__
            text = str(exc).lower()

            if "gateway_transcription_failed" in text:
                import re as _re
                m = _re.search(r"status=(\d{3})", str(exc))
                upstream_status = int(m.group(1)) if m else 502
                detail_payload: Any = None
                m2 = _re.search(r"status=\d{3}\s+(\{.*\})\s*$", str(exc), _re.DOTALL)
                if m2:
                    try:
                        import ast as _ast
                        detail_payload = _ast.literal_eval(m2.group(1))
                    except (ValueError, SyntaxError):
                        detail_payload = None
                inner = (
                    detail_payload.get("detail")
                    if isinstance(detail_payload, dict) and "detail" in detail_payload
                    else detail_payload
                )
                code = (
                    inner.get("code") if isinstance(inner, dict) else None
                ) or ""
                if upstream_status == 429 and code == "quota_exceeded":
                    human = (
                        "Monthly quota exceeded on the gateway. "
                        "Wait for the next reset or raise the limit."
                    )
                    classified = "quota_exceeded"
                elif upstream_status in (401, 402, 403):
                    human = (
                        "The gateway rejected the request (auth / billing). "
                        "Check your credential or subscription."
                    )
                    classified = "gateway_auth_failed"
                else:
                    human = (
                        f"The transcription gateway returned an error "
                        f"({upstream_status}). Retry shortly."
                    )
                    classified = "gateway_error"
                raise HTTPException(
                    status_code=upstream_status,
                    detail={
                        "error": classified,
                        "message": human,
                        "upstream": inner,
                    },
                )

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
                truncated = str(exc)[:200]
                human = (
                    f"Transcription failed ({name}): {truncated}"
                    if truncated
                    else f"Transcription failed ({name})."
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
    """Report transcribe subsystem status (probes model availability)."""
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
