"""Small, separately authenticated image manager. No API credentials are exposed."""
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import threading
import time

from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

ASSETS = Path(__file__).with_name("admin_assets")
PAGE_SIZES = (12, 24, 48, 96)
COOKIE = "image_admin"
SESSION_TTL = 8 * 3600          # "remember me" off: browser-session cookie, 8 h on the server
REMEMBER_TTL = 30 * 24 * 3600   # "remember me" on: 30 days
MAX_SESSIONS = 128
SESSION_FILE = ".admin-sessions.json"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


class Admin:
    def __init__(self, output, lock, quota, reserve, public_url, link, image_path,
                 all_meta=None, delete_meta=None):
        self.output, self.lock, self.quota, self.reserve = output, lock, quota, reserve
        self.origin, self.link, self.image_path = public_url.rstrip("/"), link, image_path
        # Image records (prompt, parameters, references) kept by the generator.
        self.all_meta = all_meta or (lambda: {})
        self.delete_meta = delete_meta or (lambda name: None)
        self.password = os.environ.get("IMAGE_ADMIN_PASSWORD", "")
        # sessions: sha256(cookie token) -> (expires_at, csrf). Only hashes are kept, in memory
        # and on disk, so the session file alone cannot be used to log in.
        self.sessions, self.attempts = {}, {}
        self.auth_lock = threading.Lock()
        self._load_sessions()

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).hexdigest() if token else ""

    def _fingerprint(self, salt):
        # Ties stored sessions to the current password: changing IMAGE_ADMIN_PASSWORD logs
        # everyone out. Slow hash, so the file reveals nothing useful about the password.
        return hashlib.pbkdf2_hmac("sha256", self.password.encode(), bytes.fromhex(salt), 200_000).hex()

    def _load_sessions(self):
        path = self.output / SESSION_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            salt = data["salt"]
            if len(self.password) < 16 or not hmac.compare_digest(self._fingerprint(salt), data["fingerprint"]):
                return
            now = time.time()
            self.salt, self.fingerprint = salt, data["fingerprint"]
            self.sessions = {
                k: (float(v[0]), str(v[1])) for k, v in data["sessions"].items()
                if isinstance(v, list) and len(v) == 2 and float(v[0]) > now
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.sessions = {}

    def _save_sessions(self):
        # Called with auth_lock held. Atomic replace; a failed write only costs a re-login.
        if not hasattr(self, "salt"):
            self.salt = secrets.token_hex(16)
            self.fingerprint = self._fingerprint(self.salt)
        payload = {"salt": self.salt, "fingerprint": self.fingerprint,
                   "sessions": {k: list(v) for k, v in self.sessions.items()}}
        path = self.output / SESSION_FILE
        tmp = path.with_name(SESSION_FILE + ".tmp")
        try:
            self.output.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                os.chmod(tmp, 0o600)
                json.dump(payload, f)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def reply(self, data, status=200):
        return JSONResponse(data, status_code=status, headers=HEADERS)

    def session(self, request):
        with self.auth_lock:
            now = time.time()
            live = {k: v for k, v in self.sessions.items() if v[0] > now}
            if len(live) != len(self.sessions):
                self.sessions = live
                self._save_sessions()
            return self.sessions.get(self._key(request.cookies.get(COOKIE, "")))

    async def body(self, request):
        if request.headers.get("origin") != self.origin:
            raise ValueError("请求来源不正确，请刷新页面后重试。")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ValueError("请求格式不正确。")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise ValueError("请求过大。")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("请求格式不正确。")
        return value

    async def login(self, request):
        if len(self.password) < 16:
            return self.reply({"error": "管理页尚未启用，请联系管理员配置登录密码。"}, 503)
        try:
            data = await self.body(request)
        except (ValueError, UnicodeError):
            return self.reply({"error": "请求无效，请刷新页面重试。"}, 400)
        password = data.get("password", "")
        remember = data.get("remember") is True
        if not isinstance(password, str):
            return self.reply({"error": "密码格式不正确。"}, 400)
        ip = request.client.host if request.client else "unknown"
        with self.auth_lock:
            now = time.time()
            self.attempts = {k: v for k, v in self.attempts.items() if v[1] > now}
            count, until = self.attempts.get(ip, (0, now + 900))
            if count >= 5:
                return self.reply({"error": "尝试次数过多，请 15 分钟后再试。"}, 429)
            if ip not in self.attempts and len(self.attempts) >= 1000:
                # Forget the oldest entry instead of locking everyone out.
                del self.attempts[min(self.attempts, key=lambda k: self.attempts[k][1])]
            if not hmac.compare_digest(password.encode(), self.password.encode()):
                self.attempts[ip] = (count + 1, until)
                return self.reply({"error": "密码不正确，请重试。"}, 401)
            self.attempts.pop(ip, None)
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] > now}
            while len(self.sessions) >= MAX_SESSIONS:
                # Drop the session that would expire soonest instead of refusing the login.
                del self.sessions[min(self.sessions, key=lambda k: self.sessions[k][0])]
            ttl = REMEMBER_TTL if remember else SESSION_TTL
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[self._key(token)] = (now + ttl, csrf)
            self._save_sessions()
        response = self.reply({"csrf": csrf, "remember": remember})
        # Without "remember me" the cookie has no max-age, so it ends with the browser session.
        response.set_cookie(COOKIE, token, max_age=REMEMBER_TTL if remember else None, httponly=True,
                            secure=True, samesite="strict", path="/admin")
        return response

    async def api(self, request):
        session = self.session(request)
        if not session:
            return self.reply({"error": "请先登录。"}, 401)
        if request.method == "POST":
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session[1]):
                return self.reply({"error": "登录已失效，请刷新页面重试。"}, 403)
            try:
                data = await self.body(request)
            except (ValueError, UnicodeError):
                return self.reply({"error": "请求无效。"}, 400)
            if request.url.path.endswith("/logout"):
                with self.auth_lock:
                    self.sessions.pop(self._key(request.cookies.get(COOKIE, "")), None)
                    self._save_sessions()
                response = self.reply({"ok": True})
                response.delete_cookie(COOKIE, path="/admin", secure=True, httponly=True, samesite="strict")
                return response
            return await run_in_threadpool(self.delete, data)
        if "/item/" in request.url.path:
            return await run_in_threadpool(self.single, request)
        if "name" in request.path_params:
            return await run_in_threadpool(self.image, request)
        return await run_in_threadpool(self.state, request, session[1])

    def state(self, request, csrf):
        # Read-only, so it never waits for a running generation; deletes still do.
        busy = not self.lock.acquire(blocking=False)
        if not busy:
            self.lock.release()
        files = []
        for p in self.output.iterdir():
            try:
                path = self.image_path(p.name)
                st = path.stat()
            except OSError:
                continue
            files.append({"name": p.name, "size": st.st_size, "modified": st.st_mtime})
        count = len(files)
        used = 0
        for p in self.output.rglob("*"):
            try:
                if p.is_file():
                    used += p.stat().st_size
            except OSError:
                pass
        free = shutil.disk_usage(self.output).free
        meta = self.all_meta()
        used_by = self._used_by(meta)
        query = request.query_params.get("q", "").lower()[:200]

        def matches(f):
            record = meta.get(f["name"]) or {}
            text = " ".join(str(record.get(k) or "") for k in ("prompt", "revised_prompt"))
            return query in f["name"].lower() or query in text.lower()
        files = [f for f in files if matches(f)]
        sort = request.query_params.get("sort", "newest")
        files.sort(key=lambda f: f["size"] if sort == "largest" else f["modified"], reverse=sort != "oldest")
        try:
            page = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            page = 1
        try:
            per_page = int(request.query_params.get("per_page", "24"))
        except ValueError:
            per_page = 24
        if per_page not in PAGE_SIZES:
            per_page = 24
        total = len(files)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        items = files[(page - 1) * per_page:page * per_page]
        available = [x for x in (self._item(item, meta, used_by) for item in items) if x]
        # Counts describe the directory scan; the next refresh picks up deletions.
        return self.reply({"items": available, "total": total, "count": count, "page": page,
                           "pages": pages, "per_page": per_page, "used": used, "quota": self.quota,
                           "free": free, "reserve": self.reserve, "csrf": csrf, "busy": busy,
                           "paused": used >= self.quota or free <= self.reserve})

    @staticmethod
    def _used_by(meta):
        """reference name -> names of the images that were made from it."""
        out = {}
        for name, record in meta.items():
            refs = list(record.get("references") or [])
            if isinstance(record.get("mask"), dict):
                refs.append(record["mask"])
            for ref in refs:
                if isinstance(ref, dict) and isinstance(ref.get("name"), str):
                    out.setdefault(ref["name"], set()).add(name)
        return out

    def _record(self, record):
        if not isinstance(record, dict):
            return None

        def ref(r):
            if not isinstance(r, dict):
                return None
            name = r.get("name") if isinstance(r.get("name"), str) else None
            exists = False
            if name:
                try:
                    self.image_path(name)
                    exists = True
                except OSError:
                    pass
            return {"name": name, "exists": exists, "source": r.get("source"),
                    "url": r.get("url") if isinstance(r.get("url"), str) else None}
        keep = {k: record.get(k) for k in ("kind", "prompt", "revised_prompt", "model", "size",
                                            "quality", "background", "created", "url")}
        keep["references"] = [x for x in map(ref, record.get("references") or []) if x]
        keep["mask"] = ref(record.get("mask")) if record.get("mask") else None
        return keep

    def _item(self, item, meta, used_by):
        from PIL import Image
        try:
            item["url"] = self.link(self.output / item["name"])
        except FileNotFoundError:
            # Another request can delete a file or its token after the scan.
            return None
        item["format"] = Path(item["name"]).suffix.lstrip(".").upper()
        try:  # header read only: image size and transparency
            with Image.open(self.output / item["name"]) as im:
                item["width"], item["height"] = im.size
                item["alpha"] = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        except FileNotFoundError:
            return None
        except OSError:
            item["width"] = item["height"] = None
            item["alpha"] = False
        item["record"] = self._record(meta.get(item["name"]))
        users = sorted(used_by.get(item["name"], ()))
        item["used_by_count"] = len(users)
        item["used_by"] = users[:20]
        return item

    def single(self, request):
        """One image with its record, for jumping to a reference from the preview."""
        try:
            path = self.image_path(request.path_params["name"])
            st = path.stat()
        except (OSError, ValueError):
            return self.reply({"error": "图片不存在，可能已经被删除。"}, 404)
        meta = self.all_meta()
        item = self._item({"name": path.name, "size": st.st_size, "modified": st.st_mtime},
                          meta, self._used_by(meta))
        if item is None:
            return self.reply({"error": "图片不存在，可能已经被删除。"}, 404)
        return self.reply({"item": item})

    def image(self, request):
        try:
            path = self.image_path(request.path_params["name"])
            if "/thumbnail/" not in request.url.path:
                return FileResponse(path, headers=HEADERS)
            from PIL import Image
            with Image.open(path) as im:
                im.thumbnail((480, 480))
                data = io.BytesIO()
                if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
                    # Keep transparency; flattening to JPEG turns it black.
                    im.convert("RGBA").save(data, format="PNG", optimize=True)
                    media = "image/png"
                else:
                    im.convert("RGB").save(data, format="JPEG", quality=78)
                    media = "image/jpeg"
            return Response(data.getvalue(), media_type=media, headers=HEADERS)
        except (OSError, ValueError):
            return self.reply({"error": "图片不存在或无法预览。"}, 404)

    def delete(self, data):
        names = data.get("names")
        if (not isinstance(names, list) or not 1 <= len(names) <= 100
                or not all(isinstance(n, str) for n in names) or len(set(names)) != len(names)):
            return self.reply({"error": "请选择 1～100 张图片。"}, 400)
        if not self.lock.acquire(blocking=False):
            return self.reply({"error": "正在生成图片，请完成后再删除。"}, 409)
        try:
            try:
                paths = [self.image_path(name) for name in names]
            except OSError:
                return self.reply({"error": "部分图片已不存在，请刷新后重新选择。"}, 400)
            deleted = []
            try:
                for path in paths:
                    path.unlink()
                    deleted.append(path.name)
                    record = self.output / ".download-tokens" / hashlib.sha256(path.name.encode()).hexdigest()
                    record.unlink(missing_ok=True)
                    self.delete_meta(path.name)
            except OSError:
                return self.reply({"error": "部分删除未完成，请刷新查看。", "deleted": deleted}, 500)
            return self.reply({"deleted": deleted})
        finally:
            self.lock.release()

    def page(self, request):
        return FileResponse(ASSETS / "index.html", headers=HEADERS)

    def asset(self, request):
        name = request.path_params["name"]
        if name not in {"app.js", "style.css", "icon.svg"}:
            return Response(status_code=404)
        return FileResponse(ASSETS / name, headers=HEADERS)

    def routes(self):
        return [Route("/", lambda r: RedirectResponse("/admin")), Route("/admin", self.page),
                Route("/admin/", self.page), Route("/admin/assets/{name}", self.asset),
                Route("/admin/api/login", self.login, methods=["POST"]),
                Route("/admin/api/state", self.api),
                Route("/admin/api/logout", self.api, methods=["POST"]),
                Route("/admin/api/delete", self.api, methods=["POST"]),
                Route("/admin/api/thumbnail/{name}", self.api),
                Route("/admin/api/item/{name}", self.api),
                Route("/admin/api/image/{name}", self.api)]
