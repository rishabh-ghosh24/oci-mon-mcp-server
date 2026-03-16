"""Local artifact generation and tokenized HTTP serving."""

from __future__ import annotations

import csv
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .errors import DependencyMissingError
from .models import ArtifactLink, ChartBlock


@dataclass(slots=True)
class StoredArtifact:
    """In-memory metadata for a generated artifact."""

    artifact_id: str
    token: str
    path: Path
    content_type: str
    expires_at: datetime


class ArtifactManager:
    """Generate PNG/CSV artifacts and serve them with short-lived token URLs."""

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        host: str = "0.0.0.0",
        port: int = 8765,
        base_url: str | None = None,
        token_ttl_minutes: int = 15,
        auto_start: bool = True,
    ) -> None:
        self.base_dir = base_dir or (Path(__file__).resolve().parents[2] / "data" / "artifacts")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.port = port
        self.base_url = (base_url or f"http://127.0.0.1:{port}").rstrip("/")
        self.token_ttl = timedelta(minutes=token_ttl_minutes)
        self.auto_start = auto_start
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._artifacts: dict[str, StoredArtifact] = {}
        self._lock = threading.Lock()

    def generate_csv(self, *, rows: list[dict[str, Any]], title: str) -> ArtifactLink | None:
        """Write a CSV artifact and return a tokenized link."""
        if not rows:
            return None
        fieldnames = list(rows[0].keys())
        artifact_id = uuid4().hex
        path = self.base_dir / f"{artifact_id}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return self._register_artifact(path=path, content_type="text/csv", title=title)

    def generate_chart_png(self, *, chart: ChartBlock) -> ArtifactLink | None:
        """Render a line chart PNG artifact from structured chart data."""
        if not chart.series:
            return None
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise DependencyMissingError(
                "matplotlib is required to generate PNG chart artifacts."
            ) from exc

        artifact_id = uuid4().hex
        path = self.base_dir / f"{artifact_id}.png"
        fig, ax = plt.subplots(figsize=(12, 6))
        try:
            for series in chart.series:
                x_values = [point.time for point in series.points]
                y_values = [point.value for point in series.points]
                ax.plot(x_values, y_values, linewidth=1.5, label=series.name)
            if chart.threshold_line is not None:
                ax.axhline(
                    y=chart.threshold_line.value,
                    color=chart.threshold_line.color,
                    linewidth=chart.threshold_line.line_width,
                    linestyle="--",
                    label="Threshold",
                )
            ax.set_title(chart.title)
            ax.set_xlabel(chart.x_axis.replace("_", " ").title())
            ax.set_ylabel(chart.y_axis.replace("_", " ").title())
            locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
            formatter = mdates.ConciseDateFormatter(locator)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(formatter)
            ax.tick_params(axis="x", labelrotation=30)
            ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5))
            ax.grid(True, linestyle=":", linewidth=0.5)
            fig.tight_layout()
            fig.savefig(path, dpi=150, bbox_inches="tight")
        finally:
            plt.close(fig)
        return self._register_artifact(path=path, content_type="image/png", title=chart.title)

    def _register_artifact(self, *, path: Path, content_type: str, title: str) -> ArtifactLink:
        if self.auto_start:
            self.ensure_started()
        artifact_id = path.stem
        token = secrets.token_urlsafe(24)
        expires_at = datetime.now(timezone.utc) + self.token_ttl
        with self._lock:
            self._cleanup_expired_locked()
            self._artifacts[artifact_id] = StoredArtifact(
                artifact_id=artifact_id,
                token=token,
                path=path,
                content_type=content_type,
                expires_at=expires_at,
            )
        return ArtifactLink(
            id=artifact_id,
            type=content_type,
            title=title,
            url=f"{self.base_url}/artifacts/{artifact_id}?token={token}",
            expires_at=expires_at.replace(microsecond=0).isoformat(),
        )

    def ensure_started(self) -> None:
        """Start the artifact HTTP server if it is not already running."""
        if self._server is not None:
            return
        manager = self

        class ArtifactHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if not parsed.path.startswith("/artifacts/"):
                    self.send_error(404, "Not found")
                    return
                artifact_id = parsed.path.rsplit("/", 1)[-1]
                token = parse_qs(parsed.query).get("token", [""])[0]
                stored = manager._get_artifact(artifact_id, token)
                if stored is None:
                    self.send_error(404, "Artifact not found or token expired")
                    return
                if not stored.path.exists():
                    self.send_error(404, "Artifact file not found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", stored.content_type)
                self.send_header("Content-Length", str(stored.path.stat().st_size))
                self.end_headers()
                self.wfile.write(stored.path.read_bytes())

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

        self._server = ThreadingHTTPServer((self.host, self.port), ArtifactHandler)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="oci-mon-artifact-server",
            daemon=True,
        )
        self._server_thread.start()

    def shutdown(self) -> None:
        """Stop the artifact server."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._server_thread = None

    def _get_artifact(self, artifact_id: str, token: str) -> StoredArtifact | None:
        with self._lock:
            self._cleanup_expired_locked()
            stored = self._artifacts.get(artifact_id)
            if stored is None or stored.token != token:
                return None
            return stored

    def _cleanup_expired_locked(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            artifact_id
            for artifact_id, stored in self._artifacts.items()
            if stored.expires_at <= now
        ]
        for artifact_id in expired:
            stored = self._artifacts.pop(artifact_id)
            try:
                stored.path.unlink(missing_ok=True)
            except OSError:
                continue

    def __enter__(self) -> ArtifactManager:
        self.ensure_started()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.shutdown()
