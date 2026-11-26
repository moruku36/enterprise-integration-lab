"""Read the lab's three synthetic source pages. No writes or background jobs."""
from dataclasses import dataclass
import json
import re
import sys

import httpx

from service_profile import READ_SCOPE, SECRET_FILE, SNAPSHOT, SOURCE, SOURCE_PATH, TOKEN_ENDPOINT, WORKER_CLIENT

PAGE_SIZE = 2
PAGE_LIMIT = 3
TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
CURSOR = re.compile(r"[A-Za-z0-9_-]{1,64}")
RECORD_ID = re.compile(r"SOURCE-[0-9]{3}")


class LabError(Exception):
    """A bounded, sanitized failure suitable for the CLI."""

    def __init__(self, message: str, code: str = "invalid_fixture"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourcePage:
    rows: tuple[tuple[str, str], ...]
    next_cursor: str | None


def fixture_secret() -> str:
    try:
        secret = SECRET_FILE.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        raise LabError("worker fixture missing; run python -m keycloak.prepare_m3") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret):
        raise LabError("invalid local worker fixture")
    return secret


def request_token(client: httpx.Client, secret: str, *, scoped: bool = True) -> str:
    data = {"grant_type": "client_credentials"}
    if scoped:
        data["scope"] = READ_SCOPE
    try:
        response = client.post(TOKEN_ENDPOINT, data=data, auth=(WORKER_CLIENT, secret))
    except httpx.HTTPError:
        raise LabError("identity service unavailable", "identity_unavailable") from None
    if response.status_code != 200:
        raise LabError(f"token endpoint returned HTTP {response.status_code}", f"token_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise LabError("invalid token response", "invalid_token_response") from None
    token_type = payload.get("token_type") if isinstance(payload, dict) else None
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise LabError("invalid token response", "invalid_token_response")
    token = payload.get("access_token")
    if not isinstance(token, str) or not 0 < len(token) <= 8192 or not TOKEN.fullmatch(token):
        raise LabError("invalid token response", "invalid_token_response")
    return token


def fetch_page(client: httpx.Client, token: str, cursor: str | None, page: int) -> SourcePage:
    try:
        response = client.get(
            SOURCE + SOURCE_PATH, headers={"Authorization": f"Bearer {token}"},
            params={} if cursor is None else {"cursor": cursor},
        )
    except httpx.HTTPError:
        raise LabError(f"page {page}: source unavailable", "source_unavailable") from None
    if response.status_code != 200:
        raise LabError(f"page {page}: source returned HTTP {response.status_code}",
                       f"source_http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise LabError(f"page {page}: invalid source response", "invalid_page") from None
    if not isinstance(payload, dict):
        raise LabError(f"page {page}: invalid source response", "invalid_page")
    if payload.get("snapshot") != SNAPSHOT:
        raise LabError(f"page {page}: source snapshot changed or is missing", "invalid_snapshot")
    rows = payload.get("records")
    following = payload.get("next_cursor")
    if not isinstance(rows, list) or len(rows) != PAGE_SIZE or "next_cursor" not in payload:
        raise LabError(f"page {page}: invalid source response", "invalid_page")
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            raise LabError(f"page {page}: invalid source record", "invalid_page")
        record_id, title = row.get("id"), row.get("title")
        if (not isinstance(record_id, str) or not RECORD_ID.fullmatch(record_id)
                or not isinstance(title, str) or not 0 < len(title) <= 256):
            raise LabError(f"page {page}: invalid source record", "invalid_page")
        parsed.append((record_id, title))
    if page == PAGE_LIMIT and following is None:
        return SourcePage(tuple(parsed), following)
    if page >= PAGE_LIMIT or not isinstance(following, str) or not CURSOR.fullmatch(following):
        raise LabError(f"page {page}: invalid source cursor", "invalid_cursor")
    return SourcePage(tuple(parsed), following)


def check_progress(page_result: SourcePage, seen_ids, seen_cursors) -> list[str]:
    ids = [record_id for record_id, _ in page_result.rows]
    if len(ids) != len(set(ids)) or set(ids).intersection(seen_ids):
        raise LabError("duplicate source record", "duplicate_record")
    if page_result.next_cursor is not None and page_result.next_cursor in seen_cursors:
        raise LabError("repeated source cursor", "invalid_cursor")
    return ids


def fetch_all(client: httpx.Client, token: str) -> list[dict[str, str]]:
    cursor = None
    seen_ids = set()
    seen_cursors = set()
    records = []
    for page in range(1, PAGE_LIMIT + 1):
        fetched = fetch_page(client, token, cursor, page)
        seen_ids.update(check_progress(fetched, seen_ids, seen_cursors))
        records.extend({"id": record_id, "title": title} for record_id, title in fetched.rows)
        cursor = fetched.next_cursor
        if cursor is None:
            return records
        seen_cursors.add(cursor)
    raise LabError("source page limit reached", "invalid_cursor")


def main() -> int:
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            rows = fetch_all(client, request_token(client, fixture_secret()))
    except LabError as error:
        print(f"Read failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "read_complete", "pages": PAGE_LIMIT, "count": len(rows),
                      "record_ids": [row["id"] for row in rows]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
