"""Read-only, loopback resource server for a synthetic paginated API."""
import os
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx
import jwt
from starlette.middleware.trustedhost import TrustedHostMiddleware

from service_profile import ISSUER, JWKS_ENDPOINT, READ_SCOPE, SNAPSHOT, SOURCE_AUDIENCE, SOURCE_PATH, WORKER_CLIENT

FAIL_PAGE_TWO = os.environ.get("LAB_M3_FAIL_PAGE_TWO") == "1"

PAGES = {
    None: (1, "after-002"),
    "after-002": (3, "after-004"),
    "after-004": (5, None),
}


class LocalKeys(jwt.PyJWKClient):
    def fetch_data(self):
        # This endpoint is fixed. Do not use token-provided key URLs, redirects or the OS proxy.
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                response = client.get(self.uri)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise jwt.PyJWKClientConnectionError("Local identity service unavailable") from None


keys = LocalKeys(JWKS_ENDPOINT, timeout=4, lifespan=60)
bearer = HTTPBearer(auto_error=False)
source = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
source.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost"])


@source.middleware("http")
async def no_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def unauthorized():
    return HTTPException(401, "Valid service access token required", headers={"WWW-Authenticate": "Bearer"})


def require_source_read(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    if not credentials or len(credentials.credentials) > 8192:
        raise unauthorized()
    token = credentials.credentials
    try:
        header = jwt.get_unverified_header(token)
        key_id = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(key_id, str) or not 0 < len(key_id) <= 128:
            raise unauthorized()
        key = keys.get_signing_key(key_id)
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=SOURCE_AUDIENCE, issuer=ISSUER,
            options={"require": ["exp", "iss", "aud", "sub", "typ", "azp"], "strict_aud": True},
        )
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(503, "Identity service unavailable") from None
    except (jwt.PyJWTError, ValueError):
        raise unauthorized() from None
    if claims["typ"] != "Bearer" or claims["azp"] != WORKER_CLIENT or not claims["sub"]:
        raise unauthorized()
    scope = claims.get("scope")
    if not isinstance(scope, str) or READ_SCOPE not in scope.split(" "):
        raise HTTPException(403, "Source read scope required", headers={
            "WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{READ_SCOPE}"'
        })


@source.get(SOURCE_PATH, dependencies=[Depends(require_source_read)])
def read_cases(cursor: str | None = None):
    if cursor not in PAGES:
        raise HTTPException(400, "Unknown fixture cursor")
    if FAIL_PAGE_TWO and cursor == "after-002":
        raise HTTPException(503, "Simulated temporary outage on page two")
    start, following = PAGES[cursor]
    return {
        "snapshot": SNAPSHOT,
        "records": [{"id": f"SOURCE-{n:03d}", "title": f"Fictional source case {n}"}
                    for n in range(start, start + 2)],
        "next_cursor": following,
    }
