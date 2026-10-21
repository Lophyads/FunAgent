"""Official SIWC public-client OAuth, with protected local credential storage."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from math import isfinite
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

import httpx
import jwt

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
BOOTSTRAP_CLIENT = "dynamic_agent_client"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
DEFAULT_STATE = Path(".funagent/siwc")


from ..frame.contracts import DomainError, RunContext, controlled_lock
from .protocols import ModelError, RemoteError


class SiwcError(ModelError):
    """Only deliberate, credential-free diagnostics reach callers."""
    def __init__(self, message, code="AUTH_REQUIRED"):
        super().__init__(code)
        self.args = (message,)



def safe_label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else "unknown"


def _dpapi(data, *, decrypt=False):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    name = "CryptUnprotectData" if decrypt else "CryptProtectData"
    operation = getattr(crypt, name)
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise SiwcError("Windows DPAPI credential protection failed.", "AUTH_STORAGE")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


class _Store:
    def __init__(self, root=DEFAULT_STATE):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def read(self, name):
        path = self.root / name
        if not path.exists():
            return {}
        try:
            raw = path.read_bytes()
            if name == "credentials.bin" and os.name == "nt":
                raw = _dpapi(raw, decrypt=True)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (OSError, ValueError, UnicodeError):
            raise SiwcError("Cannot read protected SIWC state.", "AUTH_STORAGE") from None

    def write(self, name, value):
        raw = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        if name == "credentials.bin" and os.name == "nt":
            raw = _dpapi(raw)
        fd, temporary = tempfile.mkstemp(prefix=".write-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / name)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def host_id(self):
        state = self.read("host.json")
        if not state:
            state = {"ext_agent_host_id": f"urn:uuid:{uuid.uuid4()}"}
            self.write("host.json", state)
        return state["ext_agent_host_id"]

    @contextmanager
    def exclusive(self):
        # Windows may reject reading an already locked byte before locking().
        handle = None
        try:
            handle = (self.root / ".lock").open("a+b")
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if handle is not None:
                handle.close()
            raise DomainError("CONFLICT", "SIWC state is owned by another process; stop it before login/logout.") from None
        try:
            yield
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()


def callback_result(target, state, expected_client=None):
    parsed = urlsplit(target)
    if parsed.path != "/auth/callback" or len(target) > 16384:
        raise SiwcError("Invalid OAuth callback path.")
    fields = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(values) != 1 for values in fields.values()):
        raise SiwcError("Duplicate OAuth callback parameters.")
    received = fields.get("state", [""])[0]
    if not hmac.compare_digest(received, state):
        raise SiwcError("OAuth callback state mismatch.")
    if "error" in fields:
        return {"error": safe_label(fields["error"][0])}
    client_id = fields.get("client_id", [expected_client])[0]
    if (not isinstance(client_id, str) or client_id == BOOTSTRAP_CLIENT or
            safe_label(client_id) == "unknown" or (expected_client and client_id != expected_client)):
        raise SiwcError("OAuth callback did not return the expected issued client ID.")
    code = fields.get("code", [""])[0]
    if not code:
        raise SiwcError("OAuth callback is missing an authorization code.")
    return {"code": code, "client_id": client_id}


def validate_identity(encoded, jwks, client_id, nonce, expected_subject=None):
    try:
        header = jwt.get_unverified_header(encoded)
        algorithm = header.get("alg")
        if algorithm not in {"RS256", "ES256"}:
            raise ValueError
        matches = [key for key in jwks["keys"] if key.get("kid") == header.get("kid")]
        if len(matches) != 1:
            raise ValueError
        key = matches[0]
        if key.get("use", "sig") != "sig" or key.get("alg", algorithm) != algorithm:
            raise ValueError
        claims = jwt.decode(encoded, jwt.PyJWK.from_dict(key).key, algorithms=[algorithm],
                            audience=client_id, issuer=ISSUER, leeway=15,
                            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
        if (not isinstance(claims["sub"], str) or not claims["sub"] or
                not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"], nonce) or
                claims.get("azp", client_id) != client_id or
                (isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id) or
                (expected_subject and claims["sub"] != expected_subject)):
            raise ValueError
        return claims
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
        raise SiwcError("OpenAI ID-token signature, identity, or nonce validation failed.") from None


def token_record(data, registration, previous=None, *, clock=time.time):
    previous = previous or {}
    try:
        access = data["access_token"]
        expires = data["expires_in"]
        scope = data.get("scope", " ".join(previous.get("scopes", ())))
        if (not isinstance(access, str) or not access or type(expires) is not int or expires <= 0 or
                data.get("token_type", "").lower() != "bearer" or not isinstance(scope, str)):
            raise ValueError
        result = {**previous, "client_id": registration["client_id"], "subject": registration["subject"],
                  "access_token": access, "expires_at": clock() + expires, "scopes": scope.split()}
        for field in ("refresh_token", "id_token", "earliest_refresh_at"):
            if field in data:
                result[field] = data[field]
        return result
    except (ValueError, KeyError, AttributeError):
        raise SiwcError("Invalid OAuth token response.") from None


@dataclass(frozen=True)
class AccessCredential:
    bearer: str = field(repr=False)
    binding: str


@dataclass(frozen=True)
class LoginAttempt:
    url: str = field(repr=False)
    redirect_uri: str
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    client_id: str | None
    expires_at: float


_TERMINAL_REFRESH = frozenset({"invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"})


class SiwcSession:
    """One protected registration, explicitly owned by an application or auth CLI."""
    def __init__(self, state_dir=DEFAULT_STATE, *, client=None, clock=time.time, store=None, exclusive=True):
        self.store = store if store is not None else _Store(state_dir)
        self._clock = clock
        self._lock = RLock()
        self._closed = self._closing = self._unusable = False
        self._pending = None
        self._discovery = None
        self._ownership = self.store.exclusive() if exclusive else None
        self._owns_client = client is None
        self.client = client if client is not None else httpx.Client(timeout=httpx.Timeout(15, connect=10), follow_redirects=False)
        try:
            if self._ownership is not None:
                self._ownership.__enter__()
        except BaseException:
            if self._owns_client:
                self.client.close()
            raise

    def _check(self, context, *, write=True):
        context.CheckCancelled()
        if self._closed or self._closing:
            raise DomainError("NOT_READY", "SIWC session is closed")
        if write and self._ownership is None:
            raise DomainError("CONFLICT", "Read-only SIWC status session")
        if self._unusable:
            raise SiwcError("SIWC credentials could not be saved; sign in again.", "AUTH_STORAGE")

    def _save(self, name, value):
        try:
            self.store.write(name, value)
        except (OSError, SiwcError):
            self._unusable = True
            raise SiwcError("Cannot save protected SIWC state; sign in again.", "AUTH_STORAGE") from None

    def _request(self, context, method, url, *, commit_credentials=False, **kwargs):
        self._check(context)
        try:
            response = self.client.request(method, url, timeout=min(15, context.Remaining()), **kwargs)
        except httpx.TimeoutException:
            context.CheckCancelled()
            raise ModelError("TIMEOUT") from None
        except httpx.TransportError:
            context.CheckCancelled()
            raise ModelError("TRANSPORT") from None
        # A rotating token response must be durably saved even if cancellation
        # arrived while the server processed it. Check control after that commit.
        if not commit_credentials:
            context.CheckCancelled()
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code != 200:
            error = data.get("error") if isinstance(data, dict) else None
            code = error.get("code") or error.get("type") if isinstance(error, dict) else error
            if isinstance(code, str) and code in _TERMINAL_REFRESH:
                raise ModelError("AUTH_REQUIRED", remote_code=code, http_status=response.status_code)
            raise RemoteError(response.status_code, code, response.headers.get("x-request-id"), siwc=True)
        if not isinstance(data, dict):
            raise ModelError("PROTOCOL")
        return data

    def _metadata(self, context):
        if self._discovery is None:
            data = self._request(context, "GET", ISSUER + "/.well-known/openid-configuration")
            if data.get("issuer") != ISSUER:
                raise ModelError("PROTOCOL")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "revocation_endpoint"):
                endpoint = urlsplit(data.get(key, ""))
                if endpoint.scheme != "https" or endpoint.netloc != "auth.openai.com" or endpoint.query or endpoint.fragment:
                    raise ModelError("PROTOCOL")
            self._discovery = data
        return self._discovery

    def _record(self):
        record = self.store.read("credentials.bin")
        registration = self.store.read("registration.json")
        if (not record or any(record.get(k) != registration.get(k) for k in ("client_id", "subject")) or
                any(not isinstance(record.get(k), str) or not record[k] for k in ("client_id", "subject", "access_token")) or
                record["client_id"] == BOOTSTRAP_CLIENT or record.get("issuer", ISSUER) != ISSUER or
                registration.get("issuer", ISSUER) != ISSUER):
            raise SiwcError("No usable SIWC credentials; run funagent auth login.")
        expiry = record.get("expires_at")
        scopes = record.get("scopes")
        if type(expiry) not in (int, float) or not isfinite(expiry) or not isinstance(scopes, list) or any(
                not isinstance(scope, str) for scope in scopes):
            raise SiwcError("Invalid protected SIWC credential record.", "AUTH_STORAGE")
        return record, registration

    @staticmethod
    def _permission(record):
        if not {PLAN_SCOPE, "resource.invoke"} <= set(record.get("scopes", ())):
            raise SiwcError("Signed in, but ChatGPT plan usage permission was not granted.", "PLAN_PERMISSION_REQUIRED")

    @staticmethod
    def _credential(record):
        binding = hashlib.sha256(json.dumps([ISSUER, record["subject"], record["client_id"]],
                                           separators=(",", ":")).encode()).hexdigest()
        return AccessCredential(record["access_token"], binding)

    def GetAccess(self, context):
        with controlled_lock(self._lock, context):
            self._check(context)
            record, registration = self._record()
            self._permission(record)
            now = self._clock()
            earliest = record.get("earliest_refresh_at", 0)
            if type(earliest) not in (int, float) or not isfinite(earliest):
                raise ModelError("PROTOCOL")
            if record["expires_at"] > now + 60 or (record["expires_at"] > now and now < earliest):
                return self._credential(record)
            if now < earliest:
                raise ModelError("TIMEOUT")
            if not isinstance(record.get("refresh_token"), str) or not record["refresh_token"]:
                raise SiwcError("SIWC session cannot renew; run funagent auth login.")
            try:
                data = self._request(context, "POST", self._metadata(context)["token_endpoint"], commit_credentials=True, data={
                    "grant_type": "refresh_token", "client_id": record["client_id"],
                    "refresh_token": record["refresh_token"], "resource": RESOURCE})
            except ModelError as error:
                if error.remote_code in _TERMINAL_REFRESH:
                    try:
                        (self.store.root / "credentials.bin").unlink(missing_ok=True)
                    except OSError:
                        raise SiwcError("Cannot clear unusable SIWC credentials.", "AUTH_STORAGE") from None
                raise
            if not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]:
                self._unusable = True
                raise SiwcError("Refresh returned no replacement refresh token; sign in again.")
            record = token_record(data, registration, record, clock=self._clock)
            self._save("credentials.bin", record)
            self._permission(record)
            context.CheckCancelled()
            return self._credential(record)

    def BeginLogin(self, redirect_uri, context):
        with controlled_lock(self._lock, context):
            self._check(context)
            uri = urlsplit(redirect_uri)
            if uri.scheme != "http" or uri.hostname != "127.0.0.1" or uri.path != "/auth/callback" or not uri.port or uri.query or uri.fragment:
                raise DomainError("INVALID_REQUEST", "Invalid SIWC loopback callback")
            registration = self.store.read("registration.json")
            previous = self.store.read('credentials.bin') if registration.get('client_id') else {}
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            parameters = {"client_id": registration.get("client_id") or BOOTSTRAP_CLIENT,
                "ext_agent_host_id": self.store.host_id(), "response_type": "code", "redirect_uri": redirect_uri,
                "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce,
                "code_challenge_method": "S256", "code_challenge": challenge}
            if not registration.get("client_id"):
                parameters["agent_name_hint"] = "FunAgent"
            elif previous and not {PLAN_SCOPE, 'resource.invoke'} <= set(previous.get('scopes', ())):
                # This is an explicit login command after declined plan usage,
                # not an automatic consent loop during model inference.
                parameters['prompt'] = 'consent'
            url = self._metadata(context)["authorization_endpoint"] + "?" + urlencode(parameters)
            self._pending = LoginAttempt(url, redirect_uri, state, nonce, verifier, registration.get("client_id"), self._clock() + min(600, context.Remaining()))
            return self._pending

    def CompleteLogin(self, attempt, target, context):
        with controlled_lock(self._lock, context):
            self._check(context)
            if attempt is not self._pending or self._clock() >= attempt.expires_at:
                raise SiwcError("SIWC login attempt expired or already used.")
            self._pending = None
            outcome = callback_result(target, attempt.state, attempt.client_id)
            if "error" in outcome:
                raise SiwcError("ChatGPT authorization was declined.", "PLAN_PERMISSION_REQUIRED")
            registration = {**self.store.read("registration.json"), "client_id": outcome["client_id"]}
            self._save("registration.json", registration)
            metadata = self._metadata(context)
            data = self._request(context, "POST", metadata["token_endpoint"], commit_credentials=True, data={
                "grant_type": "authorization_code", "client_id": outcome["client_id"], "code": outcome["code"],
                "code_verifier": attempt.verifier, "redirect_uri": attempt.redirect_uri, "resource": RESOURCE})
            jwks = self._request(context, "GET", metadata["jwks_uri"])
            identity = validate_identity(data.get("id_token"), jwks, outcome["client_id"], attempt.nonce,
                                         registration.get("subject"))
            registration.update(subject=identity["sub"], issuer=ISSUER)
            record = token_record(data, registration, clock=self._clock)
            record["issuer"] = ISSUER
            self._save("registration.json", registration)
            self._save("credentials.bin", record)
            self._permission(record)
            context.CheckCancelled()
            return self._credential(record)

    def Status(self):
        with self._lock:
            self._check(RunContext(), write=False)
            record = self.store.read("credentials.bin")
            registration = self.store.read("registration.json")
            if record:
                self._record()
            return {"state_dir": str(self.store.root), "registered": bool(registration.get("client_id")),
                    "signed_in": bool(record), "plan_permission": {PLAN_SCOPE, "resource.invoke"} <= set(record.get("scopes", ())),
                    "expires_at": record.get("expires_at"), "expired": record.get("expires_at", 0) <= self._clock(),
                    "renewable": bool(record.get("refresh_token")), "remote_verified": False}

    def ListModels(self, context):
        credential = self.GetAccess(context)
        with controlled_lock(self._lock, context):
            data = self._request(context, "GET", RESOURCE + "/models", headers={"Authorization": "Bearer " + credential.bearer})
            models = data.get("models")
            if not isinstance(models, list):
                raise ModelError("PROTOCOL")
            return [{"slug": m["slug"], "display_name": m.get("display_name")} for m in models if isinstance(m, dict)
                    and m.get("visibility") == "list" and isinstance(m.get("slug"), str) and m["slug"]]

    def Logout(self, context):
        with controlled_lock(self._lock, context):
            self._check(context)
            record = self.store.read("credentials.bin")
            confirmed = not bool(record.get("refresh_token"))
            if not confirmed and isinstance(record.get('client_id'), str) and record['client_id']:
                try:
                    response = self.client.post(self._metadata(context)["revocation_endpoint"], data={
                        "token": record["refresh_token"], "token_type_hint": "refresh_token", "client_id": record["client_id"]},
                        timeout=min(15, context.Remaining()))
                    confirmed = response.status_code == 200
                except (httpx.HTTPError, ModelError):
                    confirmed = False
            try:
                (self.store.root / "credentials.bin").unlink(missing_ok=True)
            except OSError:
                raise SiwcError("Cannot clear local SIWC tokens.", "AUTH_STORAGE") from None
            return {"signed_out": True, "remote_revocation_confirmed": confirmed}

    def Close(self):
        self._closing = True
        with self._lock:
            if self._closed:
                return
            try:
                if self._owns_client:
                    self.client.close()
                if self._ownership is not None:
                    self._ownership.__exit__(None, None, None)
            except Exception:
                raise DomainError("CLOSE_FAILED", "SIWC resource close failed") from None
            self._pending = None
            self._closed = True


