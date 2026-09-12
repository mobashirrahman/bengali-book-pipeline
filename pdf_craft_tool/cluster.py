"""SSH book farm: local SQLite coordinator, durable workers, incremental discovery."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

from .cluster_queue import BookQueue
from .cluster_worker import atomic_json

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=accept-new"]


def run(command: list[str], *, timeout=120, log=None) -> str:
    result = subprocess.run(command, stdout=log or subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout, check=True)
    return result.stdout or ""


class SSHWorker:
    def __init__(self, host: str, config: dict, log):
        self.host, self.config, self.log = host, config, log
        self.root = config["worker_root"]
        self.release = f"{self.root}/releases/{config['release']}"
        self.local = host.split(".")[0] == socket.gethostname().split(".")[0]

    def shell(self, command: list[str], timeout=120) -> str:
        if self.local:
            return run(command, timeout=timeout)
        return run(["ssh", *SSH_OPTIONS, self.host, shlex.join(command)], timeout=timeout)

    def copy(self, source: str, target: str, *, receive=False, timeout=7200):
        if not self.local:
            if receive:
                source = f"{self.host}:{source}"
            else:
                target = f"{self.host}:{target}"
        # --protect-args preserves spaces, Bengali names and shell metacharacters.
        run(["rsync", "-a", "--partial", "--protect-args", "-e", shlex.join(["ssh", *SSH_OPTIONS]),
             "--", source, target], timeout=timeout, log=self.log)

    def rpc(self, *args) -> dict:
        output = self.shell(["env", f"PYTHONPATH={self.release}", f"{self.root}/venv/bin/python", "-m",
                             "pdf_craft_tool.cluster_worker", "--root", self.root, *args])
        return json.loads(output)

    def prepare(self):
        self.shell(["mkdir", "-p", self.root, self.release, f"{self.root}/jobs"])
        marker = f"{self.root}/ready-{self.config['runtime']}"
        try:
            self.shell(["test", "-f", marker])
            ready = True
        except subprocess.CalledProcessError:
            ready = False
        if not ready:
            seed = Path(self.config["seed_root"])
            if not self.local or seed.resolve() != Path(self.root).resolve():
                for name in ("venv", "tesseract", "tessdata", "ollama", "models", "tokenizer-cache"):
                    self.shell(["mkdir", "-p", f"{self.root}/{name}"])
                    self.copy(str(seed / name) + "/", f"{self.root}/{name}/")
            self.shell([f"{self.root}/venv/bin/python", "-c", "import pdf_craft, PIL, tiktoken, cryptography"])
            self.shell(["env", f"TESSDATA_PREFIX={self.root}/tessdata", f"{self.root}/tesseract/bin/tesseract", "--list-langs"])
            self.shell(["touch", marker])
        source = Path(self.config["source_release"])
        if not self.local or source.resolve() != Path(self.release).resolve():
            self.copy(str(source) + "/", self.release + "/")
        return self.rpc("--probe")

    def submit(self, job: dict, local_root: Path) -> dict:
        remote = f"{self.root}/jobs/{job['id']}"
        self.shell(["mkdir", "-p", remote])
        self.copy(job["source"], remote + "/source.pdf")
        payload = {key: job[key] for key in ("id", "sha256", "title")}
        payload.update(attempt=job["attempts"], book_options=self.config["book_options"], models=self.config["models"],
                       minimum_free_gpu_mb=self.config.get("minimum_free_gpu_mb", 5500))
        directory = local_root / "jobs" / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "job.json"
        atomic_json(path, payload)
        self.copy(str(path), remote + "/job.json")
        return self.rpc("--start", remote + "/job.json")

    def collect(self, job: dict, local_root: Path) -> dict:
        remote = f"{self.root}/jobs/{job['id']}"
        target = local_root / "jobs" / job["id"]
        target.mkdir(parents=True, exist_ok=True)
        self.copy(remote + "/work/", str(target / "work") + "/", receive=True)
        for filename in ("result.json", "worker.log", "ollama.log"):
            self.copy(remote + "/" + filename, str(target / filename), receive=True)
        summary = json.loads((target / "work/run-summary.json").read_text(encoding="utf-8"))
        remote_work = remote + "/work"
        for field in ("raw", "proofreading", "audit", "output"):
            value = summary.get(field)
            if isinstance(value, str) and value.startswith(remote_work + "/"):
                summary[field] = str(target / "work" / value[len(remote_work) + 1:])
        # Completion requires local, hash-verified reader artifacts, not an SSH exit code.
        output = Path(summary["output"])
        for name in ("book.epub", "book.md", "chunks.jsonl", "source-map.json"):
            artifact = output / name
            with artifact.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != artifact.with_suffix(artifact.suffix + ".sha256").read_text():
                raise ValueError(f"Downloaded artifact checksum mismatch: {artifact}")
        atomic_json(target / "summary.json", summary)
        return summary


def host_loop(host: str, config: dict, root: Path, stop: threading.Event):
    queue = BookQueue(root / "queue.sqlite3")
    with (root / "logs" / f"{host}.log").open("a", buffering=1) as log:
        worker = SSHWorker(host, config, log)
        prepared = False
        while not stop.is_set():
            try:
                if not prepared:
                    queue.node(host, "preparing")
                    worker.prepare()
                    prepared = True
                state = worker.rpc()
                job = queue.active().get(host)
                if job:
                    same_attempt = state.get("job_id") == job["id"] and state.get("attempt", 1) == job["attempts"]
                    if same_attempt and state["state"] == "done":
                        queue.node(host, "collecting", job["id"])
                        result = worker.collect(job, root)
                        queue.finish(job["id"], host, result)
                        queue.node(host, "ready")
                    elif same_attempt and state["state"] == "failed":
                        queue.fail(job["id"], host, state.get("error", "worker failed"))
                    elif state["state"] in ("starting", "running"):
                        # Unrecognized busy jobs are never overwritten after coordinator restart.
                        queue.node(host, "running" if state.get("job_id") == job["id"] else "busy", json.dumps(state))
                    else:
                        queue.node(host, "uploading", job["id"])
                        reply = worker.submit(job, root)
                        queue.node(host, reply["state"], json.dumps(reply))
                elif state["state"] in ("running", "starting"):
                    queue.node(host, "busy", json.dumps(state))
                else:
                    probe = worker.rpc("--probe")
                    gpu = probe["gpu"]
                    if gpu["free_mb"] < config.get("minimum_free_gpu_mb", 5500) or gpu["utilization"] > 10:
                        queue.node(host, "gpu_busy", json.dumps(gpu))
                    else:
                        job = queue.claim(host, config["profile"])
                        queue.node(host, "claimed" if job else "ready", job["id"] if job else "")
                        if job:
                            continue
            except Exception as error:
                # Network loss does NOT expire ownership: the remote job may still be running.
                detail = f"{type(error).__name__}: {error}"
                if isinstance(error, subprocess.CalledProcessError) and error.stdout:
                    detail += "\n" + error.stdout[-3000:]
                log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + detail + "\n")
                queue.node(host, "unreachable" if not prepared else "retrying", detail)
            stop.wait(config.get("poll_seconds", 30))
        queue.close()


def serve(config: dict, root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    stop = threading.Event()
    def interrupt(*_):
        stop.set()
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    with (root / "coordinator.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic_json(root / "coordinator.json", {"pid": os.getpid(), "host": socket.gethostname(), "started": time.time()})
        queue = BookQueue(root / "queue.sqlite3")
        with ThreadPoolExecutor(max_workers=len(config["hosts"])) as pool:
            futures = [pool.submit(host_loop, host, config, root, stop) for host in config["hosts"]]
            try:
                while not stop.is_set():
                    try:
                        discovery = queue.scan(Path(config["data_root"]), config["profile"], config.get("settle_seconds", 120))
                        report = {"updated": time.time(), "discovery": discovery, **queue.report()}
                        atomic_json(root / "status.json", report)
                        print(json.dumps({"time": time.time(), **discovery, "counts": report["counts"]}), flush=True)
                        for future in futures:
                            if future.done():
                                future.result()
                                raise RuntimeError("Host scheduler stopped unexpectedly")
                    except (OSError, sqlite3.OperationalError) as error:
                        queue.db.rollback()
                        print(f"Discovery temporarily failed: {error}", flush=True)
                    stop.wait(config.get("scan_seconds", 60))
            finally:
                stop.set()
                queue.close()


def initialize(args) -> dict:
    """Freeze a release and processing profile before any jobs are claimed."""
    repo, root, seed = args.repo.resolve(), args.state_root.resolve(), args.seed_root.resolve()
    if not args.data_root.is_dir():
        raise ValueError("The data root must be an existing directory")
    if root == args.data_root.resolve() or root.is_relative_to(args.data_root.resolve()):
        raise ValueError("Keep cluster output outside the watched data directory")
    root.mkdir(parents=True, exist_ok=True)
    models = {}
    for model in args.model:
        name, tag = model.split(":", 1)
        path = seed / "models/manifests/registry.ollama.ai/library" / name / tag
        models[model] = hashlib.sha256(path.read_bytes()).hexdigest()
    files = [repo / "pyproject.toml"]
    for directory in ("pdf_craft", "pdf_craft_tool"):
        files.extend(path for path in (repo / directory).rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix != ".pyc")
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(repo)).encode())
        digest.update(path.read_bytes())
    release = digest.hexdigest()[:20]
    release_root = root / "releases" / release
    for path in files:
        target = release_root / path.relative_to(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(path, target)
    runtime = hashlib.sha256(run([str(seed / "venv/bin/python"), "-m", "pip", "freeze"]).encode())
    runtime.update(json.dumps(models, sort_keys=True).encode())
    runtime.update((seed / "tessdata/ben.traineddata").read_bytes())
    book_options = ["--model", args.model[0]]
    if args.no_proofread:
        book_options.append("--no-proofread")
    if args.vision_proofread:
        book_options.extend(["--vision-proofread", "--review-only"])
    config = {"data_root": str(args.data_root.resolve()), "state_root": str(root), "seed_root": str(seed),
              "source_release": str(release_root), "release": release, "runtime": runtime.hexdigest()[:20],
              "worker_root": str(seed), "models": models, "book_options": book_options,
              "hosts": [f"bio{index:02d}" for index in range(1, 25)], "scan_seconds": 60,
              "settle_seconds": 120, "poll_seconds": 30, "minimum_free_gpu_mb": 5500}
    config["processing"] = processing_hash(release_root)
    config["profile"] = hashlib.sha256(json.dumps({key: config[key] for key in
        ("processing", "runtime", "models", "book_options")}, sort_keys=True).encode()).hexdigest()
    if args.config.exists():
        previous = json.loads(args.config.read_text(encoding="utf-8"))
        same_processing = processing_hash(Path(previous["source_release"])) == config["processing"]
        if same_processing and all(previous[key] == config[key] for key in ("runtime", "models", "book_options")):
            config["profile"] = previous["profile"]
    atomic_json(args.config, config)
    return config


def processing_hash(root: Path) -> str:
    """Scheduler-only upgrades do not re-OCR the entire collection."""
    digest = hashlib.sha256()
    for directory in ("pdf_craft", "pdf_craft_tool"):
        for path in sorted((root / directory).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and not path.name.startswith("cluster"):
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "run", "status", "scan", "retry-failed"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--state-root", type=Path, default=Path("pdf-craft-output/cluster"))
    parser.add_argument("--seed-root", type=Path, default=Path("/scratch/pdf-craft-worker-" + os.environ.get("USER", "worker")))
    parser.add_argument("--model", action="append", default=None)
    parser.add_argument("--vision-proofread", action="store_true")
    parser.add_argument("--no-proofread", action="store_true",
                        help="OCR-only fleet: skip Qwen proofreading, render from raw OCR")
    args = parser.parse_args()
    if args.command == "init":
        args.model = args.model or ["qwen3.5:4b"]
        print(json.dumps(initialize(args), indent=2))
        return
    config = json.loads(args.config.read_text(encoding="utf-8"))
    root = Path(config["state_root"]).resolve()
    if args.command == "run":
        serve(config, root)
    else:
        queue = BookQueue(root / "queue.sqlite3")
        try:
            if args.command == "scan":
                print(json.dumps(queue.scan(Path(config["data_root"]), config["profile"], config.get("settle_seconds", 120))))
            elif args.command == "retry-failed":
                with queue.db:
                    queue.db.execute("UPDATE jobs SET state='pending',attempts=0,retry_at=0,error=NULL WHERE state='failed'")
            print(json.dumps(queue.report(), ensure_ascii=False, indent=2))
        finally:
            queue.close()


if __name__ == "__main__":
    main()
