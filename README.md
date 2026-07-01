# AI4S 模型下载器

> 中文文档（默认）。English documentation: [README.en.md](README.en.md)

本目录包含针对 `AI4S_内容资源_V0.8_0630.xlsx` 工作表 `模型` 中 64 条模型记录的可断点续传、并发下载工具。

## 文件说明

| 文件 | 说明 |
|------|------|
| `ai4s_models_manifest.csv` | 从 Excel 工作簿生成的模型清单 |
| `download_models.py` | 主调度器与各平台（Hugging Face / ModelScope / GitHub）下载逻辑 |
| `setup_model_downloader.sh` | 创建本地 `.venv` 并安装 Python 依赖 |
| `run_model_download.sh` | 前台入口脚本，适合配合 `nohup` 后台运行 |
| `download_config.example.env` | 凭据配置模板；仅在需要时复制为 `download_config.env` |

## 依赖检查

```bash
cd /work/home/yiziqinx/ai4s/model_download
bash setup_model_downloader.sh
./run_model_download.sh --check-deps
```

安装脚本**仅**向 `./.venv` 写入依赖，不使用 `sudo`，也不会修改系统 Python。若本机缺少 Python 3.8+，会在 `./.runtime/miniforge3` 下引导安装 Miniforge，再用该本地 Python 创建 `./.venv`。设置 `BOOTSTRAP_PYTHON=0` 可禁用此行为。

## 凭据配置

大多数公开仓库无需凭据。对于私有或需授权的仓库，将 `download_config.example.env` 复制为 `download_config.env` 并编辑。

**请使用 Token，而非明文密码：**

```bash
HF_TOKEN=hf_xxx
MODELSCOPE_TOKEN=xxx
```

配置文件中保留了用户名/密码占位符，但下载器**不会**自动提交明文密码。

## 试运行（Dry Run）

不实际下载，仅列出待下载任务并显示当前状态：

```bash
DEST_DIR=/work/home/yiziqinx/ai4s/model ./run_model_download.sh --dry-run
```

## 后台运行

```bash
cd /work/home/yiziqinx/ai4s/model_download
DEST_DIR=/work/home/yiziqinx/ai4s/model \
CONCURRENCY=2 \
PER_REPO_WORKERS=8 \
RESERVE_SPACE=100G \
nohup bash ./run_model_download.sh > nohup_model_download_$(date +%Y%m%d_%H%M%S).log 2>&1 &
```

### 默认参数

| 环境变量 | 默认值 | 含义 |
|----------|--------|------|
| `CONCURRENCY` | `2` | 同时下载的仓库数量 |
| `PER_REPO_WORKERS` | `8` | 每个 Hugging Face 仓库内部的并行 worker 数 |
| `RESERVE_SPACE` | `100G` | 磁盘剩余空间低于此阈值时，停止启动新任务并终止进行中的下载 |

## 网络访问

若 `huggingface.co` 不通，但 `hf-mirror.com` 可用，可通过 `HF_ENDPOINT` 使用镜像继续下载（断点续传同样有效）：

```bash
cd /work/home/yiziqinx/ai4s/model_download

HF_ENDPOINT=https://hf-mirror.com \
DEST_DIR=/work/home/yiziqinx/ai4s/model \
CONCURRENCY=1 \
PER_REPO_WORKERS=2 \
RESERVE_SPACE=100G \
nohup ./run_model_download.sh > nohup_model_download_hfmirror_$(date +%Y%m%d_%H%M%S).log 2>&1 &
```

若两个站点均无法访问，需在服务器上配置 HTTP/HTTPS 代理，例如：

```bash
export HTTPS_PROXY=http://你的代理地址:端口
export HTTP_PROXY=http://你的代理地址:端口
```

然后再执行同样的恢复/续传命令（可保留 `HF_ENDPOINT`，或省略以使用 Hugging Face 官方地址）。

## 进度查看与断点续传

### 查看正在运行的任务

**下载进行中时，请勿使用** `run_model_download.sh --status`——该命令会刷新状态。应直接查询进程列表和 SQLite 数据库。

检查调度器是否仍在运行：

```bash
ps -u "$USER" -o pid,ppid,stat,etime,pcpu,pmem,cmd | grep -E 'run_model_download|download_models.py' | grep -v grep
```

后台运行中通常可见：

- **父进程**（调度器）：

```text
python .../download_models.py --manifest ...
```

- **子进程**（单模型下载）：

```text
python .../download_models.py _download_one ...
```

从状态数据库查询实时计数：

```bash
cd /work/home/yiziqinx/ai4s/model_download
./.venv/bin/python - <<'PY'
import sqlite3
p='/work/home/yiziqinx/ai4s/model/_download_state/downloads.sqlite3'
con=sqlite3.connect(p)
for status, count in con.execute("select status, count(*) from downloads group by status"):
    print(status, count)
PY
```

跟踪主日志：

```bash
cd /work/home/yiziqinx/ai4s/model_download
tail -f nohup_model_download_*.log
```

跟踪单个模型的日志：

```bash
tail -f /work/home/yiziqinx/ai4s/model/_download_state/logs/model-001.log
```

下载**全部结束后**，也可使用：

```bash
cd /work/home/yiziqinx/ai4s/model_download
DEST_DIR=/work/home/yiziqinx/ai4s/model ./run_model_download.sh --status
```

### 状态与日志目录

状态与日志写入：

```text
/work/home/yiziqinx/ai4s/model/_download_state/
```

重要文件：

| 文件 | 说明 |
|------|------|
| `downloads.sqlite3` | 持久化状态数据库 |
| `status_summary.json` | 总数 / 成功 / 失败 / 运行中 / 待处理 统计 |
| `failed_downloads.csv` | 失败模型的 ID、URL、错误信息与日志路径 |
| `logs/*.log` | 每个模型独立的下载日志 |

### 续传与重试

重复执行相同命令会从状态数据库断点续传：

- **已成功**的行会被跳过
- **失败**和**中断**的行默认会重试
- 添加 `--no-retry-failed`：保留失败记录但不重试
- 添加 `--force`：将所有行重置为待处理（pending）

## 服务器磁盘空间检查

运行前请确认配额与目标文件系统：

```bash
pwd
quota -s
df -h /work/home/yiziqinx
df -h /work
```

下载器会检查 `DEST_DIR` 所在文件系统的剩余空间，因此请将 `DEST_DIR` 设置在模型文件实际存放的文件系统上。
