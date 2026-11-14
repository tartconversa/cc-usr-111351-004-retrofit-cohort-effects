"""整次分析编排：快照→单元→估计→目标权重汇总→园区整群重采样→报告。

汇总口径：
- 覆盖变化序列（coverage）：每个相对月使用当时全部可识别单元，按固定目标
  园区权重汇总；覆盖度随可识别范围变化而如实报告。
- 同一组成序列（balanced）：只在整个比较窗口内均可识别的园区集合上汇总，
  样本不变，避免把园区进出样本误读为效应变化。
- 区间：以园区整体为单位的整群bootstrap，同一园区的月份绝不拆散。
"""
from __future__ import annotations
import argparse
import json
import random
from pathlib import Path
from src.retrofit_cohort_effects_input import load_bundle
from src.retrofit_cohort_effects_util import (
    check_date,
    clean,
    fingerprint_of,
    mean,
    parse_month,
    percentile,
    sample_sd,
)
from src.retrofit_cohort_effects_vintage import build_snapshot
from src.retrofit_cohort_effects_design import (
    build_cells,
    normalize_config,
    validate_adoption,
)
from src.retrofit_cohort_effects_estimators import (
    cell_effect,
    estimate_cells,
    validate_parks_attrs,
)

ASSUMPTIONS = [
    "平行趋势：若未实施改造，目标园区与合法对照园区在基期后的耗电均值变化一致"
    "（使用协变量平衡时，为给定协变量后的条件平行趋势）。",
    "改造不可逆：园区进入已改造状态后不再回到未改造状态。",
    "提前影响仅存在于配置的提前影响窗口内；窗口内月份不进基期，"
    "处于提前影响期的园区不做对照。",
    "对照资格：比较月已改造或处于提前影响期的园区一律排除，"
    "已改造园区永不作为对照，没有合法基期或对照的单元直接拒绝。",
    "信息截止：仅使用截止日或之前公布的最新修订；按月重放时另排除观测月份"
    "晚于当月的记录，提前公布也不进入当月分析。",
    "缺测即缺测：停机或退出导致的缺测不按零耗电处理，不做任何插补。",
    "产量等结果变量不参与组间调平（除非显式开启allow_outcome_adjustment并记录说明）。",
    "无溢出：对照园区的耗电不受其他园区改造影响。",
]


def _cell_like(cell):
    return {
        "event_month": cell["event_month"],
        "effect": cell["effect"],
        "control_term": cell["control_term"],
        "treated": cell["treated"],
    }


def _coverage_series(cells_like, total_target):
    by_event = {}
    for cell in cells_like:
        by_event.setdefault(cell["event_month"], []).append(cell)
    series = []
    for event_month in sorted(by_event):
        numerator = 0.0
        denominator = 0.0
        parks = set()
        for cell in by_event[event_month]:
            weight = sum(entry["weight"] for entry in cell["treated"])
            if weight <= 0:
                continue
            numerator += cell["effect"] * weight
            denominator += weight
            parks.update(entry["park"] for entry in cell["treated"])
        if denominator <= 0:
            continue
        series.append({
            "event_month": event_month,
            "effect": numerator / denominator,
            "weight": denominator,
            "coverage": denominator / total_target,
            "parks": sorted(parks),
            "n_cells": len(by_event[event_month]),
        })
    return series


def _balanced_series(cells_like, balanced_parks, window, total_target):
    series = []
    for event_month in window:
        numerator = 0.0
        denominator = 0.0
        for cell in cells_like:
            if cell["event_month"] != event_month:
                continue
            kept = [entry for entry in cell["treated"] if entry["park"] in balanced_parks]
            weight = sum(entry["weight"] for entry in kept)
            if weight <= 0:
                continue
            treated_term = sum(entry["weight"] * entry["change"] for entry in kept) / weight
            numerator += (treated_term - cell["control_term"]) * weight
            denominator += weight
        if denominator <= 0:
            series.append({"event_month": event_month, "effect": None})
            continue
        series.append({
            "event_month": event_month,
            "effect": numerator / denominator,
            "weight": denominator,
        })
    return series


