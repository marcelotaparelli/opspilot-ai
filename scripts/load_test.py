"""Small reproducible latency baseline against a running stack (not capacity planning).

    OPSPILOT_TOKEN=... python scripts/load_test.py --scenario rag --requests 400 --concurrency 8

Warmup requests are excluded. Latency uses a monotonic clock per request. Optional
--sample CONTAINER... polls `docker stats` once per second for CPU and memory.
"""

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx

QUESTION = "How do I restart payments-api?"
SCENARIOS = {
    "rag": ("/v1/query", {"question": QUESTION, "top_k": 5}),
    "agent": ("/v1/agent/runs", {"request": QUESTION}),  # read-only path: answered, no GitLab
}


def sampler(containers: list[str], stop: threading.Event, samples: list[dict[str, Any]]) -> None:
    while not stop.is_set():
        try:
            output = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            at = time.monotonic()
            for line in output.splitlines():
                row = json.loads(line)
                samples.append(
                    {
                        "t": at,
                        "name": row["Name"],
                        "cpu_percent": float(row["CPUPerc"].rstrip("%")),
                        "memory": row["MemUsage"].split("/")[0].strip(),
                    }
                )
        except (subprocess.SubprocessError, ValueError, KeyError):
            pass
        stop.wait(1)


async def run(
    base: str, token: str, scenario: str, requests: int, concurrency: int, warmup: int
) -> dict[str, Any]:
    path, body = SCENARIOS[scenario]
    headers = {"Authorization": f"Bearer {token}"}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base, timeout=30, limits=limits) as client:
        for _ in range(warmup):
            await client.post(path, headers=headers, json=body)
        latencies: list[float] = []
        errors = 0
        queue: asyncio.Queue[int] = asyncio.Queue()
        for index in range(requests):
            queue.put_nowait(index)

        async def worker() -> None:
            nonlocal errors
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                started = time.monotonic()
                try:
                    response = await client.post(path, headers=headers, json=body)
                    if response.status_code >= 300:
                        errors += 1
                except httpx.HTTPError:
                    errors += 1
                latencies.append(time.monotonic() - started)

        started = time.monotonic()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        wall = time.monotonic() - started
    cuts = statistics.quantiles(latencies, n=100)
    return {
        "scenario": scenario,
        "path": path,
        "requests": requests,
        "warmup": warmup,
        "concurrency": concurrency,
        "wall_seconds": round(wall, 3),
        "throughput_rps": round(requests / wall, 1),
        "latency_ms": {
            "p50": round(cuts[49] * 1000, 1),
            "p95": round(cuts[94] * 1000, 1),
            "p99": round(cuts[98] * 1000, 1),
            "max": round(max(latencies) * 1000, 1),
            "mean": round(statistics.fmean(latencies) * 1000, 1),
        },
        "errors": errors,
        "error_rate": errors / requests,
    }


def environment() -> dict[str, Any]:
    cpu = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
        memory = Path("/proc/meminfo").read_text().splitlines()[0].split()[1]
    except OSError:
        memory = "unknown"
    return {
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "cpu_model": cpu,
        "memory_kib": memory,
        "python": platform.python_version(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    parser.add_argument("--requests", type=int, default=400)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=40)
    parser.add_argument("--sample", nargs="*", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    samples: list[dict[str, Any]] = []
    stop = threading.Event()
    thread = threading.Thread(target=sampler, args=(args.sample, stop, samples), daemon=True)
    if args.sample:
        thread.start()
    try:
        result = asyncio.run(
            run(
                args.base_url,
                os.environ["OPSPILOT_TOKEN"],
                args.scenario,
                args.requests,
                args.concurrency,
                args.warmup,
            )
        )
    finally:
        stop.set()
        if args.sample:
            thread.join(timeout=15)
    result["environment"] = environment()
    if samples:
        result["resources"] = {
            name: {
                "samples": len(rows),
                "cpu_percent_max": max(r["cpu_percent"] for r in rows),
                "cpu_percent_mean": round(statistics.fmean(r["cpu_percent"] for r in rows), 1),
                "memory_first": rows[0]["memory"],
                "memory_last": rows[-1]["memory"],
            }
            for name in sorted({s["name"] for s in samples})
            for rows in [[s for s in samples if s["name"] == name]]
        }
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    main()
