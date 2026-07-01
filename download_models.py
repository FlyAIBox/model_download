#!/usr/bin/env python3
"""
Concurrent resumable downloader for AI4S model manifests.
AI4S 模型清单的并发、可断点续传下载器。

The parent process schedules one repository per child process. This keeps state
tracking simple and lets the disk-space guard stop active downloads before the
filesystem is exhausted.
父进程为每个仓库启动一个子进程，便于状态追踪，并在磁盘空间不足前终止进行中的下载。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from typing import Dict, Iterable, List, Optional, Tuple


# 默认保留磁盘空间阈值；剩余空间低于此值时停止下载
DEFAULT_RESERVE = "100G"
# 调度器打印进度的默认间隔（秒）
DEFAULT_PROGRESS_INTERVAL = 30


def utcnow() -> str:
    # 返回当前 UTC 时间的 ISO 8601 字符串
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def parse_size(value: str) -> int:
    # 将人类可读的大小字符串（如 "100G"、"5GB"）解析为字节数
    text = str(value).strip().upper().replace("IB", "B")
    units = {
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "M": 1024**2,
        "MB": 1024**2,
        "G": 1024**3,
        "GB": 1024**3,
        "T": 1024**4,
        "TB": 1024**4,
    }
    for suffix in sorted(units, key=len, reverse=True):
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)].strip()) * units[suffix])
    return int(float(text))


def human_bytes(num: int) -> str:
    # 将字节数格式化为人类可读字符串（B/KB/MB/GB/TB/PB）
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1024.0 or unit == "PB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{value:.1f}PB"


def safe_name(text: str) -> str:
    # 将任意文本转为安全的文件/目录名（仅保留字母数字及 -_.，其余替换为 _）
    keep = []
    for ch in str(text):
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        elif ch in ("/", "\\", ":", " "):
            keep.append("_")
    name = "".join(keep).strip("._")
    return name[:160] or "unnamed"


def ensure_dir(path: Path) -> Path:
    # 递归创建目录（若已存在则跳过）
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_env_file(path: Optional[Path]) -> Dict[str, str]:
    # 从 .env 风格配置文件读取键值对（忽略空行与 # 注释行）
    values: Dict[str, str] = {}
    if not path:
        return values
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def merged_env(config: Dict[str, str]) -> Dict[str, str]:
    # 合并系统环境变量与配置文件，并同步各平台 Token 的别名
    env = os.environ.copy()
    for key, value in config.items():
        if value and key not in env:
            env[key] = value
    token_aliases = [
        ("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN"),
        ("HUGGINGFACE_HUB_TOKEN", "HF_TOKEN"),
        ("MODELSCOPE_TOKEN", "MODELSCOPE_API_TOKEN"),
        ("MODELSCOPE_API_TOKEN", "MODELSCOPE_TOKEN"),
        ("MODELSCOPE_ACCESS_TOKEN", "MODELSCOPE_TOKEN"),
    ]
    for dst, src in token_aliases:
        if dst not in env and env.get(src):
            env[dst] = env[src]
    return env


def load_manifest(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    required = {"id", "name", "provider", "repo_type", "repo_id", "url"}
    missing = required - set(rows[0].keys() if rows else [])
    if missing:
        raise SystemExit(f"Manifest is missing columns: {', '.join(sorted(missing))}")
    clean_rows = []
    seen = set()
    for i, row in enumerate(rows, start=1):
        rid = row.get("id") or f"item-{i:04d}"
        if rid in seen:
            raise SystemExit(f"Duplicate manifest id: {rid}")
        seen.add(rid)
        row["id"] = rid
        row["provider"] = (row.get("provider") or "").strip().lower()
        row["repo_type"] = (row.get("repo_type") or "model").strip().lower()
        row["repo_id"] = (row.get("repo_id") or "").strip()
        row["url"] = (row.get("url") or "").strip()
        clean_rows.append(row)
    return clean_rows


def connect_db(db_path: Path) -> sqlite3.Connection:
    ensure_dir(db_path.parent)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id TEXT PRIMARY KEY,
            name TEXT,
            provider TEXT,
            repo_type TEXT,
            repo_id TEXT,
            url TEXT,
            dest TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT,
            exit_code INTEGER,
            error TEXT,
            log_path TEXT
        )
        """
    )
    conn.commit()
    return conn


