from __future__ import annotations

import argparse
import base64
import fnmatch
import email.utils
import getpass
import hmac
import http.cookiejar
import hashlib
import json
import os
import pty
import re
import select
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://jumpserver.example.com"
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "jumpserver-cli"
CONFIG_FILE = "config.json"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "jumpserver-cli"
COOKIE_FILE = "cookies.txt"
SESSION_FILE = "session.json"
CREDENTIALS_FILE = "credentials.json"
TOKEN_CACHE_FILE = "tokens.json"
TOKEN_META_FILE = "token-meta.json"
DEFAULT_TOKEN_CACHE_TTL = 600
DEFAULT_TOKEN_REFRESH_COOLDOWN = 30
DEFAULT_COMMAND_INJECT_DELAY = 1.2
DEFAULT_PTY_TIMEOUT = 300
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class JumpCliError(Exception):
    pass


class SafetyError(JumpCliError):
    pass


class RemoteExecutionError(JumpCliError):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class SshToken:
    filename: str
    protocol: str
    username: str
    jump_host: str
    jump_port: int
    temp_username: str
    temp_password: str
    raw: dict[str, Any]


@dataclass
class ResolvedTarget:
    asset: dict[str, Any]
    system_user: dict[str, Any]


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def secure_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRWXU)


def config_path() -> Path:
    configured = os.environ.get("JMS_CONFIG_FILE")
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG_DIR / CONFIG_FILE


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise JumpCliError(f"invalid config file: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise JumpCliError(f"config file must contain a JSON object: {path}")
    return data


def configured_base_url() -> str:
    value = os.environ.get("JMS_BASE_URL") or load_config().get("base_url") or DEFAULT_BASE_URL
    return str(value).rstrip("/")


def configured_org_id() -> str:
    value = os.environ.get("JMS_ORG_ID") or load_config().get("org_id") or DEFAULT_ORG_ID
    return str(value)


def save_config(updates: dict[str, str]) -> Path:
    path = config_path()
    data = load_config()
    for key, value in updates.items():
        if value:
            data[key] = value
    secure_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def parse_cookie_pairs(cookie_text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for part in cookie_text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        if name:
            pairs[name] = value
    return pairs


def parse_gm_cookie_items(items: Any) -> dict[str, str]:
    if not isinstance(items, list):
        return {}
    pairs: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str) and name:
            pairs[name] = value
    return pairs


def load_cookie_payload(args: argparse.Namespace) -> dict[str, Any]:
    sources = [args.cookie, args.cookie_json, args.cookie_file]
    if sum(1 for item in sources if item) > 1:
        raise JumpCliError("use only one of --cookie, --cookie-json, or --cookie-file")

    if args.cookie:
        return {"origin": args.base_url, "cookie": args.cookie}

    if args.cookie_json:
        try:
            data = json.loads(args.cookie_json)
        except json.JSONDecodeError as exc:
            raise JumpCliError(f"invalid --cookie-json: {exc}") from exc
        if not isinstance(data, dict):
            raise JumpCliError("--cookie-json must be a JSON object")
        return data

    if args.cookie_file:
        text = Path(args.cookie_file).read_text(encoding="utf-8").strip()
        return parse_cookie_payload_text(text, args.base_url)

    if getattr(args, "prompt", False) or getattr(args, "from_browser", False) or sys.stdin.isatty():
        print("Paste TamperMonkey JSON or raw Cookie header. Input is hidden.", file=sys.stderr)
        text = getpass.getpass("Browser session: ").strip()
        if text:
            return parse_cookie_payload_text(text, args.base_url)

    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            return parse_cookie_payload_text(text, args.base_url)

    raise JumpCliError("provide cookie data with --cookie-file, --cookie-json, --cookie, or stdin")


def parse_cookie_payload_text(text: str, base_url: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {"origin": base_url, "cookie": text}


class SessionStore:
    def __init__(self, cache_dir: Path, base_url: str) -> None:
        self.cache_dir = cache_dir
        self.base_url = base_url.rstrip("/")
        self.cookie_path = cache_dir / COOKIE_FILE
        self.session_path = cache_dir / SESSION_FILE
        self.credentials_path = cache_dir / CREDENTIALS_FILE
        self.token_cache_path = cache_dir / TOKEN_CACHE_FILE
        self.token_meta_path = cache_dir / TOKEN_META_FILE
        self.cookie_jar = http.cookiejar.MozillaCookieJar(str(self.cookie_path))

    @property
    def host(self) -> str:
        parsed = urllib.parse.urlparse(self.base_url)
        return parsed.hostname or "jumpserver.example.com"

    def load(self) -> None:
        if self.cookie_path.exists():
            self.cookie_jar.load(ignore_discard=True, ignore_expires=True)

    def save_payload(self, payload: dict[str, Any]) -> None:
        cookie_text = str(payload.get("cookie") or "").strip()

        origin = str(payload.get("origin") or self.base_url).rstrip("/")
        pairs = parse_cookie_pairs(cookie_text)
        pairs.update(parse_gm_cookie_items(payload.get("gmCookies")))
        if not pairs:
            raise JumpCliError("payload does not contain cookie data")
        if "jms_sessionid" not in pairs:
            raise JumpCliError(
                "cookie data does not include jms_sessionid; use the GM_cookie TamperMonkey script or Playwright mode"
            )
        if "jms_csrftoken" not in pairs:
            raise JumpCliError("cookie does not include jms_csrftoken")

        ensure_private_dir(self.cache_dir)
        self.cookie_jar.clear()
        domain = urllib.parse.urlparse(origin).hostname or self.host
        now = int(time.time())
        for name, value in pairs.items():
            cookie = http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
            self.cookie_jar.set_cookie(cookie)
        self.cookie_jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(self.cookie_path, stat.S_IRUSR | stat.S_IWUSR)

        metadata = {
            "origin": origin,
            "base_url": self.base_url,
            "saved_at": now,
            "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "cookie_names": sorted(pairs.keys()),
        }
        if "href" in payload:
            metadata["href"] = payload["href"]
        if "copiedAt" in payload:
            metadata["copiedAt"] = payload["copiedAt"]
        secure_write_text(self.session_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")

    def csrf_token(self) -> str:
        for cookie in self.cookie_jar:
            if cookie.name == "jms_csrftoken":
                return cookie.value
        raise JumpCliError("cached cookies do not include jms_csrftoken")

    def summary(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if self.session_path.exists():
            try:
                metadata = json.loads(self.session_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                metadata = {}
        cookie_names = sorted(cookie.name for cookie in self.cookie_jar)
        return {
            "cache_dir": str(self.cache_dir),
            "cookie_file": str(self.cookie_path),
            "session_file": str(self.session_path),
            "credentials_file": str(self.credentials_path),
            "token_cache_file": str(self.token_cache_path),
            "token_meta_file": str(self.token_meta_path),
            "auth_mode": self.auth_mode(),
            "cookie_names": cookie_names,
            "metadata": metadata,
        }

    def save_aksk(self, key_id: str, secret: str, org_id: str) -> None:
        key_id = key_id.strip()
        secret = secret.strip()
        org_id = org_id.strip()
        if not key_id or not secret:
            raise JumpCliError("access key id and secret are required")
        ensure_private_dir(self.cache_dir)
        payload = {
            "type": "aksk",
            "base_url": self.base_url,
            "key_id": key_id,
            "secret": secret,
            "org_id": org_id,
            "saved_at": int(time.time()),
        }
        secure_write_text(self.credentials_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def load_aksk(self) -> dict[str, str] | None:
        key_id = os.environ.get("JMS_ACCESS_KEY_ID")
        secret = os.environ.get("JMS_ACCESS_KEY_SECRET")
        org_id = configured_org_id()
        if key_id and secret:
            return {"key_id": key_id, "secret": secret, "org_id": org_id}
        if not self.credentials_path.exists():
            return None
        try:
            payload = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise JumpCliError(f"invalid credentials file: {self.credentials_path}") from exc
        if payload.get("type") != "aksk":
            return None
        if not payload.get("key_id") or not payload.get("secret"):
            raise JumpCliError(f"incomplete AK/SK credentials: {self.credentials_path}")
        return {
            "key_id": str(payload["key_id"]),
            "secret": str(payload["secret"]),
            "org_id": str(payload.get("org_id") or org_id),
        }

    def auth_mode(self) -> str:
        if os.environ.get("JMS_ACCESS_KEY_ID") and os.environ.get("JMS_ACCESS_KEY_SECRET"):
            return "aksk-env"
        if self.credentials_path.exists():
            return "aksk"
        if self.cookie_path.exists():
            return "cookie"
        return "none"

    def has_auth(self) -> bool:
        return self.auth_mode() != "none"

    def token_cache_ttl(self) -> int:
        raw = os.environ.get("JMS_TOKEN_CACHE_TTL")
        if raw is None or raw == "":
            return DEFAULT_TOKEN_CACHE_TTL
        try:
            return max(0, int(raw))
        except ValueError as exc:
            raise JumpCliError("JMS_TOKEN_CACHE_TTL must be an integer number of seconds") from exc

    def token_cache_enabled(self) -> bool:
        return os.environ.get("JMS_TOKEN_CACHE_DISABLE", "").lower() not in {"1", "true", "yes", "on"}

    def token_refresh_cooldown(self) -> int:
        raw = os.environ.get("JMS_TOKEN_REFRESH_COOLDOWN")
        if raw is None or raw == "":
            return DEFAULT_TOKEN_REFRESH_COOLDOWN
        try:
            return max(0, int(raw))
        except ValueError as exc:
            raise JumpCliError("JMS_TOKEN_REFRESH_COOLDOWN must be an integer number of seconds") from exc

    def token_cache_key(self, asset_id: str, system_user_id: str) -> str:
        return "|".join([self.base_url, self.auth_mode(), asset_id, system_user_id])

    def load_token_cache(self) -> dict[str, Any]:
        if not self.token_cache_path.exists():
            return {}
        try:
            data = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def load_token_meta(self) -> dict[str, Any]:
        if not self.token_meta_path.exists():
            return {}
        try:
            data = json.loads(self.token_meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def save_token_meta(self, data: dict[str, Any]) -> None:
        ensure_private_dir(self.cache_dir)
        secure_write_text(self.token_meta_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def save_token_cache(self, data: dict[str, Any]) -> None:
        ensure_private_dir(self.cache_dir)
        secure_write_text(self.token_cache_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def get_cached_token(self, asset_id: str, system_user_id: str) -> SshToken | None:
        if not self.token_cache_enabled():
            return None
        ttl = self.token_cache_ttl()
        if ttl <= 0:
            return None
        cache = self.load_token_cache()
        entry = cache.get(self.token_cache_key(asset_id, system_user_id))
        if not isinstance(entry, dict):
            return None
        if int(time.time()) >= int(entry.get("expires_at") or 0):
            return None
        try:
            return SshToken(
                filename=str(entry["filename"]),
                protocol=str(entry["protocol"]),
                username=str(entry["username"]),
                jump_host=str(entry["jump_host"]),
                jump_port=int(entry["jump_port"]),
                temp_username=str(entry["temp_username"]),
                temp_password=str(entry["temp_password"]),
                raw=dict(entry.get("raw") or {}),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def put_cached_token(self, asset_id: str, system_user_id: str, token: SshToken) -> None:
        if not self.token_cache_enabled():
            return
        ttl = self.token_cache_ttl()
        if ttl <= 0:
            return
        now = int(time.time())
        cache = self.load_token_cache()
        cache[self.token_cache_key(asset_id, system_user_id)] = {
            "created_at": now,
            "expires_at": now + ttl,
            "asset_id": asset_id,
            "system_user_id": system_user_id,
            "filename": token.filename,
            "protocol": token.protocol,
            "username": token.username,
            "jump_host": token.jump_host,
            "jump_port": token.jump_port,
            "temp_username": token.temp_username,
            "temp_password": token.temp_password,
            "raw": token.raw,
        }
        self.save_token_cache(cache)
        meta = self.load_token_meta()
        meta[self.token_cache_key(asset_id, system_user_id)] = {"last_client_url_at": now}
        self.save_token_meta(meta)

    def invalidate_cached_token(self, asset_id: str, system_user_id: str) -> None:
        cache = self.load_token_cache()
        key = self.token_cache_key(asset_id, system_user_id)
        if key in cache:
            del cache[key]
            self.save_token_cache(cache)

    def enforce_token_refresh_cooldown(self, asset_id: str, system_user_id: str) -> None:
        cooldown = self.token_refresh_cooldown()
        if cooldown <= 0:
            return
        key = self.token_cache_key(asset_id, system_user_id)
        meta = self.load_token_meta().get(key)
        if not isinstance(meta, dict):
            return
        last = int(meta.get("last_client_url_at") or 0)
        remaining = cooldown - (int(time.time()) - last)
        if remaining > 0:
            raise JumpCliError(
                f"client-url refresh is cooling down for {remaining}s; "
                "reuse the cached token or wait before requesting another one"
            )


class JumpServerClient:
    def __init__(self, store: SessionStore, timeout: int = 20, debug: bool = False) -> None:
        self.store = store
        self.timeout = timeout
        self.debug = debug
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(store.cookie_jar),
            NoRedirectHandler,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        accept_json: bool = True,
    ) -> Any:
        url_path = path
        if query:
            url_path += "?" + urllib.parse.urlencode(query)
        url = self.store.base_url + url_path

        data = None
        headers = {
            "Accept": "application/json, text/plain, */*" if accept_json else "*/*",
            "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8",
            "DNT": "1",
            "Referer": self.store.base_url + "/ui/",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Origin"] = self.store.base_url

        aksk = self.store.load_aksk()
        if aksk:
            headers["X-JMS-ORG"] = aksk["org_id"]
            self._apply_http_signature(headers, method.upper(), url_path, aksk["key_id"], aksk["secret"])
        elif json_body is not None:
            headers["X-CSRFToken"] = self.store.csrf_token()

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        if self.debug:
            print(f"> {method.upper()} {url}", file=sys.stderr)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                body = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                return self._parse_response(resp.status, resp.geturl(), content_type, body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            location = exc.headers.get("Location", "")
            content_type = exc.headers.get("Content-Type", "")
            if exc.code in (301, 302, 303, 307, 308) and "/core/auth/login" in location:
                raise JumpCliError("cached session is expired: redirected to login") from exc
            if exc.code in (401, 403):
                raise JumpCliError(f"cached session is not authorized: HTTP {exc.code}") from exc
            parsed = self._body_preview(body, content_type)
            raise JumpCliError(f"HTTP {exc.code} from JumpServer: {parsed}") from exc
        except urllib.error.URLError as exc:
            raise JumpCliError(f"request failed: {exc}") from exc

    def _apply_http_signature(
        self,
        headers: dict[str, str],
        method: str,
        url_path: str,
        key_id: str,
        secret: str,
    ) -> None:
        date = email.utils.formatdate(usegmt=True)
        headers["Date"] = date
        accept = headers.get("Accept", "application/json")
        signing = "\n".join(
            [
                f"(request-target): {method.lower()} {url_path}",
                f"accept: {accept}",
                f"date: {date}",
            ]
        )
        signature = base64.b64encode(
            hmac.new(secret.encode("utf-8"), signing.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        headers["Authorization"] = (
            f'Signature keyId="{key_id}",algorithm="hmac-sha256",'
            f'headers="(request-target) accept date",signature="{signature}"'
        )

    def _parse_response(self, status: int, url: str, content_type: str, body: bytes) -> Any:
        text = body.decode("utf-8", errors="replace")
        if "/core/auth/login" in url:
            raise JumpCliError("cached session is expired: received login page")
        if "text/html" in content_type and re.search(r"login|csrfmiddlewaretoken", text, re.I):
            raise JumpCliError("cached session is expired: received login HTML")
        if "application/json" in content_type or text.strip().startswith(("{", "[")):
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise JumpCliError(f"invalid JSON response: {exc}") from exc
        if status == 204:
            return None
        raise JumpCliError(f"unexpected response content-type {content_type!r}: {text[:200]}")

    def _body_preview(self, body: bytes, content_type: str) -> str:
        text = body.decode("utf-8", errors="replace").strip()
        if "text/html" in content_type:
            return "HTML response"
        return text[:300] or "empty response"

    def probe(self, search: str = "127.0.0.1") -> bool:
        self.assets_tree(search)
        return True

    def assets_tree(self, search: str) -> list[dict[str, Any]]:
        result = self.request(
            "GET",
            "/api/v1/perms/users/assets/tree/",
            query={"search": search},
        )
        if not isinstance(result, list):
            raise JumpCliError("assets tree response is not a list")
        return result

    def system_users(self, asset_id: str) -> list[dict[str, Any]]:
        result = self.request("GET", f"/api/v1/perms/users/assets/{asset_id}/system-users/")
        if not isinstance(result, list):
            raise JumpCliError("system-users response is not a list")
        return result

    def client_url(self, asset_id: str, system_user_id: str) -> str:
        result = self.request(
            "POST",
            "/api/v1/authentication/connection-token/client-url/",
            query={"full_screen": "1"},
            json_body={"asset": asset_id, "system_user": system_user_id},
        )
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            raise JumpCliError("client-url response does not contain url")
        return result["url"]


def choose_asset(items: list[dict[str, Any]], query: str) -> dict[str, Any]:
    if not items:
        raise JumpCliError(f"no asset matched {query!r}")

    exact: list[dict[str, Any]] = []
    for item in items:
        data = item.get("meta", {}).get("data", {})
        if item.get("title") == query or data.get("ip") == query or data.get("hostname") == query:
            exact.append(item)
    if len(exact) == 1:
        return exact[0]
    if len(items) == 1:
        return items[0]

    if sys.stdin.isatty():
        return interactive_choose_asset(items, query)

    lines = [f"multiple assets matched {query!r}:"]
    for idx, item in enumerate(items, start=1):
        data = item.get("meta", {}).get("data", {})
        lines.append(
            f"  {idx}. {data.get('ip') or item.get('title')} "
            f"{data.get('hostname') or item.get('name')} [{item.get('id')}]"
        )
    raise JumpCliError("\n".join(lines) + "\nUse a more specific search string.")


def asset_label(item: dict[str, Any]) -> str:
    data = item.get("meta", {}).get("data", {})
    ip = data.get("ip") or item.get("title") or ""
    hostname = data.get("hostname") or item.get("name") or ""
    platform = data.get("platform") or ""
    protocols = ",".join(data.get("protocols") or [])
    return f"{ip:<15} {hostname} {platform} {protocols}".strip()


def interactive_choose_asset(items: list[dict[str, Any]], query: str) -> dict[str, Any]:
    candidates = items
    pattern = ""
    while True:
        print(f"\nMultiple assets matched {query!r}.", file=sys.stderr)
        if pattern:
            print(f"Filter: {pattern}", file=sys.stderr)
        for idx, item in enumerate(candidates[:20], start=1):
            print(f"{idx:2d}. {asset_label(item)}", file=sys.stderr)
        if len(candidates) > 20:
            print(f"... {len(candidates) - 20} more", file=sys.stderr)
        answer = input("Select number, type filter, or empty for 1: ").strip()
        if answer == "":
            return candidates[0]
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(candidates):
                return candidates[index - 1]
            print("Invalid selection.", file=sys.stderr)
            continue
        pattern = answer
        lowered = pattern.lower()
        filtered = [
            item for item in items
            if lowered in asset_label(item).lower() or fnmatch.fnmatch(asset_label(item).lower(), lowered)
        ]
        if not filtered:
            print("No candidates matched that filter.", file=sys.stderr)
            continue
        candidates = filtered


def choose_system_user(users: list[dict[str, Any]], preferred: str | None) -> dict[str, Any]:
    if not users:
        raise JumpCliError("asset has no available system users")
    if preferred:
        matches = [
            user for user in users
            if user.get("username") == preferred or user.get("name") == preferred or user.get("id") == preferred
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise JumpCliError(f"multiple system users matched {preferred!r}")
        raise JumpCliError(f"no system user matched {preferred!r}")

    ops_matches = [user for user in users if user.get("username") == "ops"]
    if len(ops_matches) == 1:
        return ops_matches[0]

    ssh_auto = [
        user for user in users
        if user.get("protocol") == "ssh" and user.get("login_mode") == "auto"
    ]
    if len(ssh_auto) == 1:
        return ssh_auto[0]
    if len(users) == 1:
        return users[0]

    if sys.stdin.isatty():
        return interactive_choose_system_user(users)

    lines = ["multiple system users are available:"]
    for idx, user in enumerate(users, start=1):
        lines.append(
            f"  {idx}. {user.get('name')} username={user.get('username')} "
            f"protocol={user.get('protocol')} id={user.get('id')}"
        )
    raise JumpCliError("\n".join(lines) + "\nUse --system-user to choose one.")


def interactive_choose_system_user(users: list[dict[str, Any]]) -> dict[str, Any]:
    print("\nMultiple system users are available.", file=sys.stderr)
    for idx, user in enumerate(users, start=1):
        print(
            f"{idx:2d}. {user.get('name')} username={user.get('username')} "
            f"protocol={user.get('protocol')} id={user.get('id')}",
            file=sys.stderr,
        )
    answer = input("Select number, or empty for 1: ").strip()
    if answer == "":
        return users[0]
    if not answer.isdigit() or not (1 <= int(answer) <= len(users)):
        raise JumpCliError("invalid system user selection")
    return users[int(answer) - 1]


def decode_jms_url(url: str) -> SshToken:
    if not url.startswith("jms://"):
        raise JumpCliError(f"unsupported client URL: {url[:40]}")
    encoded = url[len("jms://"):]
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        outer = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except Exception as exc:
        raise JumpCliError(f"failed to decode jms URL: {exc}") from exc
    token_text = outer.get("token")
    if not isinstance(token_text, str):
        raise JumpCliError("decoded jms URL does not contain token JSON")
    try:
        token = json.loads(token_text)
    except json.JSONDecodeError as exc:
        raise JumpCliError(f"failed to decode token JSON: {exc}") from exc

    try:
        return SshToken(
            filename=str(outer.get("filename") or ""),
            protocol=str(outer.get("protocol") or ""),
            username=str(outer.get("username") or ""),
            jump_host=str(token["ip"]),
            jump_port=int(token["port"]),
            temp_username=str(token["username"]),
            temp_password=str(token["password"]),
            raw=outer,
        )
    except KeyError as exc:
        raise JumpCliError(f"token JSON is missing field: {exc}") from exc


def build_client(args: argparse.Namespace) -> tuple[SessionStore, JumpServerClient]:
    cache_dir = Path(args.cache_dir).expanduser() if args.cache_dir else DEFAULT_CACHE_DIR
    store = SessionStore(cache_dir, args.base_url)
    store.load()
    client = JumpServerClient(store, timeout=args.timeout, debug=args.debug)
    return store, client


def verify_auth(client: JumpServerClient, probe_search: str) -> bool:
    try:
        client.probe(probe_search)
        return True
    except JumpCliError as exc:
        print(f"Cached auth is not usable: {exc}", file=sys.stderr)
        return False


def ensure_auth(args: argparse.Namespace, probe_search: str | None = None) -> tuple[SessionStore, JumpServerClient]:
    store, client = build_client(args)
    if store.has_auth() and verify_auth(client, probe_search or "127.0.0.1"):
        return store, client
    if not sys.stdin.isatty():
        raise JumpCliError("no usable cached auth; run login-aksk or login --from-browser first")
    interactive_login(args)
    store, client = build_client(args)
    if not verify_auth(client, probe_search or "127.0.0.1"):
        raise JumpCliError("new auth was saved but did not pass verification")
    return store, client


def interactive_login(args: argparse.Namespace) -> None:
    print("No usable JumpServer auth found.", file=sys.stderr)
    print("1. AK/SK (recommended)", file=sys.stderr)
    print("2. Browser cookie JSON", file=sys.stderr)
    choice = input("Choose auth method [1]: ").strip() or "1"
    cache_dir = Path(args.cache_dir).expanduser() if args.cache_dir else DEFAULT_CACHE_DIR
    store = SessionStore(cache_dir, args.base_url)
    if choice == "1":
        key_id = input("AccessKeyID: ").strip()
        secret = getpass.getpass("AccessKeySecret: ").strip()
        org_id = input(f"Org ID [{configured_org_id()}]: ").strip()
        if not org_id:
            org_id = configured_org_id()
        store.save_aksk(key_id, secret, org_id)
        print(f"Saved JumpServer AK/SK credentials to {store.credentials_path}", file=sys.stderr)
        return
    if choice == "2":
        text = getpass.getpass("Browser session JSON/Cookie: ").strip()
        payload = parse_cookie_payload_text(text, args.base_url)
        store.save_payload(payload)
        print(f"Saved JumpServer cookies to {store.cookie_path}", file=sys.stderr)
        return
    raise JumpCliError("invalid auth method")


def cmd_login(args: argparse.Namespace) -> int:
    store = SessionStore(
        Path(args.cache_dir).expanduser() if args.cache_dir else DEFAULT_CACHE_DIR,
        args.base_url,
    )
    payload = load_cookie_payload(args)
    store.save_payload(payload)
    store.load()
    print(f"Saved JumpServer cookies to {store.cookie_path}")
    print("Cached cookie names:", ", ".join(sorted(cookie.name for cookie in store.cookie_jar)))
    return 0


def cmd_login_aksk(args: argparse.Namespace) -> int:
    store = SessionStore(
        Path(args.cache_dir).expanduser() if args.cache_dir else DEFAULT_CACHE_DIR,
        args.base_url,
    )
    secret = args.secret
    if not secret:
        secret = getpass.getpass("AccessKeySecret: ")
    store.save_aksk(args.key_id, secret, args.org_id)
    print(f"Saved JumpServer AK/SK credentials to {store.credentials_path}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.config_command == "show":
        data = load_config()
        print(f"Config file: {config_path()}")
        print(f"Base URL: {args.base_url}")
        print(f"Org ID: {configured_org_id()}")
        if data:
            print("Saved keys:", ", ".join(sorted(data)))
        else:
            print("Saved keys: none")
        return 0

    updates = {
        "base_url": (args.config_base_url or "").rstrip("/"),
        "org_id": args.config_org_id or "",
    }
    if not any(updates.values()):
        raise JumpCliError("config set requires --base-url or --org-id")
    path = save_config(updates)
    print(f"Saved config to {path}")
    print(f"Base URL: {configured_base_url()}")
    print(f"Org ID: {configured_org_id()}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui import run_tui

    return run_tui(args)


def cmd_status(args: argparse.Namespace) -> int:
    store, client = build_client(args)
    summary = store.summary()
    if summary["auth_mode"] == "none":
        raise JumpCliError(f"no cached auth found under {store.cache_dir}")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Cache: {summary['cache_dir']}")
        print(f"Auth mode: {summary['auth_mode']}")
        print("Cookies:", ", ".join(summary["cookie_names"]))
        print(f"Token cache: {summary['token_cache_file']}")
        print(f"Token cache TTL: {store.token_cache_ttl()}s")
        print(f"Token refresh cooldown: {store.token_refresh_cooldown()}s")
        saved_at = summary["metadata"].get("saved_at_iso")
        if saved_at:
            print(f"Saved at: {saved_at}")
    if args.probe:
        client.probe(args.probe_search)
        print("Session: valid")
    return 0


def resolve_target(client: JumpServerClient, target: str, system_user: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    asset = choose_asset(client.assets_tree(target), target)
    users = client.system_users(str(asset["id"]))
    selected_user = choose_system_user(users, system_user)
    return asset, selected_user


def cmd_resolve(args: argparse.Namespace) -> int:
    _, client = build_client(args)
    asset, user = resolve_target(client, args.target, args.system_user)
    data = asset.get("meta", {}).get("data", {})
    result = {
        "asset": {
            "id": asset.get("id"),
            "name": asset.get("name"),
            "title": asset.get("title"),
            "hostname": data.get("hostname"),
            "ip": data.get("ip"),
            "platform": data.get("platform"),
            "protocols": data.get("protocols"),
        },
        "system_user": user,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def resolve_for_args(args: argparse.Namespace) -> tuple[SessionStore, JumpServerClient, dict[str, Any], dict[str, Any]]:
    store, client = ensure_auth(args, args.target)
    asset, user = resolve_target(client, args.target, args.system_user)
    return store, client, asset, user


def get_token_for_resolved(
    store: SessionStore,
    client: JumpServerClient,
    asset: dict[str, Any],
    user: dict[str, Any],
    *,
    refresh: bool = False,
    cache_only: bool = False,
    quiet: bool = False,
) -> SshToken:
    asset_id = str(asset["id"])
    system_user_id = str(user["id"])
    if not refresh:
        cached = store.get_cached_token(asset_id, system_user_id)
        if cached:
            return cached
    if cache_only:
        raise JumpCliError(
            "no valid cached token is available for --print-command; "
            "run the command without --print-command once, or wait for an existing token"
        )
    if refresh:
        if not quiet:
            print("Cached SSH token failed or expired; refreshing client-url token once.", file=sys.stderr)
        store.invalidate_cached_token(asset_id, system_user_id)
    else:
        if not quiet:
            print("No valid cached SSH token; requesting a new client-url token.", file=sys.stderr)
    store.enforce_token_refresh_cooldown(asset_id, system_user_id)
    url = client.client_url(str(asset["id"]), str(user["id"]))
    token = decode_jms_url(url)
    store.put_cached_token(asset_id, system_user_id, token)
    return token


def get_token_for_target(args: argparse.Namespace, *, refresh: bool = False, cache_only: bool = False) -> SshToken:
    store, client, asset, user = resolve_for_args(args)
    return get_token_for_resolved(store, client, asset, user, refresh=refresh, cache_only=cache_only)


def token_to_dict(token: SshToken, include_password: bool = False) -> dict[str, Any]:
    data = {
        "filename": token.filename,
        "protocol": token.protocol,
        "jump_username": token.username,
        "ssh": {
            "host": token.jump_host,
            "port": token.jump_port,
            "username": token.temp_username,
        },
    }
    if include_password:
        data["ssh"]["password"] = token.temp_password
    return data


def cmd_token(args: argparse.Namespace) -> int:
    token = get_token_for_target(args, cache_only=getattr(args, "print_command", False))
    print(json.dumps(token_to_dict(token, include_password=args.show_password), ensure_ascii=False, indent=2))
    return 0


def build_ssh_command(
    token: SshToken,
    *,
    ssh_options: list[str] | None = None,
    ssh_args: list[str] | None = None,
    force_tty: bool = False,
) -> list[str]:
    if not command_exists("sshpass"):
        raise JumpCliError("sshpass is required for direct ssh; install it or use token --show-password")
    cmd = [
        "sshpass",
        "-p",
        token.temp_password,
        "ssh",
        "-p",
        str(token.jump_port),
        f"{token.temp_username}@{token.jump_host}",
    ]
    if ssh_options:
        expanded: list[str] = []
        for opt in ssh_options:
            expanded.extend(["-o", opt])
        cmd[4:4] = expanded
    if ssh_args:
        cmd[4:4] = ssh_args
    if force_tty:
        cmd[4:4] = ["-tt"]
    return cmd


def command_to_string(command: list[str]) -> str:
    return " ".join(shell_quote(part) for part in command)


def remote_command_line(parts: list[str]) -> str:
    return " ".join(parts)


def remote_script(command_line: str) -> str:
    return f"{command_line}\r\n__jms_rc=$?\r\nexit $__jms_rc\r\n"


def pipeline_command_to_string(command: list[str], script: str) -> str:
    lines = [line for line in script.rstrip("\r\n").splitlines()]
    printf = "printf '%s\\r\\n' " + " ".join(shell_quote(line) for line in lines)
    return f"( sleep {command_inject_delay():g}; {printf} ) | " + pty_wrapped_command_to_string(command)


def run_ssh_with_script(command: list[str], script: str) -> int:
    run_command = pty_wrapped_command(command)
    proc = subprocess.Popen(run_command, stdin=subprocess.PIPE, text=True)
    time.sleep(command_inject_delay())
    try:
        assert proc.stdin is not None
        proc.stdin.write(script)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    return proc.wait()


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
PROMPT_RE = re.compile(r"(?m)(^|\n)[^\n\r]{0,200}[$#] ?$")


def strip_terminal_control(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\r", "")
    text = re.sub(r"\x08+", "", text)
    return text


def read_pty(master_fd: int, proc: subprocess.Popen, timeout: float, until: str | None = None) -> str:
    deadline = time.time() + timeout
    chunks: list[str] = []
    while time.time() < deadline:
        if proc.poll() is not None:
            # Drain remaining output.
            while True:
                ready, _, _ = select.select([master_fd], [], [], 0)
                if not ready:
                    break
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data.decode("utf-8", errors="replace"))
            break
        ready, _, _ = select.select([master_fd], [], [], 0.1)
        if not ready:
            continue
        try:
            data = os.read(master_fd, 65536)
        except OSError:
            break
        if not data:
            break
        piece = data.decode("utf-8", errors="replace")
        chunks.append(piece)
        if until and until in strip_terminal_control("".join(chunks)):
            break
    return "".join(chunks)


def wait_for_prompt(master_fd: int, proc: subprocess.Popen, timeout: float) -> str:
    deadline = time.time() + timeout
    chunks: list[str] = []
    while time.time() < deadline:
        text = read_pty(master_fd, proc, 0.5)
        if text:
            chunks.append(text)
        clean = strip_terminal_control("".join(chunks))
        if "No PTY requested" in clean:
            raise RemoteExecutionError("JumpServer refused the session: No PTY requested")
        if PROMPT_RE.search(clean):
            return "".join(chunks)
    raise RemoteExecutionError("timed out waiting for remote shell prompt", exit_code=3)


def remote_exec_script(command_line: str) -> tuple[str, str, str, str]:
    seed = f"{os.getpid()}_{int(time.time() * 1000)}"
    start_marker = f"__JEXEC_START_{seed}__"
    exit_marker = f"__JEXEC_EXIT_{seed}__"
    end_marker = f"__JEXEC_END_{seed}__"
    script = "\n".join(
        [
            f"printf '%s\\n' {shell_quote(start_marker)}",
            command_line,
            "__jexec_rc=$?",
            f"printf '%s:%s\\n' {shell_quote(exit_marker)} \"$__jexec_rc\"",
            f"printf '%s\\n' {shell_quote(end_marker)}",
            "exit",
            "",
        ]
    )
    return script, start_marker, exit_marker, end_marker


def parse_marked_output(raw: str, start_marker: str, exit_marker: str, end_marker: str) -> tuple[int, str]:
    clean = strip_terminal_control(raw)
    if os.environ.get("JMS_PTY_DEBUG") == "1":
        Path("/tmp/jms-pty-debug.log").write_text(clean, encoding="utf-8")
    start = clean.find(start_marker)
    exit_pos = clean.find(exit_marker)
    end = clean.find(end_marker)
    if start < 0 or exit_pos < 0 or end < 0 or not (start < exit_pos < end):
        raise RemoteExecutionError("failed to parse remote command markers", exit_code=1)
    body = clean_remote_body(clean[start + len(start_marker):exit_pos])
    exit_line = clean[exit_pos:end].splitlines()[0]
    match = re.search(re.escape(exit_marker) + r":(\d+)", exit_line)
    if not match:
        raise RemoteExecutionError("failed to parse remote exit code", exit_code=1)
    return int(match.group(1)), body


PROMPT_PREFIX_RE = re.compile(r"^\s*(?:\[[^\]\n]{1,160}\]\s*)?[$#]\s*|^\s*\[[^\]\n]{1,160}\]\s*[$#]\s*")


def clean_remote_body(body: str) -> str:
    cleaned_lines: list[str] = []
    for line in body.strip("\n").splitlines():
        line = line.rstrip()
        previous = None
        while previous != line:
            previous = line
            line = PROMPT_PREFIX_RE.sub("", line)
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def run_ssh_shell_command(command: list[str], command_line: str, timeout: int) -> tuple[int, str]:
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(command, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True)
    os.close(slave_fd)
    try:
        wait_for_prompt(master_fd, proc, timeout=min(30, timeout))
        os.write(master_fd, b"stty -echo\n")
        wait_for_prompt(master_fd, proc, timeout=5)
        script, start_marker, exit_marker, end_marker = remote_exec_script(command_line)
        for line in script.splitlines():
            os.write(master_fd, (line + "\n").encode("utf-8"))
            time.sleep(0.08)
        raw = read_pty(master_fd, proc, timeout=timeout, until=end_marker)
        rc, output = parse_marked_output(raw, start_marker, exit_marker, end_marker)
        # Give the remote exit command a moment to close the session.
        read_pty(master_fd, proc, timeout=2)
        return rc, output
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def command_inject_delay() -> float:
    raw = os.environ.get("JMS_COMMAND_INJECT_DELAY")
    if not raw:
        return DEFAULT_COMMAND_INJECT_DELAY
    try:
        return max(0.0, float(raw))
    except ValueError as exc:
        raise JumpCliError("JMS_COMMAND_INJECT_DELAY must be a number of seconds") from exc


def pty_wrapped_command(command: list[str]) -> list[str]:
    if command_exists("script"):
        return ["script", "-qfec", command_to_string(command), "/dev/null"]
    return command


def pty_wrapped_command_to_string(command: list[str]) -> str:
    if command_exists("script"):
        return "script -qfec " + shell_quote(command_to_string(command)) + " /dev/null"
    return command_to_string(command)


def cmd_ssh_command(args: argparse.Namespace) -> int:
    store, client, asset, user = resolve_for_args(args)
    token = get_token_for_resolved(store, client, asset, user, cache_only=args.print_command)
    remote_command = getattr(args, "remote_command", [])
    has_remote_command = bool(remote_command)
    cmd = build_ssh_command(
        token,
        ssh_options=args.ssh_option,
        ssh_args=getattr(args, "ssh_args", []),
        force_tty=has_remote_command,
    )
    if args.print_command:
        if has_remote_command:
            command_line = "bash -lc " + shell_quote(remote_command_line(remote_command))
            print(command_to_string(cmd))
            print("# remote script:")
            print(remote_exec_script(command_line)[0], end="")
        else:
            print(command_to_string(cmd))
        return 0
    if has_remote_command:
        command_line = "bash -lc " + shell_quote(remote_command_line(remote_command))
        rc, output = run_ssh_shell_command(cmd, command_line, args.timeout)
        if output:
            print(output)
        return rc
    rc = subprocess.call(cmd)
    if rc == 255:
        token = get_token_for_resolved(store, client, asset, user, refresh=True)
        cmd = build_ssh_command(
            token,
            ssh_options=args.ssh_option,
            ssh_args=getattr(args, "ssh_args", []),
            force_tty=has_remote_command,
        )
        rc = subprocess.call(cmd)
    return rc


def cmd_exec_command(args: argparse.Namespace) -> int:
    if args.remote_command and args.remote_command[0] == "--":
        args.remote_command = args.remote_command[1:]
    if not args.remote_command:
        raise JumpCliError("remote command is required")
    enforce_exec_safety(args)
    store, client, asset, user = resolve_for_args(args)
    token = get_token_for_resolved(store, client, asset, user, cache_only=args.print_command)
    command_line = "bash -lc " + shell_quote(remote_command_line(args.remote_command))
    if args.sudo:
        command_line = "sudo -i -- " + command_line
    cmd = build_ssh_command(token, force_tty=True)
    if args.print_command:
        print(command_to_string(cmd))
        print("# remote script:")
        print(remote_exec_script(command_line)[0], end="")
        return 0
    rc, output = run_ssh_shell_command(cmd, command_line, args.timeout)
    if output:
        print(output)
    if rc == 255:
        token = get_token_for_resolved(store, client, asset, user, refresh=True)
        cmd = build_ssh_command(token, force_tty=True)
        rc, output = run_ssh_shell_command(cmd, command_line, args.timeout)
        if output:
            print(output)
    return rc


def split_scp_target(value: str) -> tuple[str, str] | None:
    if ":" not in value:
        return None
    host, path = value.split(":", 1)
    if not host or not path:
        return None
    return host, path


HIGH_RISK_COMMAND_PATTERNS = [
    r"\brm\s+(-[^\s]*r[^\s]*f|-[^\s]*f[^\s]*r)\b",
    r"\bmkfs(\.|\s|$)",
    r"\bwipefs\b",
    r"\bdd\s+.*\bof=",
    r"\b(fdisk|parted)\b",
    r"\b(reboot|shutdown|poweroff|halt)\b",
    r"\biptables\s+-F\b",
    r"\bnft\s+flush\b",
    r"\bsystemctl\s+(restart|stop|disable|mask)\b",
    r"\bdocker\s+(rm|stop|restart|kill|system\s+prune)\b",
    r"\bkubectl\s+(delete|drain|cordon|scale)\b",
    r"\bkubectl\s+rollout\s+restart\b",
    r"\bchmod\s+(-[^\s]*R|--recursive)\b",
    r"\bchown\s+(-[^\s]*R|--recursive)\b",
    r">\s*/(etc|usr|boot|root|var/lib|opt)/",
]

SENSITIVE_REMOTE_PREFIXES = (
    "/etc/",
    "/usr/",
    "/boot/",
    "/root/",
    "/var/lib/",
    "/opt/",
    "/lib/",
    "/lib64/",
    "/bin/",
    "/sbin/",
)


def enforce_exec_safety(args: argparse.Namespace) -> None:
    reasons: list[str] = []
    command = " ".join(args.remote_command)
    if args.sudo:
        reasons.append("--sudo requested")
    for pattern in HIGH_RISK_COMMAND_PATTERNS:
        if re.search(pattern, command):
            reasons.append(f"high-risk command pattern: {pattern}")
            break
    if reasons and not args.yes:
        raise SafetyError(
            "refusing high-risk remote command without --yes\n"
            f"host: {args.target}\n"
            f"command: {command}\n"
            "reason: " + "; ".join(reasons) + "\n"
            "Re-run with --yes only after explicit user confirmation."
        )


def remote_path_is_sensitive(path: str) -> bool:
    expanded = path
    if expanded.startswith("~/"):
        return False
    if expanded == "~":
        return False
    return any(expanded == prefix.rstrip("/") or expanded.startswith(prefix) for prefix in SENSITIVE_REMOTE_PREFIXES)


def enforce_scp_safety(args: argparse.Namespace, target: str, remote_path: str, uploading: bool) -> None:
    if not uploading:
        return
    if remote_path_is_sensitive(remote_path) and not args.yes:
        raise SafetyError(
            "refusing upload to sensitive remote path without --yes\n"
            f"host: {target}\n"
            f"remote path: {remote_path}\n"
            "Upload to /tmp first, then use jexec --sudo --yes to move it after confirmation."
        )


def build_scp_command(args: argparse.Namespace, token: SshToken, rewritten_paths: list[str]) -> list[str]:
    if not command_exists("sshpass"):
        raise JumpCliError("sshpass is required for jscp")
    cmd = [
        "sshpass",
        "-p",
        token.temp_password,
        "scp",
        "-P",
        str(token.jump_port),
    ]
    if args.scp_option:
        cmd.extend(args.scp_option)
    if args.scp_o:
        for opt in args.scp_o:
            cmd.extend(["-o", opt])
    cmd.extend(rewritten_paths)
    return cmd


def cmd_scp_command(args: argparse.Namespace) -> int:
    remote_specs = [split_scp_target(path) for path in args.paths]
    remote_count = sum(1 for spec in remote_specs if spec is not None)
    if remote_count != 1:
        raise JumpCliError("jscp requires exactly one remote path in the form <target>:<path>")

    remote_index = next(idx for idx, spec in enumerate(remote_specs) if spec is not None)
    target, remote_path = remote_specs[remote_index] or ("", "")
    uploading = remote_index == len(args.paths) - 1
    enforce_scp_safety(args, target, remote_path, uploading)
    args.target = target
    store, client, asset, user = resolve_for_args(args)
    token = get_token_for_resolved(store, client, asset, user, cache_only=args.print_command)

    rewritten = list(args.paths)
    rewritten[remote_index] = f"{token.temp_username}@{token.jump_host}:{remote_path}"
    cmd = build_scp_command(args, token, rewritten)
    if args.print_command:
        print(command_to_string(cmd))
        return 0
    rc = subprocess.call(cmd)
    if rc == 255:
        token = get_token_for_resolved(store, client, asset, user, refresh=True)
        rewritten[remote_index] = f"{token.temp_username}@{token.jump_host}:{remote_path}"
        cmd = build_scp_command(args, token, rewritten)
        rc = subprocess.call(cmd)
    return rc


def command_exists(name: str) -> bool:
    path = os.environ.get("PATH", "")
    for directory in path.split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return True
    return False


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=configured_base_url(), help="JumpServer base URL")
    parser.add_argument("--cache-dir", help="cache directory, default: ~/.cache/jumpserver-cli")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds")
    parser.add_argument("--debug", action="store_true", help="print HTTP request debug information")


def build_parser(prog: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="JumpServer CLI", prog=prog)
    add_common(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="cache browser cookies")
    login.add_argument("--from-browser", action="store_true", help="alias for reading plugin JSON/cookie from stdin")
    login.add_argument("--prompt", action="store_true", help="prompt for browser session data with hidden input")
    login.add_argument("--cookie", help="raw Cookie header value")
    login.add_argument("--cookie-json", help="TamperMonkey JSON payload")
    login.add_argument("--cookie-file", help="file containing TamperMonkey JSON or raw cookie string")
    login.set_defaults(func=cmd_login)

    login_aksk = subparsers.add_parser("login-aksk", help="cache JumpServer AccessKey credentials")
    login_aksk.add_argument("--key-id", required=True, help="JumpServer AccessKeyID")
    login_aksk.add_argument("--secret", help="JumpServer AccessKeySecret; prompted if omitted")
    login_aksk.add_argument("--org-id", default=configured_org_id(), help="JumpServer org id")
    login_aksk.set_defaults(func=cmd_login_aksk)

    config = subparsers.add_parser("config", help="view or save local configuration")
    config_subparsers = config.add_subparsers(dest="config_command", required=True)
    config_show = config_subparsers.add_parser("show", help="show effective configuration")
    config_show.set_defaults(func=cmd_config)
    config_set = config_subparsers.add_parser("set", help="save JumpServer configuration")
    config_set.add_argument("--base-url", dest="config_base_url", help="JumpServer base URL")
    config_set.add_argument("--org-id", dest="config_org_id", help="JumpServer org id")
    config_set.set_defaults(func=cmd_config)

    tui = subparsers.add_parser("tui", help="open the fullscreen asset browser")
    tui.set_defaults(func=cmd_tui)

    status = subparsers.add_parser("status", help="show cached session status")
    status.add_argument("--json", action="store_true", help="print cache metadata as JSON")
    status.add_argument("--probe", action="store_true", help="call JumpServer API to verify session")
    status.add_argument("--probe-search", default="127.0.0.1", help="search value for --probe")
    status.set_defaults(func=cmd_status)

    resolve = subparsers.add_parser("resolve", help="resolve target asset and system user")
    resolve.add_argument("target", help="target IP or hostname")
    resolve.add_argument("--system-user", help="system user username/name/id, default prefers username=ops")
    resolve.set_defaults(func=cmd_resolve)

    token = subparsers.add_parser("token", help="generate and decode a JumpServer SSH token")
    token.add_argument("target", help="target IP or hostname")
    token.add_argument("--system-user", help="system user username/name/id, default prefers username=ops")
    token.add_argument("--show-password", action="store_true", help="include temporary password in output")
    token.set_defaults(func=cmd_token)

    ssh = subparsers.add_parser("ssh", help="connect with native ssh through generated token")
    ssh.add_argument("target", help="target IP or hostname")
    ssh.add_argument("--system-user", help="system user username/name/id, default prefers username=ops")
    ssh.add_argument("-o", "--ssh-option", action="append", help="pass an ssh -o option")
    ssh.add_argument("--print-command", action="store_true", help="print sshpass command instead of executing")
    ssh.set_defaults(ssh_args=[])
    ssh.set_defaults(remote_command=[])
    ssh.set_defaults(func=cmd_ssh_command)

    exec_parser = subparsers.add_parser("exec", help="execute a remote command through generated ssh token")
    exec_parser.add_argument("target", help="target IP or hostname")
    exec_parser.add_argument("--system-user", help="system user username/name/id, default prefers username=ops")
    exec_parser.add_argument("--sudo", action="store_true", help="run remote command via sudo -i")
    exec_parser.add_argument("--yes", action="store_true", help="confirm high-risk or privileged remote command")
    exec_parser.add_argument("--print-command", action="store_true", help="print sshpass command instead of executing")
    exec_parser.add_argument("remote_command", nargs=argparse.REMAINDER, help="remote command after --")
    exec_parser.set_defaults(func=cmd_exec_command)

    scp = subparsers.add_parser("scp", help="copy files with native scp through generated token")
    scp.add_argument("--system-user", help="system user username/name/id, default prefers username=ops")
    scp.add_argument("-r", action="append_const", const="-r", dest="scp_option", help="copy directories recursively")
    scp.add_argument("-p", action="append_const", const="-p", dest="scp_option", help="preserve times and modes")
    scp.add_argument("-q", action="append_const", const="-q", dest="scp_option", help="quiet mode")
    scp.add_argument("-C", action="append_const", const="-C", dest="scp_option", help="enable compression")
    scp.add_argument("-v", action="append_const", const="-v", dest="scp_option", help="verbose mode")
    scp.add_argument("-o", dest="scp_o", action="append", help="pass scp -o option")
    scp.add_argument("--yes", action="store_true", help="confirm upload to sensitive remote paths")
    scp.add_argument("--print-command", action="store_true", help="print sshpass command instead of executing")
    scp.add_argument("paths", nargs="+", help="scp paths; exactly one must be <target>:<path>")
    scp.set_defaults(func=cmd_scp_command)

    return parser


def normalize_args(argv: list[str] | None) -> list[str] | None:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return argv
    common = {"--base-url", "--cache-dir", "--timeout", "--debug"}
    first_subcommand = None
    for idx, arg in enumerate(argv):
        if arg in {"login", "login-aksk", "status", "resolve", "token", "ssh", "exec", "scp", "config", "tui"}:
            first_subcommand = idx
            break
    if first_subcommand is None:
        return argv
    before = argv[:first_subcommand]
    after = argv[first_subcommand + 1:]
    command = argv[first_subcommand]
    if command == "config":
        return argv
    moved: list[str] = []
    remaining_after: list[str] = []
    idx = 0
    while idx < len(after):
        arg = after[idx]
        if arg in common:
            moved.append(arg)
            if arg != "--debug":
                idx += 1
                if idx < len(after):
                    moved.append(after[idx])
        else:
            remaining_after.append(arg)
        idx += 1
    return before + moved + [command] + remaining_after


def split_passthrough(argv: list[str], subcommands: set[str]) -> tuple[list[str], list[str]]:
    command_index = None
    for idx, arg in enumerate(argv):
        if arg in subcommands:
            command_index = idx
            break
    if command_index is None or "--" not in argv[command_index + 1:]:
        return argv, []
    sep = argv.index("--", command_index + 1)
    return argv[:sep], argv[sep + 1:]


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    prog_name = Path(prog or sys.argv[0]).name
    if not argv and prog_name == "jump_cli.py":
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("jump-cli: no arguments require an interactive terminal; use --help", file=sys.stderr)
            return 2
        from .tui import run_tui, tui_args

        return run_tui(tui_args())
    if prog_name in {"jssh", "jexec", "jscp"}:
        mapped = {"jssh": "ssh", "jexec": "exec", "jscp": "scp"}[prog_name]
        argv = [mapped] + argv
    argv, passthrough = split_passthrough(argv, {"ssh"})
    parser = build_parser(prog=prog_name if prog_name not in {"jump_cli.py"} else None)
    args = parser.parse_args(normalize_args(argv))
    if getattr(args, "command", None) == "ssh":
        args.remote_command = passthrough
    try:
        return int(args.func(args))
    except JumpCliError as exc:
        print(f"jump-cli: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("jump-cli: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
