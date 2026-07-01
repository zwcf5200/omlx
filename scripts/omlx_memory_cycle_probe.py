#!/usr/bin/env python3
"""Probe oMLX model load/unload memory retention.

The script assumes the target oMLX service is already running. For clean
before/after comparisons, restart the service before invoking this probe.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


DEFAULT_MODELS = [
    "Qwen3-4B-Instruct-2507-MLX-4bit",
    "Qwen3-Embedding-4B-4bit-DWQ",
    "Qwen3-Reranker-0.6B-4bit",
    "Ornith-1.0-35B-5bit-mlx",
]


def run(cmd: list[str], timeout: int = 120) -> str:
    return subprocess.check_output(
        cmd,
        text=True,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def listener_port(base_url: str) -> int:
    parsed = urlparse(base_url)
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    return 80


def find_pid(port: int) -> int:
    output = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            return int(parts[1])
    raise RuntimeError(f"No listener found on port {port}")


def parse_size_to_mb(text: str) -> float:
    match = re.match(r"\s*([0-9.]+)([KMGTP]?)", text)
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = match.group(2)
    return value * {
        "": 1 / 1024 / 1024,
        "K": 1 / 1024,
        "M": 1,
        "G": 1024,
        "T": 1024 * 1024,
    }[unit]


def ps_rss_mb(pid: int) -> float:
    return int(run(["ps", "-p", str(pid), "-o", "rss="]).strip()) / 1024


def vmmap_metrics(pid: int) -> dict[str, float | None]:
    output = run(["vmmap", "-summary", str(pid)], timeout=180)
    metrics: dict[str, float | None] = {
        "physical_footprint_mb": None,
        "physical_footprint_peak_mb": None,
        "ioaccelerator_graphics_mb": None,
        "ioaccelerator_mb": None,
        "malloc_large_mb": None,
        "malloc_small_empty_mb": None,
        "vm_allocate_mb": None,
    }
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Physical footprint:"):
            metrics["physical_footprint_mb"] = parse_size_to_mb(
                stripped.split(":", 1)[1]
            )
        elif stripped.startswith("Physical footprint (peak):"):
            metrics["physical_footprint_peak_mb"] = parse_size_to_mb(
                stripped.split(":", 1)[1]
            )
        elif stripped.startswith("IOAccelerator (graphics)"):
            metrics["ioaccelerator_graphics_mb"] = parse_size_to_mb(
                stripped.split()[2]
            )
        elif stripped.startswith("IOAccelerator "):
            metrics["ioaccelerator_mb"] = parse_size_to_mb(stripped.split()[1])
        elif stripped.startswith("MALLOC_LARGE"):
            metrics["malloc_large_mb"] = parse_size_to_mb(stripped.split()[1])
        elif stripped.startswith("MALLOC_SMALL (empty)"):
            metrics["malloc_small_empty_mb"] = parse_size_to_mb(stripped.split()[2])
        elif stripped.startswith("VM_ALLOCATE"):
            metrics["vm_allocate_mb"] = parse_size_to_mb(stripped.split()[1])
    return metrics


def status(session: requests.Session, base_url: str) -> dict[str, Any]:
    resp = session.get(f"{base_url}/api/status", timeout=30)
    resp.raise_for_status()
    return resp.json()


def admin_login(session: requests.Session, base_url: str, api_key: str) -> None:
    resp = session.post(
        f"{base_url}/admin/api/login",
        json={"api_key": api_key, "remember": False},
        timeout=30,
    )
    resp.raise_for_status()


def model_action(
    session: requests.Session,
    base_url: str,
    model: str,
    action: str,
    timeout: int,
) -> dict[str, Any]:
    resp = session.post(f"{base_url}/admin/api/models/{model}/{action}", timeout=timeout)
    if resp.status_code == 400 and action == "unload" and "not loaded" in resp.text:
        return {"status": "already_unloaded", "model_id": model}
    resp.raise_for_status()
    return resp.json()


def wait_loaded(
    session: requests.Session,
    base_url: str,
    expected: set[str],
    timeout: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = status(session, base_url)
        loaded = set(last.get("loaded_models") or [])
        if expected.issubset(loaded) and not last.get("models_loading"):
            return last
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for loaded={sorted(expected)}; last={last}")


def wait_unloaded(
    session: requests.Session,
    base_url: str,
    timeout: int,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = status(session, base_url)
        if not last.get("loaded_models") and not last.get("models_loading"):
            return last
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for all models unloaded; last={last}")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "models"


def sample(
    session: requests.Session,
    base_url: str,
    pid: int,
    cycle: int,
    phase: str,
    models: list[str],
    jsonl_path: Path,
) -> dict[str, Any]:
    current = status(session, base_url)
    row: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "cycle": cycle,
        "phase": phase,
        "pid": pid,
        "tested_models": ",".join(models),
        "rss_mb": round(ps_rss_mb(pid), 1),
        "models_loaded": current.get("models_loaded"),
        "loaded_models": ",".join(current.get("loaded_models") or []),
        "model_memory_used_mb": round(
            (current.get("model_memory_used") or 0) / 1024 / 1024,
            1,
        ),
        "active_requests": current.get("active_requests"),
        "waiting_requests": current.get("waiting_requests"),
    }
    for key, value in vmmap_metrics(pid).items():
        row[key] = None if value is None else round(value, 1)
    with jsonl_path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def build_summary(
    *,
    pid: int,
    rounds: int,
    models: list[str],
    rows: list[dict[str, Any]],
    jsonl_path: Path,
    csv_path: Path,
) -> dict[str, Any]:
    after_rows = [r for r in rows if r["phase"] == "after_unload"]
    if not after_rows:
        raise RuntimeError("No after_unload samples were collected")

    baseline = rows[0]
    first = after_rows[0]
    last = after_rows[-1]

    return {
        "pid": pid,
        "rounds": rounds,
        "models": models,
        "baseline": baseline,
        "first_after_unload": first,
        "last_after_unload": last,
        "rss_drift_after_unload_mb": round(last["rss_mb"] - first["rss_mb"], 1),
        "physical_footprint_drift_after_unload_mb": round(
            last["physical_footprint_mb"] - first["physical_footprint_mb"],
            1,
        ),
        "ioaccelerator_graphics_drift_after_unload_mb": round(
            last["ioaccelerator_graphics_mb"] - first["ioaccelerator_graphics_mb"],
            1,
        ),
        "rss_drift_vs_clean_baseline_mb": round(
            last["rss_mb"] - baseline["rss_mb"],
            1,
        ),
        "physical_footprint_drift_vs_clean_baseline_mb": round(
            last["physical_footprint_mb"] - baseline["physical_footprint_mb"],
            1,
        ),
        "samples_jsonl": str(jsonl_path),
        "samples_csv": str(csv_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cycle oMLX model load/unload and record process memory."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:11335"),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OMLX_API_KEY"),
        help="Admin API key. Defaults to OMLX_API_KEY.",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Model ID to include. Repeat to test multiple models.",
    )
    parser.add_argument(
        "--default-suite",
        action="store_true",
        help="Use the four-model suite from the 11335 leak investigation.",
    )
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--settle-seconds", type=int, default=8)
    parser.add_argument("--load-timeout", type=int, default=900)
    parser.add_argument("--unload-timeout", type=int, default=900)
    parser.add_argument(
        "--out-dir",
        default="/tmp/omlx-memory-cycle-probe",
        help="Directory for JSONL/CSV/summary output.",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Output filename label. Defaults to model-derived label.",
    )
    parser.add_argument(
        "--skip-preclean",
        action="store_true",
        help="Do not unload models already loaded before cycle 1.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.api_key:
        print("error: --api-key or OMLX_API_KEY is required", file=sys.stderr)
        return 2
    if args.default_suite:
        models = list(DEFAULT_MODELS)
    else:
        models = list(args.models or [])
    if not models:
        print("error: provide --model at least once or use --default-suite", file=sys.stderr)
        return 2
    if args.rounds < 1:
        print("error: --rounds must be >= 1", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    pid = find_pid(listener_port(base_url))
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {args.api_key}"})
    admin_login(session, base_url, args.api_key)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    label = args.label or safe_name("__".join(models))
    jsonl_path = out_dir / f"{label}.jsonl"
    csv_path = out_dir / f"{label}.csv"
    summary_path = out_dir / f"{label}.summary.json"
    jsonl_path.write_text("")

    print(f"pid={pid}", flush=True)
    print(f"base_url={base_url}", flush=True)
    print(f"models={models}", flush=True)

    if not args.skip_preclean:
        current = status(session, base_url)
        for loaded in list(current.get("loaded_models") or []):
            print(f"preclean unload {loaded}", flush=True)
            model_action(session, base_url, loaded, "unload", args.unload_timeout)
        wait_unloaded(session, base_url, args.unload_timeout)
        time.sleep(args.settle_seconds)

    rows: list[dict[str, Any]] = [
        sample(session, base_url, pid, 0, "initial_clean_baseline", models, jsonl_path)
    ]

    for cycle in range(1, args.rounds + 1):
        rows.append(sample(session, base_url, pid, cycle, "before_load", models, jsonl_path))

        for model in models:
            print(f"cycle={cycle} load {model}", flush=True)
            model_action(session, base_url, model, "load", args.load_timeout)
        wait_loaded(session, base_url, set(models), args.load_timeout)
        time.sleep(args.settle_seconds)
        rows.append(sample(session, base_url, pid, cycle, "all_loaded", models, jsonl_path))

        for model in reversed(models):
            print(f"cycle={cycle} unload {model}", flush=True)
            model_action(session, base_url, model, "unload", args.unload_timeout)
        wait_unloaded(session, base_url, args.unload_timeout)
        time.sleep(args.settle_seconds)
        rows.append(sample(session, base_url, pid, cycle, "after_unload", models, jsonl_path))

    write_csv(csv_path, rows)
    summary = build_summary(
        pid=pid,
        rounds=args.rounds,
        models=models,
        rows=rows,
        jsonl_path=jsonl_path,
        csv_path=csv_path,
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
