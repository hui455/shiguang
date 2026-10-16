# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.10,<2",
#   "openai>=1.60",
#   "pillow>=10",
#   "uvicorn>=0.32",
# ]
# ///
"""OpenAI image generation MCP server.

Two modes, chosen by MCP_TRANSPORT:

  stdio (default)  local plugin for Claude Code / Claude Desktop
  http             remote server for claude.ai custom connectors

Image settings (both modes):
  OPENAI_API_KEY         required
  OPENAI_BASE_URL        optional, for proxies / OpenAI-compatible endpoints
  IMAGE_MODEL            default model (default: gpt-image-2.5-flare)
  IMAGE_OUTPUT_DIR       where images are saved (default: ./generated-images)
  IMAGE_DEFAULT_SIZE     default size (default: auto)
  IMAGE_DEFAULT_QUALITY  default quality (default: auto)
  IMAGE_PREVIEW_MAX_PX   longest side of previews returned to Claude (default: 512, max 2048)

HTTP mode only:
  MCP_SECRET             required, long random string for the MCP endpoint only
  PUBLIC_BASE_URL        e.g. https://img.example.com, used to build download links
  HOST / PORT            listen address (default: 0.0.0.0:8000)
"""

from __future__ import annotations

import base64
import json
import tempfile
import hashlib
import hmac
import secrets
import io
import ipaddress
import functools
import shutil
import threading
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP, Image
from openai import OpenAI

TRANSPORT = (os.environ.get("MCP_TRANSPORT") or "stdio").lower()
# Known/recommended models. Any other model name the endpoint (OpenAI or a relay) accepts
# also works; it is passed through as-is with gpt-image style parameters.
SUPPORTED_MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst")
KNOWN_QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = os.environ.get("IMAGE_MODEL") or SUPPORTED_MODELS[0]
OUTPUT_DIR = Path(os.environ.get("IMAGE_OUTPUT_DIR") or "generated-images")
DEFAULT_SIZE = os.environ.get("IMAGE_DEFAULT_SIZE") or "auto"
DEFAULT_QUALITY = os.environ.get("IMAGE_DEFAULT_QUALITY") or "auto"
SECRET = os.environ.get("MCP_SECRET") or ""
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
PREVIEW_MAX_PX = max(128, min(int(os.environ.get("IMAGE_PREVIEW_MAX_PX") or 512), 2048))
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
IMAGE_STORAGE_MAX_BYTES = int(os.environ.get("IMAGE_STORAGE_MAX_BYTES", 1024**3))
IMAGE_MIN_FREE_BYTES = int(os.environ.get("IMAGE_MIN_FREE_BYTES", 2 * 1024**3))
if IMAGE_STORAGE_MAX_BYTES <= 0 or IMAGE_MIN_FREE_BYTES < 0:
    raise ValueError("Invalid image storage limits")
_STORAGE_LOCK = threading.RLock()
# Download-token records have their own lock so links can be made (gallery, list_images)
# while a long generation holds _STORAGE_LOCK.
_TOKEN_LOCK = threading.Lock()
_CONFIG_LOCK = threading.RLock()