def aggregate_event_study(cells, config, total_target):
    """汇总事件研究序列，返回(coverage, balanced, window, balanced_parks)。"""
    cells_like = [_cell_like(cell) for cell in cells if cell["status"] == "identified"]
    coverage = _coverage_series(cells_like, total_target)
    available = [point["event_month"] for point in coverage]
    if config["balanced_window"] is not None:
        low, high = config["balanced_window"]
        window = [e for e in available if low <= e <= high]
    else:
        window = list(available)
    balanced_parks = None
    if window:
        common = None
        for point in coverage:
            if point["event_month"] not in window:
                continue
            current = set(point["parks"])
            common = current if common is None else common & current
        balanced_parks = common or set()
    if not window or not balanced_parks:
        balanced = {
            "status": "not_identified",
            "reason": "no_common_composition",
            "detail": "比较窗口内没有在所有相对月均可识别的园区，无法给出同一组成序列",
            "window": window,
        }
    else:
        balanced = {
            "status": "ok",
            "parks": sorted(balanced_parks),
            "coverage": sum(
                config["target_weights"][park] for park in balanced_parks
            ) / total_target,
            "window": window,
            "series": _balanced_series(cells_like, balanced_parks, window, total_target),
        }
    return coverage, balanced, window, balanced_parks


def _bootstrap_rep_cells(cells, counts_treated, counts_control, config, attrs):
    """按一次重采样结果重建单元效应。"""
    rep_cells = []
    for cell in cells:
        if cell["status"] != "identified":
            continue
        treated = [
            entry for entry in cell["treated"]
            for _ in range(counts_treated.get(entry["park"], 0))
        ]
        controls = [
            entry for entry in cell["controls"]
            for _ in range(counts_control.get(entry["park"], 0))
        ]
        result = cell_effect(treated, controls, config, attrs)
        if result is None:
            continue
        rep_cells.append({
            "event_month": cell["event_month"],
            "effect": result["effect"],
            "control_term": result["control_term"],
            "treated": treated,
        })
    return rep_cells


