"""Local-only OIDC exercise. Not a production deployment."""
from dataclasses import dataclass, field
from html import escape
import re
import secrets
import sqlite3
import time
from typing import Annotated

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx
import jwt
from joserfc.errors import JoseError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from import_job import REQUEST_KEY, RUN_ID
import staff_requests
from worker import LabError

ISSUER = "http://localhost:8180/realms/integration-lab"
ORIGIN = "http://localhost:8000"
AUDIENCE = "cases-api"
CLIENT = "lab-web"
COOKIE = "integration_lab_session"
CASES = [{"id": "CASE-001", "title": "Synthetic case"}]

oauth = OAuth()
oidc = oauth.register(
    name="keycloak",
    client_id=CLIENT,
    server_metadata_url=f"{ISSUER}/.well-known/openid-configuration",
    client_kwargs={"scope": "openid", "code_challenge_method": "S256", "timeout": 5, "trust_env": False},
)
jwks = jwt.PyJWKClient(f"{ISSUER}/protocol/openid-connect/certs", timeout=5)
bearer = HTTPBearer(auto_error=False)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost"])


@dataclass
class Session:
    deadline: float
    data: dict = field(default_factory=dict)


sessions: dict[str, Session] = {}


def start_session(request: Request, ttl: int) -> str:
    now = time.monotonic()
    for key, session in list(sessions.items()):
        if session.deadline <= now:
            del sessions[key]
    if len(sessions) >= 128:
        raise HTTPException(503, "Local session capacity reached")
    key = secrets.token_urlsafe(32)
    sessions[key] = Session(now + ttl)
    request.scope["session"] = sessions[key].data
    return key


def set_session(response: Response, key: str, ttl: int) -> Response:
    # Local plain HTTP only. Production would require HTTPS and Secure cookies.
    response.set_cookie(COOKIE, key, httponly=True, samesite="lax", max_age=ttl)
    return response


def drop_session(request: Request) -> None:
    if key := request.cookies.get(COOKIE):
        sessions.pop(key, None)
    request.scope["session"] = {}


@app.middleware("http")
async def session_context(request: Request, call_next):
    session = sessions.get(request.cookies.get(COOKIE, ""))
    request.scope["session"] = session.data if session and session.deadline > time.monotonic() else {}
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def denied() -> HTTPException:
    return HTTPException(401, "Valid API access token required", headers={"WWW-Authenticate": "Bearer"})


@dataclass(frozen=True)
class Staff:
    subject: str
    roles: frozenset[str]


def permissions(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Staff:
    if not credentials or len(credentials.credentials) > 8192:
        raise denied()
    token = credentials.credentials
    try:
        # Only trust keys from the configured issuer and only the configured algorithm.
        if jwt.get_unverified_header(token).get("alg") != "RS256":
            raise denied()
        key = jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER,
            options={"require": ["exp", "iss", "aud", "sub", "typ"]},
        )
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(503, "Identity service unavailable") from None
    except jwt.PyJWTError:
        raise denied() from None
    subject = claims.get("sub")
    if (claims.get("typ") != "Bearer" or claims.get("azp") != CLIENT
            or not isinstance(subject, str) or not 0 < len(subject) <= 256):
        raise denied()
    realm = claims.get("realm_access")
    roles = realm.get("roles") if isinstance(realm, dict) else None
    role_set = frozenset(r for r in roles if isinstance(r, str)) if isinstance(roles, list) else frozenset()
    return Staff(subject, role_set)


@app.get("/api/cases")
def list_cases(staff: Annotated[Staff, Depends(permissions)]):
    if not staff.roles.intersection({"cases_viewer", "cases_importer"}):
        raise HTTPException(403, "Case access required")
    return CASES


def importer(staff: Annotated[Staff, Depends(permissions)]) -> Staff:
    if "cases_importer" not in staff.roles:
        raise HTTPException(403, "Importer role required")
    return staff


def api_result(result: dict, *, status_code: int = 200) -> JSONResponse:
    url = "/api/imports/" + result["run_id"]
    return JSONResponse({**result, "status_url": url}, status_code=status_code, headers={"Location": url})


@app.post("/api/imports", status_code=202)
async def request_import(request: Request, staff: Annotated[Staff, Depends(importer)]):
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1 or not REQUEST_KEY.fullmatch(keys[0]):
        raise HTTPException(400, "Provide one Idempotency-Key (16 to 64 URL-safe characters)")
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(400, "Staff import requests must not contain a body")
    try:
        result, created = await run_in_threadpool(staff_requests.accept, staff.subject, keys[0])
    except LabError as error:
        if error.code == "queue_full":
            raise HTTPException(409, "Local import queue is full") from None
        raise HTTPException(503, "Local import store unavailable") from None
    except (OSError, sqlite3.Error):
        raise HTTPException(503, "Local import store unavailable") from None
    return api_result(result, status_code=202 if created else 200)