def _validate_defaults(config):
    model = config.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}", model):
        raise ValueError(
            "model must be a model name such as gpt-image-2.5-flare (letters, digits and . _ : / @ + -)."
        )
    quality = config.get("quality")
    if not isinstance(quality, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", quality):
        raise ValueError(
            "quality must be a quality name such as " + ", ".join(KNOWN_QUALITIES)
        )
    size = config.get("size")
    if not isinstance(size, str) or (size != "auto" and not re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", size)):
        raise ValueError("size must be auto or WIDTHxHEIGHT (supported dimensions depend on the API)")
    return config


def _defaults():
    with _CONFIG_LOCK:
        config = {"model": DEFAULT_MODEL, "size": DEFAULT_SIZE, "quality": DEFAULT_QUALITY}
        path = OUTPUT_DIR / ".image-config.json"
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict) or set(stored) - set(config):
                raise ValueError("Invalid saved image configuration")
            config.update(stored)
        return _validate_defaults(config)


def _request_defaults(model, size, quality):
    config = _defaults()
    config.update({k: v for k, v in {"model": model, "size": size, "quality": quality}.items() if v is not None})
    return _validate_defaults(config)


def _check_storage(required: int = 1):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    used = sum(p.stat().st_size for p in OUTPUT_DIR.rglob("*") if p.is_file())
    if used + required > IMAGE_STORAGE_MAX_BYTES:
        raise RuntimeError("Image storage limit reached. Download and manually remove old images before retrying.")
    if shutil.disk_usage(OUTPUT_DIR).free - required < IMAGE_MIN_FREE_BYTES:
        raise RuntimeError("Disk space is low. Image generation is paused to protect the server; free disk space before retrying.")


def _storage_guard(func):
    @functools.wraps(func)
    def guarded(*args, **kwargs):
        # One generation/edit at a time in this single-process server.
        if not _STORAGE_LOCK.acquire(blocking=False):
            raise RuntimeError("Another image request is running. Please retry when it finishes.")
        try:
            _check_storage()
            return func(*args, **kwargs)
        finally:
            _STORAGE_LOCK.release()
    return guarded


def _in_worker_thread(func):
    """Expose a blocking tool to MCP as async, so a 30-90 s image call does not freeze
    the event loop (admin page, downloads and health checks keep responding)."""
    import anyio

    @functools.wraps(func)
    async def threaded(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(func, *args, **kwargs))
    return threaded

if TRANSPORT == "http":
    if len(SECRET) < 24 or not re.fullmatch(r"[A-Za-z0-9_\-]+", SECRET):
        sys.exit(
            "MCP_SECRET must be at least 24 characters of letters, digits, '-' or '_'. "
            "Generate one with: openssl rand -hex 24"
        )
    mcp = FastMCP(
        "openai-image-gen",
        host=os.environ.get("HOST") or "0.0.0.0",
        port=int(os.environ.get("PORT") or 8000),
        streamable_http_path=f"/{SECRET}/mcp",
        stateless_http=True,
    )
else:
    mcp = FastMCP("openai-image-gen")


def _client() -> OpenAI:
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set on the server.")
    return OpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None)


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w\-]+", "-", text.strip().lower(), flags=re.UNICODE).strip("-")
    return (s[:limit] or "image").strip("-")


def _link(path: Path) -> str | None:
    """Full-resolution download link (remote mode only)."""
    token = _token(path)
    return f"{PUBLIC_BASE_URL}/files/{token}/{urllib.parse.quote(path.name)}" if token else None


def _thumb_link(path: Path) -> str | None:
    """Small preview link for chat replies, gated by the same per-image token."""
    token = _token(path)
    return f"{PUBLIC_BASE_URL}/thumbs/{token}/{urllib.parse.quote(path.name)}" if token else None


def _token(path: Path) -> str | None:
    if TRANSPORT != "http" or not PUBLIC_BASE_URL:
        return None
    path = _download_path(path.name)
    with _TOKEN_LOCK:
        directory = OUTPUT_DIR / ".download-tokens"
        directory.mkdir(mode=0o700, exist_ok=True)
        record = directory / hashlib.sha256(path.name.encode()).hexdigest()
        if record.exists():
            token = record.read_text(encoding="ascii")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                raise RuntimeError("Invalid download token record")
        else:
            token = secrets.token_urlsafe(32)
            with record.open("x", encoding="ascii") as stream:
                record.chmod(0o600)
                stream.write(token)
    return token


def _download_path(name: str) -> Path:
    # Only actual image files directly inside the image directory are downloadable.
    path = OUTPUT_DIR / name
    if (not name or "/" in name or "\\" in name or name.startswith(".")
            or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}
            or path.is_symlink() or not path.is_file()
            or path.resolve().parent != OUTPUT_DIR.resolve()):
        raise FileNotFoundError("Image not found")
    return path


def _authorized_path(request) -> Path | None:
    name = request.path_params["name"]
    token = request.path_params["token"]
    try:
        path = _download_path(name)
        record = OUTPUT_DIR / ".download-tokens" / hashlib.sha256(name.encode()).hexdigest()
        expected = record.read_text(encoding="ascii")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token) or not hmac.compare_digest(token, expected):
            return None
        return path
    except (OSError, ValueError):
        return None


