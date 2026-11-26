"""Fixed loopback settings for the disposable M3 exercise."""
from pathlib import Path

ISSUER = "http://localhost:8180/realms/workload-lab"
TOKEN_ENDPOINT = f"{ISSUER}/protocol/openid-connect/token"
JWKS_ENDPOINT = f"{ISSUER}/protocol/openid-connect/certs"
SOURCE = "http://localhost:8001"
SOURCE_PATH = "/v1/cases"
SOURCE_AUDIENCE = "mock-source-api"
READ_SCOPE = "source.read"
SNAPSHOT = "case-fixture-v1"
WORKER_CLIENT = "import-worker"
FIXTURE_DIR = Path(__file__).resolve().parent / ".keycloak-m3-fixture"
SECRET_FILE = FIXTURE_DIR / "worker-secret"