def run_bootstrap(cells, config, attrs, total_target, window, balanced_parks, warnings):
    """园区整群bootstrap：处理池与对照池分别按园区重采样，月份随园区整体进出。"""
    reps = config["bootstrap"]["reps"]
    seed = config["bootstrap"]["seed"]
    level = config["bootstrap"]["level"]
    identified = [cell for cell in cells if cell["status"] == "identified"]
    treated_pool = sorted({entry["park"] for cell in identified for entry in cell["treated"]})
    control_pool = sorted({entry["park"] for cell in identified for entry in cell["controls"]})
    info = {
        "method": "park_cluster_bootstrap",
        "note": "以园区整体为重采样单位（处理池与对照池分层），"
                "同一园区的所有月份随之进出，绝不把月份当独立样本",
        "reps": reps,
        "seed": seed,
        "level": level,
        "treated_pool": treated_pool,
        "control_pool": control_pool,
    }
    if not treated_pool or not control_pool:
        info["skipped"] = True
        return info, {}, {}
    rng = random.Random(seed)
    coverage_draws = {}
    balanced_draws = {}
    for _ in range(reps):
        counts_treated = {}
        for _ in range(len(treated_pool)):
            park = treated_pool[rng.randrange(len(treated_pool))]
            counts_treated[park] = counts_treated.get(park, 0) + 1
        counts_control = {}
        for _ in range(len(control_pool)):
            park = control_pool[rng.randrange(len(control_pool))]
            counts_control[park] = counts_control.get(park, 0) + 1
        rep_cells = _bootstrap_rep_cells(cells, counts_treated, counts_control, config, attrs)
        for point in _coverage_series(rep_cells, total_target):
            coverage_draws.setdefault(point["event_month"], []).append(point["effect"])
        if balanced_parks:
            for point in _balanced_series(rep_cells, balanced_parks, window, total_target):
                if point["effect"] is not None:
                    balanced_draws.setdefault(point["event_month"], []).append(point["effect"])
    for event_month, draws in list(coverage_draws.items()):
        if len(draws) < max(20, reps // 2):
            warnings.append("相对月%d的有效重采样次数不足（%d/%d），区间仅供参考"
                            % (event_month, len(draws), reps))
    return info, coverage_draws, balanced_draws


def _interval(draws, level):
    if not draws:
        return None
    ordered = sorted(draws)
    alpha = (1.0 - level) / 2.0
    return {
        "lo": percentile(ordered, alpha),
        "hi": percentile(ordered, 1.0 - alpha),
        "se": sample_sd(ordered),
        "n_valid": len(ordered),
    }


def _public_entry(entry):
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _public_cell(cell):
    view = {key: value for key, value in cell.items() if not key.startswith("_")}
    view["treated"] = [_public_entry(entry) for entry in cell["treated"]]
    view["controls"] = [_public_entry(entry) for entry in cell["controls"]]
    return view


def run_analysis(bundle, cutoff=None, max_month=None):
    """执行一次完整分析，返回确定性报告（相同截止与配置重跑结果一致）。"""
    config = normalize_config(bundle["analysis"])
    cutoff = cutoff if cutoff is not None else config["cutoff"]
    check_date(cutoff)
    if max_month is not None:
        parse_month(max_month)
    warnings = []
    snapshot = build_snapshot(bundle, cutoff, max_month)
    adoption = validate_adoption(bundle, config, warnings)
    cells = build_cells(snapshot, adoption, config)
    attrs = validate_parks_attrs(bundle, cells, config)
    for field in config["secondary_outcomes"]:
        if not any(field in record for record in bundle["panel"]):
            raise ValueError("次要结果字段在资料中不存在：" + field)
    estimate_cells(cells, config, attrs, warnings)

    weights = config["target_weights"]
    total_target = sum(weights[park] for park in weights if adoption.get(park) is not None)
    never_treated_target = sorted(park for park in weights if adoption.get(park) is None)
    if never_treated_target:
        warnings.append("目标权重包含未改造园区%s，其权重不计入覆盖分母"
                        % ",".join(never_treated_target))
    if total_target <= 0:
        raise ValueError("目标权重未覆盖任何已改造园区，无法定义估计目标")

    coverage, balanced, window, balanced_parks = aggregate_event_study(
        cells, config, total_target)

    inference = {"method": None, "note": "bootstrap.reps=0，未做区间推断"}
    if config["bootstrap"]["reps"] > 0:
        inference, coverage_draws, balanced_draws = run_bootstrap(
            cells, config, attrs, total_target, window, balanced_parks, warnings)
        level = config["bootstrap"]["level"]
        for point in coverage:
            interval = _interval(coverage_draws.get(point["event_month"]), level)
            if interval:
                point["ci"] = interval
        if balanced["status"] == "ok":
            for point in balanced["series"]:
                interval = _interval(balanced_draws.get(point["event_month"]), level)
                if interval:
                    point["ci"] = interval

    balance_summary = []
    if config["covariates"]:
        for index, name in enumerate(config["covariates"]):
            before = []
            after = []
            for cell in cells:
                table = cell.get("estimation", {}).get("balance") if cell.get("estimation") else None
                if not table:
                    continue
                row = table[index]
                if row["smd_before"] is not None:
                    before.append(abs(row["smd_before"]))
                if row["smd_after"] is not None:
                    after.append(abs(row["smd_after"]))
            balance_summary.append({
                "covariate": name,
                "max_abs_smd_before": max(before) if before else None,
                "max_abs_smd_after": max(after) if after else None,
                "exceeds_threshold": bool(after) and max(after) > config["balance_threshold"],
            })

    event_grid = {}
    for cell in cells:
        event_grid.setdefault(cell["cohort"], {})[str(cell["event_month"])] = cell["effect"]
    identifiable = {}
    for cell in cells:
        if cell["status"] == "identified":
            identifiable.setdefault(cell["cohort"], []).append(cell["event_month"])

    report = {
        "report_type": "retrofit_cohort_effects",
        "fingerprint": fingerprint_of({
            "bundle": fingerprint_of(bundle),
            "cutoff": cutoff,
            "max_month": max_month,
            "config": config,
        }),
        "cutoff": cutoff,
        "max_month": max_month,
        "config": config,
        "assumptions": ASSUMPTIONS,
        "data_trace": snapshot["trace"],
        "cells": [_public_cell(cell) for cell in cells],
        "event_grid": event_grid,
        "identifiable_range": identifiable,
        "event_study": {"coverage": coverage, "balanced": balanced},
        "weights_trace": {
            "target_weights": weights,
            "treated_target_total": total_target,
            "never_treated_target": never_treated_target,
            "per_event": [
                {
                    "event_month": point["event_month"],
                    "contributing_weight": point["weight"],
                    "coverage": point["coverage"],
                }
                for point in coverage
            ],
            "balanced": (
                {"parks": balanced["parks"], "coverage": balanced["coverage"]}
                if balanced["status"] == "ok" else {"reason": balanced["reason"]}
            ),
        },
        "balance_summary": balance_summary,
        "inference": inference,
        "warnings": warnings,
    }
    if config["adjustment_note"]:
        report["adjustment_note"] = config["adjustment_note"]
    return clean(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--cutoff", default=None, help="覆盖信息截止日（YYYY-MM-DD）")
    parser.add_argument("--max-month", default=None,
                        help="排除观测月份晚于该月的记录（重放防泄漏）")
    args = parser.parse_args()
    bundle = load_bundle(args.path)
    report = run_analysis(bundle, cutoff=args.cutoff, max_month=args.max_month)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
