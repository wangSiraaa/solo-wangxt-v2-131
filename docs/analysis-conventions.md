# 分析口径

## 1. 原始数据

上传块编码为 little-endian `float32` interleaved，例如三通道样本顺序为 `Va,Vb,Vc,Va,Vb,Vc,...`。块元数据声明：

- `sequence`
- `byte_offset`, `byte_length`
- `sample_count`, `sample_rate`
- `channels`
- `start_time`, `end_time`
- `sha256`

块时间戳按“最后一个采样点”定义，因此相邻块的时间差应等于 `1/sample_rate`，而不是整块时长。

## 2. 标定

每个通道：

```text
y = gain * x + offset
```

`phase_shift_rad` 是频域常数相位校准：

```text
Y[k] = X[k] * exp(-j*phase_shift_rad), k > 0
```

极性反接可由以下任一方式表达：

- `gain = -1`
- `phase_shift_rad = π`

任务只保存创建时选定版本的完整系数。标定版本后续变化不会修改旧结果。

## 3. 窗长与非整周期

默认：

- 基波：50 Hz
- 每窗：6 个基波周期
- 最大谐波：15 次

窗长：

```text
N = round(cycles_per_window * fs / f0)
```

完整窗逐段分析。不足一整窗的尾段不会被静默补零或拉伸：

- 若整段不足一窗，标记 `non_integer_cycle` warning，并对现有样本做诊断；
- 若只留下尾段，尾段不进入指标，标记 `non_integer_cycle` warning。

基波 DFT bin 与标称频率偏差超过 `fundamental_tolerance` 时给出 `fundamental_off_bin`。本平台不偷偷做重采样；需要变录波频率分析时另建任务/参数。

## 4. RMS

校准后时间域真实 RMS：

```text
RMS = sqrt( (1/N) * Σ y[n]^2 )
```

因此包含直流和所有频带能量，不受 DFT 窗泄漏影响。直流单独输出：

```text
DC = mean(y)
```

## 5. 谐波、基波相位与 THD

信号模型：

```text
y[n] = A_h cos(2πh f0 n/fs + φ_h)
```

SciPy `rfft` 的非零频系数满足：

```text
phasor_h = Y[h*k0] / N
A_h      = 2 * |phasor_h|
RMS_h    = A_h / sqrt(2)
φ_h      = angle(phasor_h)
```

THD-RMS 分母明确为基波 RMS：

```text
THD% = sqrt(Σ_{h=2}^{H} RMS_h²) / RMS_1 × 100%
```

该口径与 IEEE/常见 THD 定义一致；分母不是总 RMS，也不是基波峰值。

## 6. 对称分量

先把各相基波表示为峰值相量：

```text
Va = A_a exp(jφ_a)
Vb = A_b exp(jφ_b)
Vc = A_c exp(jφ_c)
a  = exp(j 2π/3)
```

Fortescue 分量：

```text
V0 = (Va + Vb  + Vc ) / 3
V1 = (Va + aVb + a²Vc) / 3
V2 = (Va + a²Vb + aVc) / 3
```

接口输出峰值相量及对应 RMS：`|Vsequence|/sqrt(2)`。不平衡度：

```text
V2/V1 × 100%
```

缺 A/B/C 任一相时，对称分量阶段状态为 failed，整份报告进入 `diagnostic_failed`，不发布为完成报告。

## 7. 饱和与缺相

- 饱和阈值来自固定标定：`saturation_low/saturation_high`。
- 达到阈值的样本比例高于参数 `saturation_warning_fraction` 时 warning；超过 50% 时 error。
- 缺相基于通道集合及通道名（Va/Vb/Vc、Ia/Ib/Ic、L1/L2/L3 等）识别。电压和电流分别计算。

## 8. 采样率变化

完成清单时允许同一录波声明不同采样率，但必须在块边界发生并保持时间连续。分析阶段：

- 按连续相同 `sample_rate` 分段；
- 不做插值、不跨采样率做 FFT；
- 每段独立输出 RMS、谐波、对称分量；
- 汇总质量带 `sample_rate_changed` warning。