def upsert_manifest(
    conn: sqlite3.Connection,
    rows: List[Dict[str, str]],
    dest_dir: Path,
    force: bool,
    retry_failed: bool,
) -> None:
    for row in rows:
        dest = item_target_dir(dest_dir, row)
        conn.execute(
            """
            INSERT INTO downloads
                (id, name, provider, repo_type, repo_id, url, dest, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                provider=excluded.provider,
                repo_type=excluded.repo_type,
                repo_id=excluded.repo_id,
                url=excluded.url,
                dest=excluded.dest,
                updated_at=excluded.updated_at
            """,
            (
                row["id"],
                row.get("name", ""),
                row.get("provider", ""),
                row.get("repo_type", ""),
                row.get("repo_id", ""),
                row.get("url", ""),
                str(dest),
                utcnow(),
            ),
        )
    if force:
        conn.execute(
            """
            UPDATE downloads
            SET status='pending', started_at=NULL, finished_at=NULL,
                exit_code=NULL, error=NULL, updated_at=?
            """,
            (utcnow(),),
        )
    else:
        reset_status = ["running", "interrupted"]
        if retry_failed:
            reset_status.append("failed")
        qmarks = ",".join("?" for _ in reset_status)
        conn.execute(
            f"""
            UPDATE downloads
            SET status='pending', started_at=NULL, exit_code=NULL,
                updated_at=?
            WHERE status IN ({qmarks})
            """,
            (utcnow(), *reset_status),
        )
    conn.commit()


def item_target_dir(dest_dir: Path, row: Dict[str, str]) -> Path:
    provider = safe_name(row.get("provider", "unknown"))
    repo_type = safe_name(row.get("repo_type", "model"))
    repo_id = safe_name(row.get("repo_id") or row.get("name") or row["id"])
    return dest_dir / provider / repo_type / repo_id


def disk_free(path: Path) -> int:
    ensure_dir(path)
    return shutil.disk_usage(str(path)).free


def status_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM downloads GROUP BY status"
    ).fetchall()
    return {r["status"]: int(r["c"]) for r in rows}


def print_progress(conn: sqlite3.Connection, dest_dir: Path, reserve: int) -> None:
    counts = status_counts(conn)
    total = sum(counts.values())
    success = counts.get("success", 0)
    failed = counts.get("failed", 0)
    running = counts.get("running", 0)
    pending = counts.get("pending", 0)
    free = disk_free(dest_dir)
    reserve_note = f"reserve={human_bytes(reserve)}"
    print(
        f"[{utcnow()}] total={total} success={success} failed={failed} "
        f"running={running} pending={pending} free={human_bytes(free)} {reserve_note}",
        flush=True,
    )


def check_deps(rows: Optional[Iterable[Dict[str, str]]] = None) -> Tuple[bool, Dict[str, str]]:
    providers = {r.get("provider", "") for r in rows} if rows else {
        "huggingface",
        "modelscope",
        "github",
    }
    result: Dict[str, str] = {}
    if sys.version_info < (3, 8):
        result["python"] = f"missing: Python >= 3.8 required, got {sys.version.split()[0]}"
    else:
        result["python"] = f"ok: {sys.version.split()[0]}"
    if "huggingface" in providers:
        result["huggingface_hub"] = (
            "ok" if importlib.util.find_spec("huggingface_hub") else "missing"
        )
    if "modelscope" in providers:
        result["modelscope"] = (
            "ok" if importlib.util.find_spec("modelscope") else "missing"
        )
    if "github" in providers:
        result["git"] = "ok" if shutil.which("git") else "missing"
        result["git-lfs"] = "ok" if shutil.which("git-lfs") else "missing: optional"
    ok = not any(v == "missing" or v.startswith("missing: Python") for v in result.values())
    return ok, result


def install_deps() -> None:
    packages = ["huggingface_hub>=0.23", "modelscope>=1.14"]
    cmd = [sys.executable, "-m", "pip", "install", "-U", *packages]
    print("Installing Python dependencies:", " ".join(packages), flush=True)
    subprocess.check_call(cmd)


def write_failed_csv(conn: sqlite3.Connection, path: Path) -> int:
    rows = conn.execute(
        """
        SELECT id, name, provider, repo_type, repo_id, url, attempts, error, log_path
        FROM downloads
        WHERE status='failed'
        ORDER BY id
        """
    ).fetchall()
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        fieldnames = [
            "id",
            "name",
            "provider",
            "repo_type",
            "repo_id",
            "url",
            "attempts",
            "error",
            "log_path",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fieldnames})
    return len(rows)