def _download(request):
    from starlette.responses import FileResponse, Response

    path = _authorized_path(request)
    if path is None:
        return Response(status_code=404)
    # inline: the link can be shown as an image in chat (Markdown) or opened in a browser tab;
    # the admin page's download buttons still save it via the <a download> attribute.
    # Cached privately so a chat that shows the image does not re-download it on every scroll.
    return FileResponse(path, filename=path.name, content_disposition_type="inline", headers={
        "Cache-Control": "private, max-age=86400", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


def _thumbnail(request):
    # Sync endpoint: Starlette runs it in a worker thread, so resizing never blocks the loop.
    from starlette.responses import Response

    path = _authorized_path(request)
    if path is None:
        return Response(status_code=404)
    try:
        data, fmt = _thumbnail_bytes(path)
    except OSError:
        return Response(status_code=404)
    return Response(data, media_type=f"image/{fmt}", headers={
        "Cache-Control": "private, max-age=86400", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })


def _save(data, prompt: str, filename: str | None, fmt: str) -> list[Path]:
    with _STORAGE_LOCK:
        # Check the whole batch before writing; recheck actual bytes before each file.
        encoded = []
        for item in data:
            b64 = getattr(item, "b64_json", None)
            if not b64:
                raise RuntimeError("The API returned no image data (b64_json missing).")
            encoded.append("".join(b64.split()))
        required = sum((len(b) // 4) * 3 - (len(b) - len(b.rstrip("="))) for b in encoded)
        _check_storage(max(1, required))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = _slug(filename) if filename else f"{stamp}-{_slug(prompt)}"
        paths: list[Path] = []
        try:
            for i, b64 in enumerate(encoded):
                content = base64.b64decode(b64, validate=True)
                _check_storage(len(content))
                suffix = f"-{i + 1}" if len(encoded) > 1 else ""
                path = OUTPUT_DIR / f"{base}{suffix}.{fmt}"
                n = 2
                while path.exists():
                    path = OUTPUT_DIR / f"{base}{suffix}-v{n}.{fmt}"
                    n += 1
                with path.open("xb") as f:
                    paths.append(path.resolve())
                    f.write(content)
            return paths
        except Exception:
            # Only remove new files from this failed batch, never existing images.
            for path in paths:
                path.unlink(missing_ok=True)
            raise


# ---------- per-image records: prompt, parameters, references ----------
# One JSON file per image in .image-meta/, keyed like download tokens (sha256 of the name).
# Hidden directory, so it is never listed, downloaded or shown as an image.
META_DIR = ".image-meta"
MAX_PROMPT_CHARS = 32000


def _meta_path(name: str) -> Path:
    return OUTPUT_DIR / META_DIR / (hashlib.sha256(name.encode()).hexdigest() + ".json")


def _write_meta(name: str, record: dict) -> None:
    directory = OUTPUT_DIR / META_DIR
    directory.mkdir(mode=0o700, exist_ok=True)
    target = _meta_path(name)
    temporary = target.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump({"name": name, **record}, stream, ensure_ascii=False)
    os.replace(temporary, target)


def read_meta(name: str) -> dict | None:
    try:
        record = json.loads(_meta_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("name") == name else None


def delete_meta(name: str) -> None:
    _meta_path(name).unlink(missing_ok=True)


def all_meta() -> dict[str, dict]:
    """name -> record for every stored record (records of deleted images are skipped)."""
    out: dict[str, dict] = {}
    directory = OUTPUT_DIR / META_DIR
    if not directory.is_dir():
        return out
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and isinstance(record.get("name"), str):
            out[record["name"]] = record
    return out


def _record_outputs(paths: list[Path], base: dict, revised: list | None = None) -> None:
    # Best effort: a failed record never fails a generation that already cost money.
    for i, path in enumerate(paths):
        record = dict(base, created=time.time())
        if revised and i < len(revised) and revised[i]:
            record["revised_prompt"] = revised[i]
        try:
            _write_meta(path.name, record)
        except (OSError, TypeError, ValueError) as error:
            print(f"warning: could not save the record for {path.name}: {error}", file=sys.stderr)


def _save_reference_copy(data: bytes, stem: str, label: str) -> str | None:
    """Keep a copy of a URL / local-path reference next to the image made from it."""
    from PIL import Image as PILImage

    try:
        with PILImage.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            if fmt not in ("PNG", "JPEG", "WEBP"):
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                data, fmt = buf.getvalue(), "PNG"
    except Exception:
        return None
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}[fmt]
    try:
        _check_storage(len(data))
    except RuntimeError:
        return None
    path = OUTPUT_DIR / f"{stem}-{label}.{ext}"
    n = 2
    while path.exists():
        path = OUTPUT_DIR / f"{stem}-{label}-v{n}.{ext}"
        n += 1
    try:
        with path.open("xb") as f:
            f.write(data)
    except OSError:
        path.unlink(missing_ok=True)
        return None
    return path.name


def _describe_meta(record: dict, full: bool) -> list[str]:
    lines = []
    prompt = record.get("prompt") or ""
    if not full and len(prompt) > 160:
        prompt = prompt[:160] + "…"
    lines.append(f"prompt: {prompt}")
    if full and record.get("revised_prompt"):
        lines.append(f"revised_prompt: {record['revised_prompt']}")
    params = [f"{k}={record[k]}" for k in ("model", "size", "quality", "background") if record.get(k)]
    if params:
        lines.append("params: " + ", ".join(params))
    refs = [r.get("name") or r.get("url") or r.get("path") or "?" for r in record.get("references") or []]
    if refs:
        lines.append("references: " + ", ".join(refs))
    if record.get("mask"):
        m = record["mask"]
        lines.append("mask: " + (m.get("name") or m.get("url") or m.get("path") or "?"))
    return lines


def _thumbnail_bytes(path: Path) -> tuple[bytes, str]:
    """Longest side PREVIEW_MAX_PX; PNG when the image has transparency, else JPEG."""
    from PIL import Image as PILImage

    with PILImage.open(path) as im:
        im.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX))
        buf = io.BytesIO()
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            im.convert("RGBA").save(buf, format="PNG", optimize=True)
            return buf.getvalue(), "png"
        im.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "jpeg"


def _preview(path: Path) -> Image:
    """Small thumbnail so previews stay cheap; falls back to the original."""
    try:
        data, fmt = _thumbnail_bytes(path)
        return Image(data=data, format=fmt)
    except Exception:
        return Image(path=str(path))


def _result(paths: list[Path], model: str, revised: list, preview: bool, copies: list[str] | None = None):
    lines = [f"Generated {len(paths)} image(s) with {model}:"]
    for p in paths:
        link = _link(p)
        lines.append(f"- {p.name}" + (f"  download: {link}" if link else f"  saved: {p}"))
    lines += [f"Revised prompt: {r}" for r in revised if r]
    if copies:
        lines.append("Reference images were copied into the gallery (not generated images; "
                     "leave them out of the ps block): " + ", ".join(copies))
    lines.append("The prompt, parameters and references are saved with each image (see image_info).")
    if TRANSPORT == "http":
        lines.append("To edit one of these, pass its file name to edit_image.")
    shown = [p for p in paths if _link(p)]
    if shown:
        lines.append(
            "Reply format required by the user: the preview below is a low-res thumbnail for "
            "your own review only. End your reply with a block that starts with 'ps:'. For each "
            "image, write exactly one line containing only the full-resolution image as a plain "
            "Markdown image: ![<file name>](<download URL>), using the download URL above. Do not "
            "wrap it in a link (the Claude app does not render linked images) and add nothing else "
            "to the block: no bare URLs, no labels, no commentary. Include only "
            "images generated by generate_image/edit_image calls made for this reply; never "
            "include images from earlier replies or from list_images. Exception: when an active "
            "skill or the user asks for the images inline in the text instead (e.g. roleplay "
            "mode), put each image as ![<file name>](<download URL>) inline and skip the ps block."
        )
    out: list = ["\n".join(lines)]
    if preview:
        out += [_preview(p) for p in paths]
    return out


def _is_public_host(host: str) -> bool:
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


def _in_memory(data: bytes, name: str, source: dict):
    f = io.BytesIO(data)
    f.name = name
    f.source = source  # what the image record stores about this reference
    return f


def _own_download_name(ref: str) -> str | None:
    """A download link of this server -> the gallery file name it points to (token checked)."""
    if not PUBLIC_BASE_URL or not ref.startswith(PUBLIC_BASE_URL + "/files/"):
        return None
    parts = ref[len(PUBLIC_BASE_URL) + 1:].split("?")[0].split("/")
    if len(parts) != 3:
        return None
    token, name = parts[1], urllib.parse.unquote(parts[2])
    try:
        _download_path(name)
        expected = (OUTPUT_DIR / ".download-tokens" / hashlib.sha256(name.encode()).hexdigest()).read_text(encoding="ascii")
    except (OSError, ValueError):
        return None
    return name if hmac.compare_digest(token, expected) else None


def _open_input(ref: str):
    """Accept a file name from earlier generations, an http(s) URL, or (stdio) a local path."""
    own = _own_download_name(ref) if ref.startswith(("http://", "https://")) else None
    if own:
        ref = own
    elif ref.startswith(("http://", "https://")):
        host = urllib.parse.urlparse(ref).hostname or ""
        if not _is_public_host(host):
            raise ValueError(f"Refusing to fetch non-public address: {ref}")
        with urllib.request.urlopen(ref, timeout=30) as r:
            data = r.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError("Reference image is larger than 20 MB.")
        name = Path(urllib.parse.urlparse(ref).path).name or "reference.png"
        return _in_memory(data, name, {"source": "url", "url": ref})

    candidate = (OUTPUT_DIR / Path(ref).name).resolve()
    if candidate.is_file() and candidate.parent == OUTPUT_DIR.resolve() and not candidate.name.startswith("."):
        return _in_memory(candidate.read_bytes(), candidate.name, {"source": "gallery", "name": candidate.name})
    if TRANSPORT != "http":
        path = Path(ref).expanduser()
        if path.is_file():
            return _in_memory(path.read_bytes(), path.name, {"source": "local", "path": str(path.resolve())})
    raise FileNotFoundError(f"Image not found: {ref}")


def _reference_records(files: list, given: list[str], stem: str, label: str = "ref") -> tuple[list[dict], list[str]]:
    """Describe each reference for the image record; URL / local ones get a gallery copy."""
    records, copies = [], []
    for i, (f, ref) in enumerate(zip(files, given), start=1):
        source = getattr(f, "source", None)
        if not isinstance(source, dict):
            records.append({"source": "unknown", "given": str(ref)[:500]})
            continue
        record = dict(source)
        if source.get("source") in ("url", "local"):
            f.seek(0)
            copy = _save_reference_copy(f.read(), stem, f"{label}{i}" if len(files) > 1 or label == "ref" else label)
            if copy:
                record["name"] = copy
                copies.append(copy)
                _record_outputs([OUTPUT_DIR / copy], {"kind": "reference", **{k: v for k, v in source.items() if k != "source"}})
        records.append(record)
    return records, copies


@_storage_guard
def generate_image(
    prompt: str,
    size: str | None = None,
    quality: str | None = None,
    n: int = 1,
    background: Literal["auto", "transparent", "opaque"] | None = None,
    output_format: Literal["png", "jpeg", "webp"] = "png",
    filename: str | None = None,
    model: str | None = None,
    preview: bool = True,
):
    """Generate images from a text prompt with an OpenAI image model and save them.

    Use for UI assets: illustrations, hero art, character portraits, card art, icons,
    backgrounds, or full-screen style references.

    Args:
        prompt: Detailed description of the image.
        size: e.g. "1024x1024", "1536x1024" (landscape), "1024x1536" (portrait), "auto".
        quality: GPT-Image-2.5: "auto" | "low" | "medium" | "high" | "xhigh" | "max".
            Other models: whatever their API accepts.
        n: Number of images (1-4 recommended).
        background: "transparent" for icons/stickers/sprites (png or webp only).
        output_format: png, jpeg or webp.
        filename: Optional base name, e.g. "hero-guanyu".
        model: Override the default model for this call. Default gpt-image-2.5-flare;
            gpt-image-2.5-sunburst is slower and more precise. Any other model name the
            configured endpoint supports (e.g. a relay's model) is passed through as-is.
        preview: Also return a small preview so Claude can look at it.
    """
    config = _request_defaults(model, size, quality)
    model = config["model"]
    params: dict = {**config, "prompt": prompt, "n": n, "output_format": output_format}
    if background:
        params["background"] = background
    fmt = output_format

    resp = _client().images.generate(**params)
    paths = _save(resp.data, prompt, filename, fmt)
    revised = [getattr(d, "revised_prompt", None) for d in resp.data]
    revised = [r if isinstance(r, str) else None for r in revised]
    _record_outputs(paths, {
        "kind": "generate", "prompt": prompt[:MAX_PROMPT_CHARS], "model": model,
        "size": config["size"], "quality": config["quality"], "background": background,
        "output_format": output_format, "n": n,
    }, revised)
    return _result(paths, model, revised, preview)


@_storage_guard
def edit_image(
    prompt: str,
    images: list[str],
    mask: str | None = None,
    size: str | None = None,
    quality: str | None = None,
    n: int = 1,
    background: Literal["auto", "transparent", "opaque"] | None = None,
    output_format: Literal["png", "jpeg", "webp"] = "png",
    filename: str | None = None,
    model: str | None = None,
    preview: bool = True,
):
    """Edit or combine existing images with a prompt (style transfer, change background,
    keep a character consistent across poses, merge references into one image).

    Args:
        prompt: What to change or create from the reference image(s).
        images: Reference images. Each is a file name returned by an earlier
            generate_image/edit_image call, an http(s) image URL, or (local mode) a file path.
        mask: Optional PNG mask (same forms as images); transparent areas get edited.
        model: Default gpt-image-2.5-flare; gpt-image-2.5-sunburst for more precision; any
            other model the endpoint supports is passed through.
        size, quality, n, background, output_format, filename, preview: as in generate_image.
    """
    config = _request_defaults(model, size, quality)
    model = config["model"]
    files = [_open_input(ref) for ref in images]
    mask_file = _open_input(mask) if mask else None
    try:
        params: dict = {
            "model": model,
            "prompt": prompt,
            "image": files if len(files) > 1 else files[0],
            "n": n,
        }
        if mask_file:
            params["mask"] = mask_file
        params.update(size=config["size"], quality=config["quality"], output_format=output_format)
        if background:
            params["background"] = background
        fmt = output_format
        resp = _client().images.edit(**params)
        paths = _save(resp.data, prompt, filename, fmt)
        # Record what the image was made from. URL / local-path references are copied into
        # the gallery (they may disappear later); gallery references are recorded by name.
        stem = Path(paths[0].name).stem if paths else "edit"
        references, copies = _reference_records(files, images, stem)
        mask_record = None
        if mask_file:
            masks, mask_copies = _reference_records([mask_file], [mask], stem, "mask")
            mask_record, copies = masks[0], copies + mask_copies
        _record_outputs(paths, {
            "kind": "edit", "prompt": prompt[:MAX_PROMPT_CHARS], "model": model,
            "size": config["size"], "quality": config["quality"], "background": background,
            "output_format": output_format, "n": n, "references": references, "mask": mask_record,
        })
    finally:
        for f in files:
            f.close()
        if mask_file:
            mask_file.close()

    return _result(paths, model, [], preview, copies)


# The module-level functions stay synchronous (tests and local callers use them directly);
# MCP gets async wrappers that run them in a worker thread.
mcp.tool(name="generate_image")(_in_worker_thread(generate_image))
mcp.tool(name="edit_image")(_in_worker_thread(edit_image))


@mcp.tool()
def list_images(limit: int = 20) -> str:
    """List the most recently generated images (newest first), with download links in remote mode."""
    if not OUTPUT_DIR.exists():
        return "No images yet."
    files = sorted(
        (p for p in OUTPUT_DIR.iterdir() if p.is_file() and not p.is_symlink()
         and not p.name.startswith(".") and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    if not files:
        return "No images yet."
    lines = []
    for p in files:
        lines.append(f"- {p.name}" + (f"  {_link(p)}" if _link(p) else f"  {p.resolve()}"))
        record = read_meta(p.name)
        if record and record.get("kind") != "reference":
            lines += ["    " + line for line in _describe_meta(record, full=False)]
        elif record:
            lines.append("    (reference copy saved by edit_image)")
    return "\n".join(lines)


@mcp.tool()
def image_info(name: str) -> str:
    """Show how an image was made: full prompt, model, size, quality, reference images and
    mask. Use it to recreate or vary an earlier image ("draw that one again"). Images made
    before records were added have no record."""
    try:
        path = _download_path(Path(name).name)
    except FileNotFoundError:
        return f"Image not found: {name}"
    record = read_meta(path.name)
    if not record:
        return f"{path.name}: no record (made before prompts were saved, or added manually)."
    if record.get("kind") == "reference":
        origin = record.get("url") or record.get("path") or "unknown source"
        return f"{path.name}: reference copy saved by edit_image, from {origin}"
    lines = [f"{path.name} ({record.get('kind', 'generate')}, "
             f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(record.get('created', 0)))})"]
    lines += _describe_meta(record, full=True)
    for ref in record.get("references") or []:
        if ref.get("name") and not (OUTPUT_DIR / ref["name"]).is_file():
            lines.append(f"note: reference {ref['name']} has been deleted")
    return "\n".join(lines)


@mcp.tool()
def image_config() -> str:
    """Show the current image generation settings (model, endpoint, output folder)."""
    config = _defaults()
    lines = [
        f"mode: {TRANSPORT}",
        f"model: {config['model']}",
        f"known_models: {', '.join(SUPPORTED_MODELS)} (any other model your endpoint supports also works)",
        f"base_url: {os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1 (default)'}",
        f"api_key: {'set' if os.environ.get('OPENAI_API_KEY') else 'NOT SET'}",
        f"default_size: {config['size']}",
        f"default_quality: {config['quality']}",
        "change_defaults: use set_image_config (persistent, shared by all clients)",
        f"image_storage_max_bytes: {IMAGE_STORAGE_MAX_BYTES}",
        f"image_min_free_bytes: {IMAGE_MIN_FREE_BYTES}",
    ]
    if TRANSPORT != "http":
        lines.append(f"output_dir: {OUTPUT_DIR.resolve()}")
    else:
        lines.append(f"download links: {'on' if PUBLIC_BASE_URL else 'off (PUBLIC_BASE_URL not set)'}")
    return "\n".join(lines)


@mcp.tool()
def set_image_config(
    model: str | None = None,
    size: str | None = None,
    quality: str | None = None,
) -> str:
    """Change persistent defaults for future generate_image and edit_image calls.

    Only use when the user asks to change defaults. gpt-image-2.5-flare is the initial
    default and gpt-image-2.5-sunburst the slower, more precise option; any other model
    name the configured endpoint supports is also accepted. Quality for GPT-Image-2.5:
    auto, low, medium, high, xhigh, max. Omitted fields keep their current values.
    Changes are shared by all MCP clients and survive server restarts. Per-call
    model/size/quality overrides do not change defaults. No image is generated and
    API keys, storage quotas and authentication cannot be changed with this tool.
    """
    if not _STORAGE_LOCK.acquire(blocking=False):
        raise RuntimeError("An image request is running. Change defaults after it finishes.")
    try:
        with _CONFIG_LOCK:
            config = _request_defaults(model, size, quality)
            if any(v is not None for v in (model, size, quality)):
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=OUTPUT_DIR,
                                                     prefix=".image-config-", delete=False) as stream:
                        temporary = Path(stream.name)
                        json.dump(config, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, OUTPUT_DIR / ".image-config.json")
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            return image_config()
    finally:
        _STORAGE_LOCK.release()


def _http_app():
    from admin import Admin
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app = mcp.streamable_http_app()
    app.router.routes.append(
        Route("/files/{token}/{name}", _download, methods=["GET", "HEAD"], name="files")
    )
    app.router.routes.append(
        Route("/thumbs/{token}/{name}", _thumbnail, methods=["GET", "HEAD"], name="thumbs")
    )
    app.router.routes.append(Route("/healthz", lambda r: PlainTextResponse("ok")))
    app.router.routes.extend(Admin(OUTPUT_DIR, _STORAGE_LOCK, IMAGE_STORAGE_MAX_BYTES,
                                  IMAGE_MIN_FREE_BYTES, PUBLIC_BASE_URL, _link, _download_path,
                                  all_meta, delete_meta).routes())
    return app


if __name__ == "__main__":
    if TRANSPORT == "http":
        import uvicorn

        uvicorn.run(
            _http_app(),
            host=mcp.settings.host,
            port=mcp.settings.port,
            proxy_headers=True,
            # Only trust X-Forwarded-For from the reverse proxy (Caddy on the compose
            # network, or Nginx on the host via Docker's bridge), never from the internet,
            # so login rate limits see the real client IP.
            forwarded_allow_ips=os.environ.get("TRUSTED_PROXY_IPS")
            or "127.0.0.1,::1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16",
            access_log=False,  # Request URLs contain the MCP secret.
        )
    else:
        mcp.run()
