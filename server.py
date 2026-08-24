#!/usr/bin/env python3
"""Static demo server plus asynchronous live graph-analysis jobs."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

from catalog import ALGORITHMS, BACKENDS, DATASETS, public_catalog


ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
BACKEND_IDS = [backend["id"] for backend in BACKENDS]
BACKEND_LABELS = {backend["id"]: backend["label"] for backend in BACKENDS}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compute_statistics(rows: list[dict]) -> dict:
    values = sorted(value for row in rows if (value := finite(row.get("value"))) is not None)
    if not values:
        return {"count": 0, "histogram": []}

    def percentile(fraction: float) -> float:
        if len(values) == 1:
            return values[0]
        position = fraction * (len(values) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return values[lower]
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    nonzero = [value for value in values if value > 0]
    histogram = []
    if nonzero:
        low = math.floor(math.log10(min(nonzero)))
        high = math.ceil(math.log10(max(nonzero)))
        if low == high:
            high += 1
        bin_count = min(14, max(6, high - low + 2))
        width = (high - low) / bin_count
        counts = [0] * bin_count
        for value in nonzero:
            index = min(bin_count - 1, int((math.log10(value) - low) / width))
            counts[index] += 1
        histogram = [
            {
                "from": 10 ** (low + index * width),
                "to": 10 ** (low + (index + 1) * width),
                "count": count,
                "percentage": count * 100 / len(values),
            }
            for index, count in enumerate(counts)
        ]

    return {
        "count": len(values),
        "min": values[0],
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "max": values[-1],
        "nonzero": len(nonzero),
        "nonzero_percentage": len(nonzero) * 100 / len(values),
        "p75": percentile(0.75),
        "p90": percentile(0.90),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "histogram": histogram,
    }


def normalize_rows(payload: dict) -> list[dict]:
    raw_rows = payload.get("results") or []
    rows = []
    for row in raw_rows:
        value = finite(row.get("value"))
        if value is None:
            continue
        rows.append(
            {
                "node_id": row.get("node_id"),
                "mapped_id": row.get("mapped_id"),
                "value": value,
            }
        )
    rows.sort(key=lambda row: row["value"], reverse=True)
    total = len(rows)
    for index, row in enumerate(rows):
        row["rank"] = index + 1
        row["percentile"] = (total - index) * 100 / total if total else 0
    return rows


class ProcessMetricsSampler:
    """Measure interval CPU usage on Linux, with a portable ps fallback."""

    def __init__(self, pid: int):
        self.pid = pid
        self.proc_root = Path("/proc") / str(pid)
        self.clock_ticks = float(os.sysconf("SC_CLK_TCK")) if self.proc_root.exists() else None
        self.previous_cpu_seconds: float | None = None
        self.previous_wall_time: float | None = None

    def sample(self) -> tuple[float, float] | None:
        if self.clock_ticks is not None:
            return self._sample_proc()
        return self._sample_ps()

    def _sample_proc(self) -> tuple[float, float] | None:
        stat_text = (self.proc_root / "stat").read_text(encoding="utf-8")
        closing_parenthesis = stat_text.rfind(")")
        fields = stat_text[closing_parenthesis + 2:].split()
        cpu_seconds = (int(fields[11]) + int(fields[12])) / self.clock_ticks
        memory_kb = 0.0
        for line in (self.proc_root / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                memory_kb = float(line.split()[1])
                break

        wall_time = time.monotonic()
        if self.previous_cpu_seconds is None or self.previous_wall_time is None:
            self.previous_cpu_seconds = cpu_seconds
            self.previous_wall_time = wall_time
            return None
        wall_delta = wall_time - self.previous_wall_time
        cpu_delta = cpu_seconds - self.previous_cpu_seconds
        self.previous_cpu_seconds = cpu_seconds
        self.previous_wall_time = wall_time
        if wall_delta <= 0:
            return None
        return max(0.0, cpu_delta / wall_delta * 100.0), memory_kb / 1024.0

    def _sample_ps(self) -> tuple[float, float] | None:
        result = subprocess.run(
            ["ps", "-o", "%cpu=,rss=", "-p", str(self.pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        fields = result.stdout.strip().split()
        if len(fields) < 2:
            return None
        return float(fields[0]), float(fields[1]) / 1024.0


def sample_process(
    sampler: ProcessMetricsSampler,
    started: float,
    elapsed_offset: float,
    backend: str,
    configured_threads: int,
    job: dict,
) -> None:
    try:
        measurement = sampler.sample()
        elapsed = round(elapsed_offset + time.monotonic() - started, 3)
        if measurement is None:
            with JOBS_LOCK:
                job["elapsed_seconds"] = elapsed
            return
        raw_cpu, memory_mb = measurement
        thread_cpu = min(100.0, raw_cpu / max(1, configured_threads))
        host_cpu = raw_cpu / max(1, os.cpu_count() or 1)
        point = {
            "elapsed": elapsed,
            "backend": backend,
            "cpu_percent": round(thread_cpu, 2),
            "host_cpu_percent": round(host_cpu, 2),
            "raw_process_cpu_percent": round(raw_cpu, 2),
            "memory_mb": round(memory_mb, 2),
        }
        with JOBS_LOCK:
            is_new_peak = point["cpu_percent"] > job["peak_cpu"]
            job["peak_cpu"] = max(job["peak_cpu"], point["cpu_percent"])
            job["peak_memory_mb"] = max(job["peak_memory_mb"], point["memory_mb"])
            job["elapsed_seconds"] = point["elapsed"]
            last_stored = job.get("_last_metric_store", -1.0)
            if is_new_peak or point["elapsed"] - last_stored >= 0.2:
                job["metrics"].append(point)
                job["_last_metric_store"] = point["elapsed"]
    except (OSError, ValueError, subprocess.SubprocessError):
        return


def worker_command(job: dict, backend: str, output_path: Path) -> list[str]:
    config = job["config"]
    benchmark = config["mode"] == "benchmark"
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_algorithm.py"),
        "--backend", backend,
        "--dataset", config["dataset"],
        "--algorithm", config["algorithm"],
        "--threads", str(config["threads"]),
        "--runs", str(config["benchmark_runs"] if benchmark else 1),
        "--warmup", "1" if benchmark else "0",
        "--params", json.dumps(config["params"], separators=(",", ":")),
        "--output", str(output_path),
    ]
    if benchmark:
        command.append("--summary-only")
    return command


def run_backend(job: dict, backend: str, position: int, total: int) -> dict:
    output_path = RUNS_DIR / f"{job['id']}-{backend}.json"
    with JOBS_LOCK:
        job["current_backend"] = backend
        job["current_backend_label"] = BACKEND_LABELS[backend]
        job["backend_index"] = position
        job["backend_total"] = total
        job["stage"] = "running"
        job["message"] = f"Running {BACKEND_LABELS[backend]} ({position}/{total})"

    process = subprocess.Popen(
        worker_command(job, backend, output_path),
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    sampler = ProcessMetricsSampler(process.pid)
    with JOBS_LOCK:
        elapsed_offset = job["metrics"][-1]["elapsed"] + 0.5 if job["metrics"] else job["elapsed_seconds"]
    while process.poll() is None:
        if job["config"]["collect_metrics"]:
            sample_process(sampler, started, elapsed_offset, backend, job["config"]["threads"], job)
        else:
            with JOBS_LOCK:
                job["elapsed_seconds"] = round(elapsed_offset + time.monotonic() - started, 3)
        time.sleep(0.05)
    _, stderr = process.communicate()
    if output_path.exists():
        try:
            return json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return {"status": "error", "backend": backend, "error": f"Invalid worker output: {error}"}
    return {
        "status": "error",
        "backend": backend,
        "error": (stderr or f"Worker exited with status {process.returncode}").strip()[-3000:],
    }


def execute_job(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job["status"] = "running"
        job["started_at"] = utc_now()
    config = job["config"]
    backends = BACKEND_IDS if config["mode"] == "benchmark" else ["easygraph"]

    try:
        for position, backend in enumerate(backends, start=1):
            payload = run_backend(job, backend, position, len(backends))
            result = {
                "backend": backend,
                "backend_label": BACKEND_LABELS[backend],
                "status": payload.get("status", "error"),
                "runtime_seconds": payload.get("runtime_seconds"),
                "runtime_std_seconds": payload.get("std_seconds"),
                "error": payload.get("error"),
            }
            with JOBS_LOCK:
                job["benchmark_results"].append(result)

            if config["mode"] == "analysis":
                if payload.get("status") != "completed":
                    raise RuntimeError(payload.get("error") or "EGMCC analysis failed.")
                algorithm = ALGORITHMS[config["algorithm"]]
                if algorithm["result_kind"] == "scalar":
                    job["scalar_result"] = finite(payload.get("scalar"))
                else:
                    job["results"] = normalize_rows(payload)
                    job["statistics"] = compute_statistics(job["results"])
                job["runtime_seconds"] = payload.get("runtime_seconds")

            if config["mode"] == "benchmark" and position < len(backends):
                with JOBS_LOCK:
                    job["stage"] = "cooldown"
                    job["message"] = f"{BACKEND_LABELS[backend]} finished; preparing the next isolated process"
                time.sleep(0.5)

        with JOBS_LOCK:
            job["status"] = "completed"
            job["stage"] = "completed"
            job["message"] = "Analysis completed" if config["mode"] == "analysis" else "Serial benchmark completed"
            job["completed_at"] = utc_now()
            job["current_backend"] = None
            job["current_backend_label"] = None
    except Exception as error:
        with JOBS_LOCK:
            job["status"] = "failed"
            job["stage"] = "failed"
            job["error"] = str(error)
            job["message"] = "Run failed"
            job["completed_at"] = utc_now()


def create_job(body: dict) -> tuple[dict | None, str | None]:
    mode = body.get("mode", "analysis")
    dataset = body.get("dataset")
    algorithm = body.get("algorithm")
    if mode not in {"analysis", "benchmark"}:
        return None, "mode must be 'analysis' or 'benchmark'."
    if dataset not in DATASETS:
        return None, "Unknown dataset."
    if algorithm not in ALGORITHMS:
        return None, "Unknown algorithm."
    try:
        threads = max(1, min(256, int(body.get("threads", 56))))
        benchmark_runs = max(1, min(30, int(body.get("benchmark_runs", 3))))
    except (TypeError, ValueError):
        return None, "threads and benchmark_runs must be integers."
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return None, "params must be an object."
    if params.get("weight_mode") == "weighted" and not DATASETS[dataset].get("weighted"):
        return None, "The selected dataset does not provide edge weights."

    with JOBS_LOCK:
        active = next((item for item in JOBS.values() if item["status"] in {"queued", "running"}), None)
        if active:
            return None, f"Run {active['id']} is still active."
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "status": "queued",
            "stage": "queued",
            "message": "Queued",
            "created_at": utc_now(),
            "started_at": None,
            "completed_at": None,
            "elapsed_seconds": 0,
            "runtime_seconds": None,
            "current_backend": None,
            "current_backend_label": None,
            "backend_index": 0,
            "backend_total": len(BACKEND_IDS) if mode == "benchmark" else 1,
            "peak_cpu": 0,
            "peak_memory_mb": 0,
            "metrics": [],
            "benchmark_results": [],
            "results": [],
            "scalar_result": None,
            "statistics": {},
            "error": None,
            "config": {
                "mode": mode,
                "dataset": dataset,
                "algorithm": algorithm,
                "threads": threads,
                "benchmark_runs": benchmark_runs,
                "collect_metrics": bool(body.get("collect_metrics", True)),
                "params": params,
            },
        }
        JOBS[job_id] = job
    threading.Thread(target=execute_job, args=(job_id,), daemon=True, name=f"analysis-{job_id}").start()
    return job, None


def public_job(job: dict) -> dict:
    algorithm = ALGORITHMS[job["config"]["algorithm"]]
    return {
        key: job[key]
        for key in (
            "id", "status", "stage", "message", "created_at", "started_at", "completed_at",
            "elapsed_seconds", "runtime_seconds", "current_backend", "current_backend_label",
            "backend_index", "backend_total", "peak_cpu", "peak_memory_mb", "metrics",
            "benchmark_results", "statistics", "scalar_result", "error", "config",
        )
    } | {
        "result_count": len(job["results"]),
        "result_kind": algorithm["result_kind"],
        "result_label": algorithm["result_label"],
    }


class DemoHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        sys.stdout.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def send_json(self, payload, status=HTTPStatus.OK):
        encoded = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def send_download(self, content: bytes, content_type: str, filename: str):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/runs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = self.read_json()
        if body is None:
            self.send_json({"error": "Invalid JSON request body."}, HTTPStatus.BAD_REQUEST)
            return
        job, error = create_job(body)
        if error:
            status = HTTPStatus.CONFLICT if error.startswith("Run ") else HTTPStatus.BAD_REQUEST
            self.send_json({"error": error}, status)
            return
        self.send_json(public_job(job), HTTPStatus.ACCEPTED)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/OpenMP.pdf":
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", "/EGMCC.pdf")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path == "/api/catalog":
            self.send_json(public_catalog())
            return
        if path == "/api/health":
            with JOBS_LOCK:
                active = next((job["id"] for job in JOBS.values() if job["status"] in {"queued", "running"}), None)
            self.send_json({"status": "ready", "active_job": active})
            return
        if not path.startswith("/api/runs/"):
            if path.startswith("/runs/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            super().do_GET()
            return

        suffix = path.removeprefix("/api/runs/")
        parts = suffix.split("/", 1)
        job_id = parts[0]
        action = parts[1] if len(parts) > 1 else ""
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            self.send_json({"error": "Run not found."}, HTTPStatus.NOT_FOUND)
            return
        if not action:
            with JOBS_LOCK:
                self.send_json(public_job(job))
            return
        if action == "results":
            self.send_results(job, parse_qs(parsed.query))
            return
        if action in {"download.csv", "download.json"}:
            self.send_result_download(job, action)
            return
        self.send_json({"error": "Unknown run endpoint."}, HTTPStatus.NOT_FOUND)

    def send_results(self, job: dict, query: dict):
        if job["status"] != "completed" or job["config"]["mode"] != "analysis":
            self.send_json({"error": "Results are not available yet."}, HTTPStatus.CONFLICT)
            return
        if ALGORITHMS[job["config"]["algorithm"]]["result_kind"] == "scalar":
            self.send_json({"kind": "scalar", "value": job["scalar_result"], "total": 1})
            return
        try:
            page = max(1, int(query.get("page", ["1"])[0]))
            page_size = max(1, min(200, int(query.get("page_size", ["50"])[0])))
        except ValueError:
            self.send_json({"error": "Invalid pagination."}, HTTPStatus.BAD_REQUEST)
            return
        search = query.get("search", [""])[0].strip().lower()
        sort_key = query.get("sort", ["value"])[0]
        direction = query.get("direction", ["desc"])[0]
        if sort_key not in {"rank", "node_id", "mapped_id", "value", "percentile"}:
            sort_key = "value"
        rows = job["results"]
        if search:
            rows = [row for row in rows if search in str(row["node_id"]).lower()]
        rows = sorted(rows, key=lambda row: (row.get(sort_key) is None, row.get(sort_key)), reverse=direction != "asc")
        start = (page - 1) * page_size
        self.send_json({"kind": "node_scores", "rows": rows[start:start + page_size], "page": page, "page_size": page_size, "total": len(rows)})

    def send_result_download(self, job: dict, action: str):
        if job["status"] != "completed" or job["config"]["mode"] != "analysis":
            self.send_json({"error": "Results are not available yet."}, HTTPStatus.CONFLICT)
            return
        dataset = job["config"]["dataset"]
        algorithm = job["config"]["algorithm"]
        basename = f"{dataset}-{algorithm}-{job['id']}"
        if action == "download.json":
            payload = {
                "run_id": job["id"],
                "config": job["config"],
                "runtime_seconds": job["runtime_seconds"],
                "statistics": job["statistics"],
                "scalar_result": job["scalar_result"],
                "results": job["results"],
            }
            self.send_download(json.dumps(payload, indent=2, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", basename + ".json")
            return
        stream = io.StringIO()
        writer = csv.writer(stream)
        if ALGORITHMS[algorithm]["result_kind"] == "scalar":
            writer.writerow(["metric", "value"])
            writer.writerow([ALGORITHMS[algorithm]["result_label"], job["scalar_result"]])
        else:
            writer.writerow(["rank", "node_id", "mapped_id", "value", "percentile"])
            for row in job["results"]:
                writer.writerow([row["rank"], row["node_id"], row["mapped_id"], row["value"], row["percentile"]])
        self.send_download(stream.getvalue().encode("utf-8"), "text/csv; charset=utf-8", basename + ".csv")


def main() -> None:
    RUNS_DIR.mkdir(exist_ok=True)
    host = os.environ.get("EGMCC_DEMO_HOST", "127.0.0.1")
    port = int(os.environ.get("EGMCC_DEMO_PORT", "4173"))
    server = ThreadingHTTPServer((host, port), DemoHandler)
    print(f"EGMCC demo: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
