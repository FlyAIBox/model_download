# AI4S model downloader

This folder contains a resumable concurrent downloader for the 64 model rows in
`AI4S_内容资源_V0.8_0630.xlsx` sheet `模型`.

## Files

- `ai4s_models_manifest.csv`: model manifest generated from the workbook.
- `download_models.py`: main scheduler and provider downloader.
- `setup_model_downloader.sh`: creates a local `.venv` and installs Python deps.
- `run_model_download.sh`: foreground entrypoint, suitable for `nohup`.
- `download_config.example.env`: credential template. Copy it to
  `download_config.env` only when credentials are needed.

## Dependency check

```bash
cd /work/home/yiziqinx/ai4s/model_download
bash setup_model_downloader.sh
./run_model_download.sh --check-deps
```

The setup script installs only into `./.venv`; it does not use `sudo` and does
not change system Python. If Python 3.8+ is missing, it bootstraps Miniforge
under `./.runtime/miniforge3` and then creates `./.venv` from that local Python.
Set `BOOTSTRAP_PYTHON=0` to disable this behavior.

## Credentials

Most public repositories do not need credentials. For private or gated
repositories, copy `download_config.example.env` to `download_config.env` and
edit the copy.

Use tokens rather than passwords:

```bash
HF_TOKEN=hf_xxx
MODELSCOPE_TOKEN=xxx
```

Username/password placeholders exist in the config file, but the downloader does
not automatically submit plaintext passwords.

## Dry run

```bash
DEST_DIR=/work/home/yiziqinx/ai4s/model ./run_model_download.sh --dry-run
```

## Background run

```bash
cd /work/home/yiziqinx/ai4s/model_download
DEST_DIR=/work/home/yiziqinx/ai4s/model \
CONCURRENCY=2 \
PER_REPO_WORKERS=8 \
RESERVE_SPACE=100G \
nohup ./run_model_download.sh > nohup_model_download_$(date +%Y%m%d_%H%M%S).log 2>&1 &
```

Defaults:

- `CONCURRENCY=2`: two repositories at the same time.
- `PER_REPO_WORKERS=8`: internal Hugging Face worker count per repository.
- `RESERVE_SPACE=100G`: stop launching/terminate active downloads when free
  space would drop to the reserve threshold.

## Progress and resume

```bash
DEST_DIR=/work/home/yiziqinx/ai4s/model ./run_model_download.sh --status
tail -f /work/home/yiziqinx/ai4s_models/_download_state/logs/model-001.log
```

State and logs are written under:

```text
/work/home/yiziqinx/ai4s_models/_download_state/
```

Important files:

- `downloads.sqlite3`: durable status database.
- `status_summary.json`: counts for total/success/failed/running/pending.
- `failed_downloads.csv`: failed model ids, URLs, errors, and log paths.
- `logs/*.log`: per-model logs.

Rerunning the same command resumes from the state database. Successful rows are
skipped. Failed and interrupted rows are retried by default. Add
`--no-retry-failed` to keep failed rows recorded without retrying, or `--force`
to reset all rows to pending.

## Server space checks

Before running, check quota and the target filesystem:

```bash
pwd
quota -s
df -h /work/home/yiziqinx
df -h /work
```

The downloader checks free space on `DEST_DIR`, so put `DEST_DIR` on the
filesystem where the model files should live.
