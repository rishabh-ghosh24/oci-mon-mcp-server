import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

from oci_mon_mcp.audit import AuditLogger, AuditEntry


class AuditLoggerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        self.archive_path = Path(self.tmpdir) / "archive"
        self.logger = AuditLogger(
            log_path=self.log_path,
            archive_dir=self.archive_path,
            max_bytes=1024,
            backup_count=2,
            retention_days=90,
        )

    def test_write_audit_entry(self):
        entry = AuditEntry(
            profile_id="pilot_alice_codex",
            user_id="alice",
            query_text="show me cpu utilization",
            resolved_intent="threshold",
            namespace="oci_computeagent",
        )
        self.logger.log(entry)
        self.assertTrue(self.log_path.exists())
        with open(self.log_path) as f:
            line = f.readline()
            record = json.loads(line)
        self.assertEqual(record["profile_id"], "pilot_alice_codex")
        self.assertEqual(record["query_text"], "show me cpu utilization")
        self.assertIn("timestamp", record)

    def test_timing_breakdown_included(self):
        entry = AuditEntry(
            profile_id="pilot_bob_claude",
            user_id="bob",
            query_text="top 5 by memory",
            resolved_intent="top_n",
            namespace="oci_computeagent",
            timing={
                "total_ms": 9200,
                "breakdown": {
                    "query_parsing_ms": 15,
                    "oci_api_calls": [
                        {"api": "SummarizeMetricsData", "duration_ms": 4800},
                    ],
                    "chart_generation_ms": 280,
                },
            },
        )
        self.logger.log(entry)
        with open(self.log_path) as f:
            record = json.loads(f.readline())
        self.assertEqual(record["timing"]["total_ms"], 9200)
        self.assertEqual(record["timing"]["breakdown"]["query_parsing_ms"], 15)

    def test_sensitive_data_masked(self):
        entry = AuditEntry(
            profile_id="pilot_alice_codex",
            user_id="alice",
            query_text="show cpu in ocid1.compartment.oc1..aaaaexample",
            resolved_intent="threshold",
            namespace="oci_computeagent",
            mql_queries=["CpuUtilization[5m]{compartmentId = \"ocid1.compartment.oc1..aaaaexample\"}"],
        )
        self.logger.log(entry)
        with open(self.log_path) as f:
            record = json.loads(f.readline())
        self.assertNotIn("ocid1.compartment.oc1..aaaaexample", record["query_text"])
        self.assertIn("<OCID>", record["query_text"])

    def test_cleanup_archives_removes_old_files(self):
        self.archive_path.mkdir(parents=True, exist_ok=True)
        old_file = self.archive_path / "audit.log.1.gz"
        old_file.write_bytes(b"old data")
        old_mtime = time.time() - (200 * 86400)
        os.utime(old_file, (old_mtime, old_mtime))
        recent_file = self.archive_path / "audit.log.2.gz"
        recent_file.write_bytes(b"recent data")
        recent_mtime = time.time() - (10 * 86400)
        os.utime(recent_file, (recent_mtime, recent_mtime))

        removed = self.logger.cleanup_archives()
        self.assertEqual(removed, 1)
        self.assertFalse(old_file.exists())
        self.assertTrue(recent_file.exists())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