@app.get("/api/imports/{run_id}")
def import_status(run_id: str, staff: Annotated[Staff, Depends(importer)]):
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(404, "Import request not found")
    try:
        result = staff_requests.status(staff.subject, run_id)
    except LabError as error:
        if error.code in {"unknown_store", "unknown_request"}:
            raise HTTPException(404, "Import request not found") from None
        raise HTTPException(503, "Local import store unavailable") from None
    except (OSError, sqlite3.Error):
        raise HTTPException(503, "Local import store unavailable") from None
    return api_result(result)


@app.get("/login")
async def login(request: Request):
    drop_session(request)
    ttl = 180
    key = start_session(request, ttl)
    try:
        response = await oidc.authorize_redirect(request, f"{ORIGIN}/auth/callback")
    except httpx.HTTPError:
        sessions.pop(key, None)
        raise HTTPException(503, "Identity service unavailable") from None
    return set_session(response, key, ttl)


@app.get("/auth/callback")
async def callback(request: Request):
    try:
        token = await oidc.authorize_access_token(
            request, claims_options={"iss": {"essential": True, "value": ISSUER}}, leeway=0,
        )
    except (OAuthError, JoseError, httpx.HTTPError, ValueError):
        drop_session(request)
        raise HTTPException(400, "Sign-in could not be verified. Start again at /login.") from None
    if not token.get("userinfo") or not isinstance(token.get("access_token"), str):
        drop_session(request)
        raise HTTPException(400, "Identity service returned an incomplete login")
    expires_in = token.get("expires_in")
    ttl = min(expires_in, 300) if type(expires_in) is int else 0
    if ttl <= 0:
        drop_session(request)
        raise HTTPException(400, "Identity service returned an expired login")
    drop_session(request)
    key = start_session(request, ttl)
    request.session.update(access_token=token["access_token"], csrf=secrets.token_urlsafe(32))
    return set_session(RedirectResponse("/", status_code=303), key, ttl)


async def call_api(request: Request, method: str, path: str, *, key: str | None = None) -> httpx.Response:
    if not (token := request.session.get("access_token")):
        raise HTTPException(401, "Sign in first")
    # Exercise the same public API boundary without sending the token over a socket.
    headers = {"Authorization": "Bearer " + token}
    if key is not None:
        headers["Idempotency-Key"] = key
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        return await client.request(method, path, headers=headers)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if "access_token" not in request.session:
        return '<h1>Enterprise Integration Lab</h1><p><a href="/login">Sign in</a></p>'
    result = await call_api(request, "GET", "/api/cases")
    csrf = escape(request.session["csrf"], quote=True)
    data = escape(result.text)
    request_key = secrets.token_hex(16)
    return f'''<h1>Enterprise Integration Lab</h1>
<p>List API: HTTP {result.status_code}</p><pre>{data}</pre>
<form method="post" action="/import"><button>Request import</button><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="key" value="{request_key}"></form>
<form method="post" action="/logout"><button>End app session</button><input type="hidden" name="csrf" value="{csrf}"></form>'''


async def check_csrf(request: Request, *, import_form: bool = False) -> str | None:
    if request.headers.get("origin") != ORIGIN:
        raise HTTPException(403, "Same-origin form required")
    if request.headers.get("content-type") != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Form required")
    expected = request.session.get("csrf")
    supplied = bytearray()
    async for chunk in request.stream():
        supplied.extend(chunk)
        if len(supplied) > 128:
            raise HTTPException(413, "Form too large")
    csrf, marker, key = supplied.partition(b"&key=")
    if not expected or not secrets.compare_digest(csrf, ("csrf=" + expected).encode("ascii")):
        raise HTTPException(403, "CSRF verification failed")
    if import_form:
        if not marker or not re.fullmatch(rb"[a-f0-9]{32}", key):
            raise HTTPException(400, "Invalid import form key")
        return key.decode("ascii")
    if marker:
        raise HTTPException(400, "Invalid logout form")


def api_error(result: httpx.Response) -> JSONResponse:
    return JSONResponse(result.json(), status_code=result.status_code, headers={
        key: value for key, value in result.headers.items() if key == "www-authenticate"
    })


@app.post("/import")
async def browser_import(request: Request):
    key = await check_csrf(request, import_form=True)
    result = await call_api(request, "POST", "/api/imports", key=key)
    if result.status_code not in {200, 202}:
        return api_error(result)
    return RedirectResponse("/import/status/" + result.json()["run_id"], status_code=303)


@app.get("/import/status/{run_id}", response_class=HTMLResponse)
async def browser_import_status(request: Request, run_id: str):
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(404, "Import request not found")
    result = await call_api(request, "GET", "/api/imports/" + run_id)
    if result.status_code != 200:
        return api_error(result)
    return "<h1>Import status</h1><pre>" + escape(result.text) + '</pre><p><a href="/">Home</a></p>'


@app.post("/logout")
async def logout(request: Request):
    await check_csrf(request)
    drop_session(request)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE)
    return response