def write_summary(conn: sqlite3.Connection, path: Path, dest_dir: Path, reserve: int) -> None:
    data = {
        "generated_at": utcnow(),
        "destination": str(dest_dir),
        "disk_free": disk_free(dest_dir),
        "disk_free_human": human_bytes(disk_free(dest_dir)),
        "reserve_space": reserve,
        "reserve_space_human": human_bytes(reserve),
        "counts": status_counts(conn),
    }
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_command(cmd: List[str], cwd: Optional[Path] = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)


def download_huggingface(row: Dict[str, str], dest_dir: Path, per_repo_workers: int) -> str:
    from huggingface_hub import snapshot_download

    target = item_target_dir(dest_dir, row)
    ensure_dir(target)
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    kwargs = {
        "repo_id": row["repo_id"],
        "local_dir": str(target),
        "max_workers": per_repo_workers,
        "token": token,
    }
    repo_type = row.get("repo_type") or "model"
    if repo_type != "model":
        kwargs["repo_type"] = repo_type
    revision = row.get("revision")
    if revision:
        kwargs["revision"] = revision
    path = snapshot_download(**kwargs)
    return str(path)


def call_modelscope_snapshot(repo_id: str, target: Path, revision: Optional[str]) -> str:
    try:
        from modelscope import snapshot_download
    except Exception:
        from modelscope.hub.snapshot_download import snapshot_download

    kwargs = {}
    args: List[str] = []
    params = set()
    try:
        params = set(inspect.signature(snapshot_download).parameters)
    except Exception:
        pass
    if "model_id" in params:
        kwargs["model_id"] = repo_id
    else:
        args.append(repo_id)
    if "cache_dir" in params:
        kwargs["cache_dir"] = str(target)
    if "local_dir" in params:
        kwargs["local_dir"] = str(target)
    if revision and "revision" in params:
        kwargs["revision"] = revision
    token = (
        os.environ.get("MODELSCOPE_TOKEN")
        or os.environ.get("MODELSCOPE_API_TOKEN")
        or os.environ.get("MODELSCOPE_ACCESS_TOKEN")
    )
    if token and "token" in params:
        kwargs["token"] = token
    try:
        return str(snapshot_download(*args, **kwargs))
    except TypeError:
        fallback_kwargs = {"cache_dir": str(target)}
        if revision:
            fallback_kwargs["revision"] = revision
        return str(snapshot_download(repo_id, **fallback_kwargs))


def download_modelscope(row: Dict[str, str], dest_dir: Path) -> str:
    if row.get("repo_type") not in ("", "model"):
        raise RuntimeError("ModelScope dataset downloads are not enabled in model mode")
    target = item_target_dir(dest_dir, row)
    ensure_dir(target)
    return call_modelscope_snapshot(row["repo_id"], target, row.get("revision"))


def download_github(row: Dict[str, str], dest_dir: Path) -> str:
    target = item_target_dir(dest_dir, row)
    if (target / ".git").exists():
        run_command(["git", "-C", str(target), "fetch", "--all", "--tags", "--prune"])
        run_command(["git", "-C", str(target), "pull", "--ff-only"])
    else:
        ensure_dir(target.parent)
        run_command(["git", "clone", "--recursive", row["url"], str(target)])
    if shutil.which("git-lfs"):
        try:
            run_command(["git", "-C", str(target), "lfs", "pull"])
        except subprocess.CalledProcessError as exc:
            print(f"git lfs pull failed with exit {exc.returncode}; continuing", flush=True)
    return str(target)


def child_download(args: argparse.Namespace) -> int:
    row = json.loads(Path(args.job_json).read_text(encoding="utf-8"))
    config = read_env_file(Path(args.config)) if args.config else {}
    os.environ.update(merged_env(config))
    result_path = Path(args.result_json)
    started = utcnow()
    try:
        print(f"[{started}] start {row['id']} {row.get('provider')} {row.get('repo_id')}")
        provider = row.get("provider")
        if provider == "huggingface":
            downloaded_path = download_huggingface(row, Path(args.dest_dir), args.per_repo_workers)
        elif provider == "modelscope":
            downloaded_path = download_modelscope(row, Path(args.dest_dir))
        elif provider == "github":
            downloaded_path = download_github(row, Path(args.dest_dir))
        else:
            raise RuntimeError(f"Unsupported provider: {provider}")
        result = {
            "id": row["id"],
            "ok": True,
            "started_at": started,
            "finished_at": utcnow(),
            "path": downloaded_path,
            "error": "",
        }
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{result['finished_at']}] success {row['id']} -> {downloaded_path}")
        return 0
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(error, file=sys.stderr, flush=True)
        traceback.print_exc()
        result = {
            "id": row["id"],
            "ok": False,
            "started_at": started,
            "finished_at": utcnow(),
            "path": "",
            "error": error,
        }
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1


