"""Offline SQLite checks for atomic page commits and explicit recovery."""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from import_job import SCHEMA, apply_page, import_lease, open_state, read_run, resume_run, start_run
from worker import LabError, SourcePage

PAGE_ONE = SourcePage((("SOURCE-001", "record one"), ("SOURCE-002", "record two")), "after-002")
PAGE_TWO = SourcePage((("SOURCE-003", "record three"), ("SOURCE-004", "record four")), "after-004")


class RecoveryStorage(unittest.TestCase):
    def test_rows_and_checkpoint_both_rollback_when_second_row_write_fails(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "fixture.sqlite3"
            with closing(open_state(path)) as db:
                db.executescript(SCHEMA)
                first = apply_page(db, start_run(db), PAGE_ONE)
                db.executescript("""
                    CREATE TRIGGER reject_second_row BEFORE INSERT ON records
                    WHEN NEW.id = 'SOURCE-004'
                    BEGIN SELECT RAISE(ABORT, 'synthetic DB rejection'); END;
                """)
                with self.assertRaises(sqlite3.IntegrityError):
                    apply_page(db, first, PAGE_TWO)
            with closing(open_state(path, read_only=True)) as fresh:
                self.assertEqual(read_run(fresh, first.run_id), first)
                self.assertEqual([row["id"] for row in fresh.execute("SELECT id FROM records ORDER BY id")],
                                 ["SOURCE-001", "SOURCE-002"])

    def test_stale_checkpoint_cannot_overwrite_records(self):
        with TemporaryDirectory() as directory, closing(open_state(Path(directory) / "fixture.sqlite3")) as db:
            db.executescript(SCHEMA)
            zero = start_run(db)
            first = apply_page(db, zero, PAGE_ONE)
            with self.assertRaisesRegex(LabError, "checkpoint changed"):
                apply_page(db, zero, PAGE_TWO)
            self.assertEqual(read_run(db, zero.run_id), first)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)

    def test_per_store_lease_blocks_second_writer_and_releases(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "lease.sqlite3"
            with import_lease(path):
                with self.assertRaisesRegex(LabError, "another worker"):
                    with import_lease(path):
                        self.fail("concurrent writer was not refused")
            with import_lease(path):
                pass

    def test_nontransient_failure_is_not_resumed(self):
        with TemporaryDirectory() as directory, closing(open_state(Path(directory) / "fixture.sqlite3")) as db:
            db.executescript(SCHEMA)
            first = apply_page(db, start_run(db), PAGE_ONE)
            db.execute("UPDATE import_runs SET status = 'failed', failure = 'invalid_snapshot' WHERE run_id = ?",
                       (first.run_id,))
            with self.assertRaisesRegex(LabError, "not safe to resume"):
                resume_run(db, first.run_id)
            with self.assertRaisesRegex(LabError, "unfinished run"):
                start_run(db)
            self.assertEqual(read_run(db, first.run_id).next_cursor, "after-002")
