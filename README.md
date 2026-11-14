# 分期节能改造的可识别队列效应分析

面向分期上线面板的节能改造效果分析。业务方可按**上线队列 × 相对月份**查看节电变化，
并明确哪些队列单元实际上缺乏可比对照。模块先固定信息截止日，再对各条记录取当时
已公布的最新修订；按月滚动重放时收紧到该月信息截止，未来月份的观测即使提前公布
也不进入当月分析，杜绝全期数据预处理向早期比较泄漏。

## 方法概览

- **队列与相对月**：以改造生效月定义队列 g，相对月 e = 比较月 t − g，只估计 e ≥ 0。
- **基期**：g 之前、提前影响窗口之外最近的 `base_months` 个月，须全部有观测，
  否则该单元被拒绝（`missing_base`）。
- **对照资格**：比较月已改造（`already_treated`）或处于提前影响期
  （`in_anticipation`）的园区一律排除；已改造园区**永不**作为对照，
  没有合法基期或合法对照的单元直接拒绝，不做任何退回。
- **估计器**：
  - `did`（默认）：处理侧按目标权重加权、对照侧等权的双向差分；
  - `did_ipw`：以园区属性拟合倾向得分，对照按 p/(1−p) 加权，
    倾向得分高于 1−`support_epsilon` 的对照判定为支撑外并剔除，
    剔除后对照不足时单元降级为不可识别。
- **汇总**：按配置中明确且固定的目标园区权重汇总到每个相对月，同时给出
  - 覆盖变化序列（coverage）：使用当时全部可识别单元，覆盖度如实报告；
  - 同一组成序列（balanced）：只在整个窗口内均可识别的园区集合上汇总，
    样本不变，避免把园区进出样本误读为效应变化。
- **区间**：园区整群 bootstrap（处理池与对照池分层），同一园区的月份绝不拆散。
- **诊断**：支撑判定、极端权重（最大份额、有效样本量）、协变量平衡（SMD）
  全部随单元输出并触发告警。

## 识别假设

1. 平行趋势：若未实施改造，目标园区与合法对照园区在基期后的耗电均值变化一致
   （使用协变量平衡时为条件平行趋势）。
2. 改造不可逆：进入已改造状态后不再回到未改造状态。
3. 提前影响仅存在于配置的窗口内；窗口内月份不进基期、不做对照。
4. 信息截止一致：仅用截止日前已公布的修订；重放时排除未来月份观测。
5. 缺测即缺测：停机或退出导致的缺测不按零耗电处理，不做任何插补。
6. 产量等结果变量不参与组间调平（除非显式开启并记录说明）。
7. 无溢出：对照园区的耗电不受其他园区改造影响。

## 资料结构

```json
{
  "schema_version": 1,
  "panel": [{"park": "A", "month": "2026-08", "published": "2026-09-05",
             "revision": 1, "energy": 10, "output": 100}],
  "parks": {"A": {"size": 12.0}},
  "adoption": {"A": "2026-09", "B": null},
  "analysis": {"cutoff": "2026-11-01", "anticipation_months": 0,
               "target_weights": {"A": 1}}
}
```

- `panel`：一园区一月份的耗电（`energy`）与可选产出（`output`）、公布日、修订号；
  修订保留旧值，同一（园区, 月份）取截止日前已公布的最大修订号。
- `parks`（可选）：园区属性，作为 `did_ipw` 协变量与平衡诊断来源。
- `adoption`：园区改造生效月（YYYY-MM）或 null（未改造）；面板中出现的园区
  必须全部列出，缺失即报错，不推断。
- `analysis`：分析配置，见下。

### 配置项（analysis）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `cutoff` | 必填 | 信息截止日 YYYY-MM-DD |
| `target_weights` | 必填 | 目标园区权重（正数），汇总口径固定不变 |
| `anticipation_months` | 0 | 提前影响窗口长度（月） |
| `base_months` | 1 | 基期月数 |
| `estimator` | `did` | `did` 或 `did_ipw` |
| `covariates` | [] | 园区属性协变量（`did_ipw` 必需） |
| `min_controls` | 1 | 单元所需最少合法对照数 |
| `support_epsilon` | 0.05 | 支撑判定阈值 |
| `max_weight_share` | 0.8 | 单一对照权重份额告警阈值 |
| `balance_threshold` | 0.1 | 加权后 SMD 告警阈值 |
| `bootstrap` | reps=500, seed=20261114, level=0.9 | 园区整群重采样 |
| `event_window` | null | 报告的相对月范围 {min, max} |
| `balanced_window` | null | 同一组成序列的比较窗口 [min, max] |
| `secondary_outcomes` | [] | 次要结果字段（如 output），仅描述，不调平 |
| `allow_outcome_adjustment` + `adjustment_note` | 关闭 | 结果变量调平的显式开关与说明 |

把 `output`/`energy` 等结果变量列入 `covariates` 会被拒绝，除非显式开启
`allow_outcome_adjustment` 并在 `adjustment_note` 中说明；说明会写入报告。

## 运行

单次分析（输出完整 JSON 报告）：

    python -m src.retrofit_cohort_effects_analysis fixtures/sample.json
    python -m src.retrofit_cohort_effects_analysis fixtures/sample.json --cutoff 2026-10-31

按月滚动重放（每月收紧信息截止并排除未来月份观测）：

    python -m src.retrofit_cohort_effects_replay fixtures/case_staggered_publication.json \
        --months 2026-10,2026-11,2026-12

报告包含：`data_trace`（修订选择、被取代旧值、因公布日/未来月份被排除的记录）、
`cells`（每个队列×相对月的处理/对照名单与排除原因）、`event_study`（覆盖变化与
同一组成两条序列及区间）、`weights_trace`（目标权重与逐月覆盖度）、
`balance_summary`、`inference`、`warnings` 与 `fingerprint`。
同一截止与配置重跑结果完全一致（指纹相同）。

## 演示案例（fixtures/）

- `sample.json`：最小 worked example。A 从 10 降到 8、合法对照 B 从 20 降到 19，
  两期差中差为 **−1**（而非 −11）。
- `case_invalid_control.json`：无效对照。B 在比较月已改造或处于提前影响期时被
  排除（`already_treated` / `in_anticipation`），绝不退回用已改造组当对照。
- `case_last_cohort.json`：只剩最后一批。全部园区最终都改造时，最后一批永远没有
  合法对照（单元不可识别）；可识别覆盖度随相对月收缩到 0，同一组成序列与覆盖
  变化序列的分歧一目了然。
- `case_staggered_publication.json`：错开公布日。各园区公布日不同、记录被修订，
  重放不同月份得到不同信息集；提前公布的未来月份观测被当月分析排除。
- `case_attrition.json`：园区退出。T2 在 2026-10 后停止上报，覆盖变化序列在
  e=2 处从 −2.5 跳到 −1.0 纯属样本组成变化；同一组成序列（仅 T1）全程为 −1.0。

## 运行检查

    python -m unittest discover -s tests -v
    python -m compileall -q src tests

此版本没有第三方运行依赖。读取入口仍为
`python -m src.retrofit_cohort_effects_input fixtures/sample.json`。

## 局限

- 估计为队列×相对月的双向差分（可选 IPW 平衡），未实现更复杂的插补类估计器。
- 倾向得分为线性逻辑回归；小样本下可能分离，此时以诊断告警而非静默处理。
- 区间基于百分位 bootstrap，未做多重检验校正。
