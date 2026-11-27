"""Generate a disposable, least-privilege M1 fixture; never edit the baseline."""
from copy import deepcopy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURE_NAME = "integration-lab-realm.json"
OUTPUT = ROOT.parent / ".keycloak-m1-role-reduction"


def reduced_realm(baseline: dict) -> dict:
    fixture = deepcopy(baseline)
    if fixture.get("realm") != "integration-lab":
        raise ValueError("expected the synthetic integration-lab realm")
    users = [user for user in fixture.get("users", ()) if user.get("username") == "importer"]
    if len(users) != 1 or users[0].get("realmRoles") != ["cases_viewer", "cases_importer"]:
        raise ValueError("the synthetic importer no longer matches the exercise baseline")
    users[0]["realmRoles"] = ["cases_viewer"]
    return fixture


def main() -> None:
    baseline = json.loads((ROOT / FIXTURE_NAME).read_text(encoding="utf-8"))
    try:
        fixture = reduced_realm(baseline)
        OUTPUT.mkdir(mode=0o700)
    except FileExistsError:
        raise SystemExit("Fixture directory already exists; no files were changed.") from None
    except ValueError as error:
        raise SystemExit(f"Fixture not prepared: {error}.") from None
    # This file contains only the existing fictional M1 credentials.
    # The read-only container mount must be readable under the Keycloak UID.
    with (OUTPUT / FIXTURE_NAME).open("x", encoding="utf-8") as handle:
        json.dump(fixture, handle, indent=2)
        handle.write("\n")
    (OUTPUT / FIXTURE_NAME).chmod(0o644)
    print(f"Prepared a disposable viewer-only importer fixture in {OUTPUT.name}/.")


if __name__ == "__main__":
    main()
