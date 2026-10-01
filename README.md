# 电能质量离线复核平台

面向电气实验室数 GB 级三相录波的离线复核系统。核心原则是：**原始块不可变、完成核对原子化、分析启动时冻结清单/标定/参数、失败只给诊断不冒充报告、重复执行最多发布一次**。

## 组成

- `backend/`：FastAPI、SQLAlchemy、NumPy/SciPy、Celery/Redis。
- `frontend/`：Vue 3 + ECharts，展示波形、谐波、对称分量和缺块。
- MinIO：内容寻址原始块，key 为 `raw/{manifest}/{sequence}/{sha256}.bin`，应用不覆盖。生产部署应在桶上启用 Object Lock/compliance retention；代码对同 key 异名内容也会报 `ImmutableObjectError`。
- PostgreSQL：清单、块元数据、校验问题、标定版本、任务租约、报告。
- Docker Compose：PostgreSQL、Redis、MinIO、API、Celery worker、Vite。

## 启动

```bash
docker compose up --build
# API: http://localhost:8000/docs
# UI:  http://localhost:5173
```

本地无外部依赖的测试模式默认使用 SQLite 和内存对象库：

```bash
cd backend
pytest -q
```

## 上传与完成协议

1. `POST /manifests` 提交完整分块清单。创建时即检查：
   - 序号唯一且连续；
   - 字节范围无重叠、无空洞；
   - 每块 `(sample_count-1)/sample_rate` 与起止时间一致；
   - 相邻块时间为上一块最后一个采样点后一个采样周期；
   - 所有块通道集合一致。
2. `PUT /manifests/{id}/chunks/{sequence}/raw` 支持乱序和断点补传。服务端落临时文件时计算 SHA-256，摘要或长度不符立即拒绝。
3. `POST /manifests/{id}/finalize` 原子地把 `open -> validating -> completed/open`。再次核对：
   - 缺块、多块；
   - 元数据/摘要/字节范围；
   - 采样时间连续性；
   - 通道集合；
   - 重叠与缺测区间会生成明确 issue，不靠排序吞掉。
4. 完成调用并发时，只有一个条件更新能把 `open` 改为 `validating`。已完成后的重复调用返回 `already_completed=true`。
5. 采样率变化不是数据拼接错误，但会给出 `sample_rate_changed` warning，并在分析中形成独立固定采样率段，不做插值。

## 分析冻结与标定版本

创建任务时保存 `manifest_snapshot`：

- manifest id 与 manifest digest；
- 固定的有序块清单；
- 通道集合及 hash；
- 标定版本 id 与完整系数；
- 完整分析参数。

计算只使用快照中的系数和参数。之后发布新标定版本：

- 旧报告不重算、不改数；
- 旧报告自动变为 `needs_review`，记录替代标定版本；
- 需要新结果时创建新任务、新报告，绝不在同一结果中混用新旧系数。

## 数值口径

详见 [`docs/analysis-conventions.md`](docs/analysis-conventions.md)。摘要：

- 标定：`y = gain*x + offset`，另有常数相位偏移 `phase_shift_rad`；反相接线可写为 `gain=-1` 或 `phase=π`。
- RMS：校准后时间域真实 RMS，包含直流：`sqrt(mean(x²))`。
- 基波相位：DFT 单频-bin 系数相位，对应 `x=A cos(2πf t+φ)`。
- THD：`sqrt(sum(RMS_2²..RMS_N²)) / RMS_1 × 100%`。
- 对称分量：基波 RMS 相量，Fortescue：
  - `V1=(Va+aVb+a²Vc)/3`
  - `V2=(Va+a²Vb+aVc)/3`
  - `V0=(Va+Vb+Vc)/3`

## 质量状态

- `ok`：无问题。
- `warning`：可发布但需复核，例如非整周期尾段、采样率变化、轻微饱和。
- `error`：生成 `diagnostic_failed` 诊断结果且不发布正常完成报告，例如缺相、基波缺失、必要标定缺失。

阶段结果持续写入 `stage_results`（快照、原始块清单、字节装载、分段、频谱）。频谱阶段崩溃后可诊断，但不会存在已发布报告。

## 任务可靠性

- 任务状态：`queued/running/succeeded/failed/retry_wait/cancelled`。
- 租约字段：`lease_owner`、`lease_until`、`heartbeat_at`。
- worker 重启后，过期租约由 `/maintenance/recover-stale-tasks` 或新 worker 条件更新接管。
- `reports.task_id` 唯一，发布前再次检查；任务重试不会替换或重复发布旧报告。
- Celery 配置 `acks_late`、单 worker prefetch 1，并以数据库租约作为最终防重边界。

## 合成验收

`backend/tests` 覆盖：

- 乱序补块、重复上传、缺块、重叠声明、摘要错误；
- 8 线程并发完成同一清单；
- 已知 3/5 次谐波的 THD、真实 RMS；
- 平衡三相正/负/零序；
- A 相反相接线：负序 200、零序 100、正序 100；
- 标定更新后旧报告 `needs_review`；
- 频谱任务崩溃、过期 worker 租约恢复与重试不重复发布；
- 缺相、饱和、非整周期、采样率变化的质量状态。
