"""Start the local Windows API and real institution workers with one command."""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def commands(python, *, port=8000, institution=True, farmer=False):
    result = {
        "api": [
            python,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
    }
    worker = [
        "worker",
        "--pool=solo",
        "--concurrency=1",
        "--loglevel=info",
        "--without-gossip",
        "--without-mingle",
    ]
    if farmer:
        result["farmer"] = [
            python,
            "-m",
            "celery",
            "-A",
            "tasks.celery_app:celery_app",
            *worker,
            "-Q",
            "celery",
            "--hostname=tt-local-farmer@%h",
        ]
    if institution:
        app = [python, "-m", "celery", "-A", "tasks.institution_app:institution_app"]
        result["operations"] = [
            *app,
            *worker,
            "-Q",
            "tt_institution_operations",
            "--hostname=tt-local-operations@%h",
        ]
        queues = "tt_institution_evidence,tt_institution_exports,tt_institution_accounting,tt_institution_raster,tt_institution_advisory,tt_institution_ingestion,tt_institution_privacy"
        result["institution"] = [
            *app,
            *worker,
            "-Q",
            queues,
            "--hostname=tt-local-institution@%h",
        ]
        result["beat"] = [
            *app,
            "beat",
            "--loglevel=info",
            "--schedule",
            str(ROOT / ".local-runtime" / "institution-beat"),
        ]
    return result


def stop(process):
    if process.poll() is not None:
        return
    try:
        process.send_signal(
            signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
        )
        process.wait(timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--include-farmer-worker",
        action="store_true",
        help="Use only if no existing farmer worker is serving the selected broker.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without importing the app or starting services.",
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    from dotenv import dotenv_values

    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    if config.get("ENVIRONMENT", "development") != "development":
        parser.error(
            "Set ENVIRONMENT=development for the laptop; this launcher is local-only."
        )
    institution = str(config.get("INSTITUTION_SCHEMA_ENABLED", "false")).lower() in {
        "true",
        "1",
        "yes",
        "on",
    }
    selected = commands(
        sys.executable,
        port=args.port,
        institution=institution,
        farmer=args.include_farmer_worker,
    )
    if args.dry_run:
        for name, command in selected.items():
            print(name + ": " + subprocess.list2cmdline(command))
        return 0
    runtime = ROOT / ".local-runtime"
    runtime.mkdir(exist_ok=True)
    processes, logs = [], []
    try:
        for name, command in selected.items():
            output = None
            if name != "api":
                output = (runtime / (name + ".log")).open("a", encoding="utf-8")
                logs.append(output)
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.STDOUT if output else None,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0,
            )
            processes.append((name, process))
        print(f"Local API: http://localhost:{args.port}/docs", flush=True)
        print(
            "Worker logs: .local-runtime/  |  Ctrl+C stops these local processes.",
            flush=True,
        )
        if institution:
            print(
                "Use only one institution scheduler on this broker/database at a time.",
                flush=True,
            )
        while True:
            for name, process in processes:
                if process.poll() is not None:
                    print(
                        f"{name} exited ({process.returncode}); inspect its console/log.",
                        file=sys.stderr,
                    )
                    return process.returncode or 1
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        for _, process in reversed(processes):
            stop(process)
        for output in logs:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
