"""Detached per-host worker, controlled through SSH; one job owns the GPU."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import urlopen


def atomic_json(path: Path, value: dict):
    temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def status(root: Path) -> dict:
    path = root / "worker-state.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"state": "idle"}
    if state["state"] in ("starting", "running"):
        with (root / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return state
            state.update(state="failed", error="Worker exited without recording completion")
            atomic_json(path, state)
    return state


def gpu_status() -> dict:
    result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,utilization.gpu",
                             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15, check=True)
    name, total, free, utilization = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
    return {"name": name, "total_mb": int(total), "free_mb": int(free), "utilization": int(utilization)}


def start(root: Path, job_path: Path) -> dict:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    lock = (root / "worker.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {"accepted": False, **status(root)}
    try:
        gpu = gpu_status()
        if gpu["free_mb"] < job.get("minimum_free_gpu_mb", 5500) or gpu["utilization"] > 10:
            return {"accepted": False, "state": "gpu_busy", "gpu": gpu}
        state = {"state": "starting", "job_id": job["id"], "attempt": job.get("attempt", 1), "updated": time.time(), "gpu": gpu}
        atomic_json(root / "worker-state.json", state)
        with (job_path.parent / "worker.log").open("a") as log:
            subprocess.Popen([sys.executable, "-m", "pdf_craft_tool.cluster_worker", "--root", str(root), "--execute", str(job_path),
                              "--lock-fd", str(lock.fileno())], pass_fds=(lock.fileno(),),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        return {"accepted": True, **state}
    finally:
        lock.close()


def execute(root: Path, job_path: Path, lock_fd: int):
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(root / "tokenizer-cache"))
    from .cli import _parser
    from .book import run_book

    job = json.loads(job_path.read_text(encoding="utf-8"))
    state = {"state": "running", "job_id": job["id"], "attempt": job.get("attempt", 1), "pid": os.getpid(), "started": time.time()}
    done = threading.Event()
    state_lock = threading.Lock()
    def heartbeat():
        while not done.is_set():
            with state_lock:
                atomic_json(root / "worker-state.json", {**state, "updated": time.time()})
            done.wait(15)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    server = None
    server_log = None
    try:
        source = job_path.parent / "source.pdf"
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != job["sha256"]:
                raise ValueError("Transferred PDF does not match the queued content hash")
        environment = os.environ.copy()
        environment.update(OLLAMA_HOST="127.0.0.1:11634", OLLAMA_MODELS=str(root / "models"),
                           OLLAMA_NUM_PARALLEL="1", OLLAMA_MAX_LOADED_MODELS="1",
                           OLLAMA_CONTEXT_LENGTH="4096", OLLAMA_NO_CLOUD="true")
        server_log = (job_path.parent / "ollama.log").open("a")
        server = subprocess.Popen([str(root / "ollama/bin/ollama"), "serve"], env=environment,
                                  stdout=server_log, stderr=server_log)
        for _ in range(60):
            if server.poll() is not None:
                raise RuntimeError("Worker's private Ollama failed to start; inspect ollama.log")
            try:
                with urlopen("http://127.0.0.1:11634/api/tags", timeout=2) as response:
                    installed = {item["name"]: item["digest"] for item in json.load(response)["models"]}
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("Timed out starting Ollama")
        for name, digest in job["models"].items():
            if installed.get(name) != digest:
                raise ValueError(f"Model digest mismatch: {name}")
        work = job_path.parent / "work"
        argv = ["book", str(source), "--work-dir", str(work), "--title", job["title"],
                "--tesseract", str(root / "tesseract/bin/tesseract"), "--tessdata", str(root / "tessdata"),
                "--ollama-url", "http://127.0.0.1:11634", *job["book_options"]]
        args = _parser().parse_args(argv)
        if run_book(args):
            raise RuntimeError("Book pipeline failed")
        summary = json.loads((work / "run-summary.json").read_text(encoding="utf-8"))
        with state_lock:
            state.update(state="done", summary=summary, finished=time.time())
    except BaseException as error:
        with state_lock:
            state.update(state="failed", error=f"{type(error).__name__}: {error}", finished=time.time())
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        if server_log:
            server_log.close()
        done.set()
        thread.join(timeout=20)
        atomic_json(root / "worker-state.json", {**state, "updated": time.time()})
        atomic_json(job_path.parent / "result.json", state)
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--start", type=Path)
    parser.add_argument("--execute", type=Path)
    parser.add_argument("--lock-fd", type=int)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.execute:
        execute(args.root, args.execute, args.lock_fd)
    elif args.start:
        print(json.dumps(start(args.root, args.start)))
    elif args.probe:
        print(json.dumps({"gpu": gpu_status(), **status(args.root)}))
    else:
        print(json.dumps(status(args.root)))


if __name__ == "__main__":
    main()
