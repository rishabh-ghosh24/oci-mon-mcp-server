"""Tests for MCP server response shaping."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from oci_mon_mcp.artifacts import ArtifactManager
from oci_mon_mcp.server import SERVICE, _artifact_inline_markdown


class ServerArtifactMarkdownTests(unittest.TestCase):
    """Verify inline markdown generation for chart artifacts."""

    def test_prefers_data_uri_when_artifact_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            original_artifact_manager = SERVICE.artifact_manager
            try:
                artifact_dir = Path(tempdir)
                SERVICE.artifact_manager = ArtifactManager(
                    base_dir=artifact_dir,
                    base_url="http://127.0.0.1:8765",
                    auto_start=False,
                )
                png_path = artifact_dir / "chart123.png"
                png_path.write_bytes(b"png")
                markdown = _artifact_inline_markdown(
                    {
                        "id": "chart123",
                        "type": "image/png",
                        "title": "CPU utilization trend",
                        "url": "http://example.invalid/chart123.png",
                    }
                )
                encoded = base64.b64encode(b"png").decode("ascii")
                self.assertEqual(
                    markdown,
                    f"![CPU utilization trend](data:image/png;base64,{encoded})",
                )
            finally:
                SERVICE.artifact_manager = original_artifact_manager

    def test_falls_back_to_remote_url_when_local_png_missing(self) -> None:
        markdown = _artifact_inline_markdown(
            {
                "id": "missing",
                "type": "image/png",
                "title": "CPU utilization trend",
                "url": "http://example.invalid/missing.png",
            }
        )
        self.assertEqual(
            markdown,
            "![CPU utilization trend](http://example.invalid/missing.png)",
        )


if __name__ == "__main__":
    unittest.main()
