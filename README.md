# 电能质量离线复核平台

面向数 GB 三相录波的离线上传、不可变保存、异步分析和复核展示。系统把 **原始清单、原始块、通道标定版本和分析参数** 作为不可混用的输入，报告发布采用条件更新，Celery 至少一次投递或节点重启不会重复发布。

## 组件

- `backend/app/api`：FastAPI 接口。
- `backend/app/services/storage.py`：MinIO/S3 不可变块存储；本地文件实现用于测试。
- `backend/app/services/manifests.py`：块序号、SHA-256、字节数、通道集合、采样点和时间连续性核对。
- `backend/app/services/calibration.py`：标定版本；新版本只令旧报告进入 `review_required`，不回写旧结果。
- `backend/app/services/signals.py`：NumPy/SciPy 谐波、RMS、相位和 Fortescue 对称分量口径。
- `backend/app/services/analysis.py`：mmap/分块流式读取，显式缺口与采样率分段。
- `backend/app/services/tasks.py` + `worker.py`：Celery/Redis、租约、fence token、阶段结果、取消/重试、一次性发布。
- `frontend`：Vue 3 + ECharts 展示波形、频谱、缺口和质量状态。
- `backend/tests`：乱序补块、重复完成、重叠、缺口、采样率变化、标定更新、频谱任务崩溃重试、已知谐波和反相接线算例。

## 启动

```bash
docker compose up --build
# API: http://localhost:8000/docs
# Web: http://localhost:5173
# MinIO console: http://localhost:9001 minioadmin/minioadmin
```

本地无 Docker 时：

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
STORAGE_BACKEND=local uvicorn app.main:app --reload
celery -A app.worker.celery_app worker --loglevel=info

cd ../frontend
npm install && npm run dev
```

## 上传协议

`POST /manifests` 提交分块清单，每块声明：

- 稠密 `block_index: 0..N-1`
- 交错 little-endian 原始字节的 `byte_size` 和 `sha256`
- `start_sample`、`sample_count`、`sample_rate`、`start_timestamp`
- `dtype=float32|int16`
- 全清单固定通道集合

`PUT /manifests/{key}/blocks/{index}` 可乱序到达、重传。服务端重算 SHA-256 和交错字节数；同一内容幂等，同键不同摘要拒绝。完成时 `POST /manifests/{key}/complete`：

- 缺块/块序号不连续：fatal，不允许分析。
- 字节摘要不符：上传时拒绝。
- 样本区间重叠：fatal，不能排序后吞掉。
- 样本缺测：显式记录 gap，并在分析中以 NaN 段表示，报告为 incomplete。
- 时间戳与样本数不一致：fatal。
- 通道字节数不能按声明通道解码：fatal。
- 采样率变化：warning/质量状态，按常数采样率分段，不静默重采样。
- 重复或并发完成：同一清单状态以数据库行锁串行，只成功一次，重复调用返回 `already_completed=true`。

MinIO/S3 key 包含清单和摘要，写入使用 `If-None-Match=*`，bucket 开启版本控制。

## 任务不可混用输入

创建任务时写入 JSON 快照：

- `raw_manifest_digest` 和完整块对象/摘要
- `calibration_snapshot`（每个通道 gain/offset/phase）
- 分析参数

执行只读快照。之后启用新标定：

1. 旧报告的指标和系数不变；
2. 旧报告变为 `review_required` 并记录原因；
3. 新参数/新标定必须创建新任务、新报告。

幂等键由 `raw_manifest_digest + calibration_id + parameters` SHA-256 得到，重复请求返回同一任务。

## 计算口径

- 标定：`physical = raw * gain + offset`。
- 相位：余弦参考，`A cos(2πft + φ)` 的基波系数为 `A e^{jφ}`，RMS 相量为 `A/√2 e^{jφ}`。标定相位从测量相量中扣除：`phasor * exp(-j*phase_shift_rad)`。
- RMS：默认整数周期窗口、去除直流；`include_dc_in_rms=true` 时包含直流。
- THD：`sqrt(sum(H2..Hn RMS²)) / denominator`。
  - `denominator=rms`：THD-R，分母为总 RMS；
  - `denominator=fundamental`：THD-F，分母为基波 RMS。
- 对称分量：`a=e^{j2π/3}`，`V1=(Va+aVb+a²Vc)/3`，`V2=(Va+a²Vb+aVc)/3`，`V0=(Va+Vb+Vc)/3`，输出三相 RMS 相量和负/零序不平衡百分比。
- 频谱：分析窗口 rFFT，RMS 幅值；前端降采样显示。

## 质量状态

- 缺相/通道声明缺失或某相阶段异常：fatal；保留阶段结果和错误，但报告 `incomplete`，不得冒充完成。
- 饱和：达到原始量纲满量程阈值（默认 0.98）时 warning。
- 非整周期：不能得到完整整数周期窗口时 warning。
- 采样率变化：warning，并分段计算。
- 缺测：warning/gap，报告 incomplete。
- 少于一周期、零基波、NaN/Inf：fatal。

## 异步可靠性

- `claim_task` 对任务行加锁，使用 `fence_token`、租约和心跳；过期崩溃的 worker 会被新 worker 接管。
- `save_stage` 持续保存输入快照、读块、崩溃注入等阶段结果。
- `publish_report` 条件要求 `(id, fence_token, state=running)` 且报告尚不存在；重复 worker 返回不发布。
- 取消是协作式 fence；已发布/取消终态不能再写报告。
- Celery `acks_late=True`、`worker_prefetch_multiplier=1`，崩溃后可安全重投。
- 重试显式重置 attempt 并产生新 Celery task id；终态重复投递只返回 `lease_unavailable`。

## 合成验收

API/MinIO 启动后：

```bash
python scripts/synthetic_acceptance.py --gap --reverse-c
```

自动化测试（需先安装 requirements）：

```bash
cd backend
pytest -q
```

测试覆盖：

1. 乱序上传块 1→0→重传0，重复 complete。
2. 重叠块为 validation_failed。
3. gap + 采样率变化为 completed_with_defects，分析报告 incomplete。
4. 已知 1 Vrms 基波、5% 五次谐波，核对 THD-R/THD-F、RMS、相位。
5. C 相反相接线产生显著负序和零序不平衡。
6. 频谱阶段注入崩溃；重试后仅一份报告。
7. 标定 v2 激活后，v1 报告标为待复核，旧 Va 数值仍保持 v1 系数结果。
