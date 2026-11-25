"""Local-only OIDC exercise. Not a production deployment."""
from dataclasses import dataclass, field
from html import escape
import secrets
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
from starlette.middleware.trustedhost import TrustedHostMiddleware

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


def permissions(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> frozenset[str]:
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
    if claims.get("typ") != "Bearer" or claims.get("azp") != CLIENT:
        raise denied()
    realm = claims.get("realm_access")
    roles = realm.get("roles") if isinstance(realm, dict) else None
    return frozenset(r for r in roles if isinstance(r, str)) if isinstance(roles, list) else frozenset()


@app.get("/api/cases")
def list_cases(roles: Annotated[frozenset[str], Depends(permissions)]):
    if not roles.intersection({"cases_viewer", "cases_importer"}):
        raise HTTPException(403, "Case access required")
    return CASES


@app.post("/api/imports", status_code=202)
def request_import(roles: Annotated[frozenset[str], Depends(permissions)]):
    if "cases_importer" not in roles:
        raise HTTPException(403, "Importer role required")
    return {"status": "accepted_stub", "import_started": False}


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


async def call_api(request: Request, method: str, path: str) -> httpx.Response:
    if not (token := request.session.get("access_token")):
        raise HTTPException(401, "Sign in first")
    # Exercise the same public API boundary without sending the token over a socket.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        return await client.request(method, path, headers={"Authorization": "Bearer " + token})


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if "access_token" not in request.session:
        return '<h1>Enterprise Integration Lab</h1><p><a href="/login">Sign in</a></p>'
    result = await call_api(request, "GET", "/api/cases")
    csrf = escape(request.session["csrf"], quote=True)
    data = escape(result.text)
    return f'''<h1>Enterprise Integration Lab</h1>
<p>List API: HTTP {result.status_code}</p><pre>{data}</pre>
<form method="post" action="/import"><button>Try import</button><input type="hidden" name="csrf" value="{csrf}"></form>
<form method="post" action="/logout"><button>End app session</button><input type="hidden" name="csrf" value="{csrf}"></form>'''


async def check_csrf(request: Request):
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
    if not expected or not secrets.compare_digest(supplied, ("csrf=" + expected).encode("ascii")):
        raise HTTPException(403, "CSRF verification failed")


@app.post("/import", dependencies=[Depends(check_csrf)])
async def browser_import(request: Request):
    result = await call_api(request, "POST", "/api/imports")
    return JSONResponse(result.json(), status_code=result.status_code, headers={
        key: value for key, value in result.headers.items() if key == "www-authenticate"
    })


@app.post("/logout", dependencies=[Depends(check_csrf)])
async def logout(request: Request):
    drop_session(request)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE)
    return response