def load_pending(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM downloads
        WHERE status='pending'
        ORDER BY id
        """
    ).fetchall()


def mark_running(conn: sqlite3.Connection, row_id: str, log_path: Path) -> None:
    conn.execute(
        """
        UPDATE downloads
        SET status='running', attempts=attempts+1, started_at=?, finished_at=NULL,
            exit_code=NULL, error=NULL, log_path=?, updated_at=?
        WHERE id=?
        """,
        (utcnow(), str(log_path), utcnow(), row_id),
    )
    conn.commit()


def mark_finished(
    conn: sqlite3.Connection,
    row_id: str,
    ok: bool,
    exit_code: int,
    error: str,
) -> None:
    conn.execute(
        """
        UPDATE downloads
        SET status=?, finished_at=?, exit_code=?, error=?, updated_at=?
        WHERE id=?
        """,
        ("success" if ok else "failed", utcnow(), exit_code, error, utcnow(), row_id),
    )
    conn.commit()


def mark_interrupted(conn: sqlite3.Connection, row_id: str, error: str) -> None:
    conn.execute(
        """
        UPDATE downloads
        SET status='interrupted', finished_at=?, exit_code=-15, error=?, updated_at=?
        WHERE id=?
        """,
        (utcnow(), error, utcnow(), row_id),
    )
    conn.commit()


def row_from_db(row: sqlite3.Row) -> Dict[str, str]:
    keys = ["id", "name", "provider", "repo_type", "repo_id", "url"]
    return {k: row[k] or "" for k in keys}


def terminate_process(proc: subprocess.Popen, timeout: int = 20) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def scheduler(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest).expanduser().resolve()
    dest_dir = Path(args.dest_dir).expanduser().resolve()
    state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else dest_dir / "_download_state"
    log_dir = Path(args.log_dir).expanduser().resolve() if args.log_dir else state_dir / "logs"
    jobs_dir = ensure_dir(state_dir / "jobs")
    results_dir = ensure_dir(state_dir / "results")
    ensure_dir(log_dir)
    ensure_dir(dest_dir)

    rows = load_manifest(manifest)
    if args.install_deps:
        install_deps()
    ok, deps = check_deps(rows)
    print("Dependency check:")
    for name, status in deps.items():
        print(f"  {name}: {status}")
    if args.check_deps:
        return 0 if ok else 2
    if not ok and not args.skip_dep_check:
        print("Missing required dependencies. Run with --install-deps or use setup_model_downloader.sh.")
        return 2

    reserve = parse_size(args.reserve_space)
    start_buffer = parse_size(args.start_buffer)
    conn = connect_db(state_dir / "downloads.sqlite3")
    upsert_manifest(conn, rows, dest_dir, args.force, not args.no_retry_failed)

    if args.status:
        print_progress(conn, dest_dir, reserve)
        failed_csv = state_dir / "failed_downloads.csv"
        failed_count = write_failed_csv(conn, failed_csv)
        print(f"failed_count={failed_count} failed_csv={failed_csv}")
        return 0

    if args.dry_run:
        print_progress(conn, dest_dir, reserve)
        for row in load_pending(conn):
            print(f"would download: {row['id']} {row['provider']} {row['repo_id']} -> {row['dest']}")
        return 0

    free = disk_free(dest_dir)
    if free <= reserve:
        print(
            f"Refusing to start: free={human_bytes(free)} <= reserve={human_bytes(reserve)}",
            file=sys.stderr,
        )
        return 3

    config_path = Path(args.config).expanduser().resolve() if args.config else None
    config = read_env_file(config_path)
    env = merged_env(config)
    running: Dict[str, Dict[str, object]] = {}
    last_progress = 0.0
    exit_code = 0

    try:
        while True:
            for row_id, info in list(running.items()):
                proc = info["proc"]
                assert isinstance(proc, subprocess.Popen)
                rc = proc.poll()
                if rc is None:
                    continue
                log_handle = info.get("log_handle")
                if log_handle:
                    log_handle.close()
                result_path = Path(str(info["result_path"]))
                ok_result = False
                error = f"child exited with code {rc}"
                if result_path.exists():
                    try:
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                        ok_result = bool(result.get("ok")) and rc == 0
                        error = result.get("error") or error
                    except Exception as exc:
                        error = f"could not parse result json: {exc}"
                mark_finished(conn, row_id, ok_result, int(rc), error)
                del running[row_id]

            free = disk_free(dest_dir)
            if free <= reserve:
                msg = (
                    f"disk guard triggered: free={human_bytes(free)} "
                    f"<= reserve={human_bytes(reserve)}"
                )
                print(msg, file=sys.stderr, flush=True)
                for row_id, info in list(running.items()):
                    proc = info["proc"]
                    assert isinstance(proc, subprocess.Popen)
                    terminate_process(proc)
                    mark_interrupted(conn, row_id, msg)
                    log_handle = info.get("log_handle")
                    if log_handle:
                        log_handle.close()
                    del running[row_id]
                exit_code = 3
                break

            pending = load_pending(conn)
            while pending and len(running) < args.concurrency:
                free = disk_free(dest_dir)
                if free <= reserve + start_buffer:
                    print(
                        f"Pausing launch: free={human_bytes(free)} "
                        f"<= reserve+buffer={human_bytes(reserve + start_buffer)}",
                        flush=True,
                    )
                    break
                db_row = pending.pop(0)
                row = row_from_db(db_row)
                job_path = jobs_dir / f"{row['id']}.json"
                result_path = results_dir / f"{row['id']}.json"
                log_path = log_dir / f"{row['id']}.log"
                job_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
                if result_path.exists():
                    result_path.unlink()
                mark_running(conn, row["id"], log_path)
                child_cmd = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "_download_one",
                    "--job-json",
                    str(job_path),
                    "--result-json",
                    str(result_path),
                    "--dest-dir",
                    str(dest_dir),
                    "--per-repo-workers",
                    str(args.per_repo_workers),
                ]
                if config_path:
                    child_cmd.extend(["--config", str(config_path)])
                log_handle = log_path.open("ab")
                proc = subprocess.Popen(
                    child_cmd,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
                running[row["id"]] = {
                    "proc": proc,
                    "log_handle": log_handle,
                    "result_path": result_path,
                }
                print(f"started {row['id']} pid={proc.pid} log={log_path}", flush=True)

            now = time.time()
            if now - last_progress >= args.progress_interval:
                print_progress(conn, dest_dir, reserve)
                last_progress = now

            if not running and not load_pending(conn):
                break
            time.sleep(2)
    except KeyboardInterrupt:
        print("Interrupted by user; terminating active downloads.", file=sys.stderr)
        for row_id, info in list(running.items()):
            proc = info["proc"]
            assert isinstance(proc, subprocess.Popen)
            terminate_process(proc)
            mark_interrupted(conn, row_id, "interrupted by user")
            log_handle = info.get("log_handle")
            if log_handle:
                log_handle.close()
        exit_code = 130

    print_progress(conn, dest_dir, reserve)
    failed_csv = state_dir / "failed_downloads.csv"
    failed_count = write_failed_csv(conn, failed_csv)
    write_summary(conn, state_dir / "status_summary.json", dest_dir, reserve)
    if failed_count:
        print(f"Failed downloads recorded in: {failed_csv}")
        if exit_code == 0:
            exit_code = 1
    print(f"State database: {state_dir / 'downloads.sqlite3'}")
    print(f"Summary JSON: {state_dir / 'status_summary.json'}")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Concurrent AI4S model downloader")
    sub = parser.add_subparsers(dest="command")
    child = sub.add_parser("_download_one")
    child.add_argument("--job-json", required=True)
    child.add_argument("--result-json", required=True)
    child.add_argument("--dest-dir", required=True)
    child.add_argument("--config")
    child.add_argument("--per-repo-workers", type=int, default=8)

    parser.add_argument("--manifest", default="ai4s_models_manifest.csv")
    parser.add_argument("--dest-dir", required=False, default="./models")
    parser.add_argument("--state-dir")
    parser.add_argument("--log-dir")
    parser.add_argument("--config", default="./download_config.env")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--per-repo-workers", type=int, default=8)
    parser.add_argument("--reserve-space", default=DEFAULT_RESERVE)
    parser.add_argument("--start-buffer", default="5G")
    parser.add_argument("--progress-interval", type=int, default=DEFAULT_PROGRESS_INTERVAL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--force", action="store_true", help="reset all manifest rows to pending")
    parser.add_argument("--no-retry-failed", action="store_true")
    parser.add_argument("--skip-dep-check", action="store_true")
    parser.add_argument("--check-deps", action="store_true")
    parser.add_argument("--install-deps", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "_download_one":
        return child_download(args)
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.per_repo_workers < 1:
        parser.error("--per-repo-workers must be >= 1")
    return scheduler(args)


if __name__ == "__main__":
    raise SystemExit(main())
