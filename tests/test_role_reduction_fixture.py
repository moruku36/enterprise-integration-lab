"""Offline checks for the disposable M1 least-privilege realm fixture."""
import json
import os
from pathlib import Path
from shutil import copyfile
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from keycloak.prepare_m1_role_reduction import FIXTURE_NAME, OUTPUT, ROOT, reduced_realm


class RoleReductionFixture(unittest.TestCase):
    def test_only_the_synthetic_importer_grant_changes(self):
        raw = (ROOT / FIXTURE_NAME).read_text(encoding="utf-8")
        baseline = json.loads(raw)
        variant = reduced_realm(baseline)

        self.assertEqual(baseline, json.loads(raw))
        self.assertEqual(variant["users"][1]["realmRoles"], ["cases_viewer"])
        variant["users"][1]["realmRoles"].append("cases_importer")
        self.assertEqual(variant, baseline)

    def test_refuses_unexpected_or_ambiguous_baseline(self):
        raw = (ROOT / FIXTURE_NAME).read_text(encoding="utf-8")
        for mutation in (
            lambda fixture: fixture.update(realm="unrelated"),
            lambda fixture: fixture["users"].pop(),
            lambda fixture: fixture["users"].append(fixture["users"][1]),
            lambda fixture: fixture["users"][1].update(realmRoles=["cases_importer"]),
        ):
            with self.subTest(mutation=mutation):
                baseline = json.loads(raw)
                mutation(baseline)
                with self.assertRaises(ValueError):
                    reduced_realm(baseline)

    def test_generator_preserves_baseline_and_never_overwrites_a_variant(self):
        with TemporaryDirectory() as directory:
            isolated = Path(directory)
            fixtures = isolated / "keycloak"
            fixtures.mkdir()
            source = fixtures / FIXTURE_NAME
            script = fixtures / "prepare_m1_role_reduction.py"
            copyfile(ROOT / FIXTURE_NAME, source)
            copyfile(ROOT / script.name, script)
            before = source.read_bytes()
            args = [sys.executable, "-B", str(script)]
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            created = subprocess.run(args, cwd=isolated, capture_output=True, text=True, timeout=8, env=env)
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(source.read_bytes(), before)
            output = isolated / OUTPUT.name / FIXTURE_NAME
            variant = output.read_bytes()
            self.assertEqual(json.loads(variant), reduced_realm(json.loads(before)))
            repeated = subprocess.run(args, cwd=isolated, capture_output=True, text=True, timeout=8, env=env)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertIn("no files were changed", repeated.stderr)
            self.assertEqual(output.read_bytes(), variant)
            self.assertEqual(source.read_bytes(), before)
