"""分析设计与队列×相对月单元构建。

核心规则：
- 基期：改造生效月之前、提前影响窗口之外的最近 base_months 个月，须全部有观测。
- 对照资格：比较月不得已改造、不得处于提前影响期；已改造园区永不作为对照，
  没有合法基期或合法对照的单元直接拒绝，不做任何退回或插补。
- 处理侧只保留目标权重覆盖的园区（目标总体即估计对象）；对照侧不受目标权重限制。
- 缺测即缺测：停机或退出导致的缺测不按零耗电处理。
"""
from __future__ import annotations
from src.retrofit_cohort_effects_util import (
    check_date,
    is_number,
    mean,
    month_add,
    month_diff,
    parse_month,
)

ESTIMATORS = ("did", "did_ipw")
OUTCOME_NAMES = ("output", "energy", "产量", "耗电")


class NoDataError(ValueError):
    """截止日前无可用观测：重放早期月份的合法状态，与配置错误区分。"""

DEFAULTS = {
    "anticipation_months": 0,
    "base_months": 1,
    "estimator": "did",
    "covariates": [],
    "min_controls": 1,
    "support_epsilon": 0.05,
    "max_weight_share": 0.8,
    "balance_threshold": 0.1,
    "bootstrap": {"reps": 500, "seed": 20261114, "level": 0.9},
    "event_window": None,
    "secondary_outcomes": [],
    "allow_outcome_adjustment": False,
    "adjustment_note": None,
    "balanced_window": None,
}

_BOOTSTRAP_KEYS = ("reps", "seed", "level")


def _check_int(value, name, minimum):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError("配置项%s必须为不小于%d的整数" % (name, minimum))
    return value


def _check_probability(value, name, low, high, low_open=False, high_open=False):
    if not is_number(value) or not (low < value if low_open else low <= value) \
            or not (value < high if high_open else value <= high):
        raise ValueError("配置项%s超出合法范围" % name)
    return value


def normalize_config(analysis):
    """校验并补全analysis配置，未知字段直接拒绝。"""
    if not isinstance(analysis, dict):
        raise ValueError("analysis字段必须为对象")
    unknown = sorted(set(analysis) - set(DEFAULTS) - {"cutoff", "target_weights"})
    if unknown:
        raise ValueError("未知配置项：" + ",".join(unknown))
    config = dict(DEFAULTS)
    config.update({key: value for key, value in analysis.items() if key in DEFAULTS})

    if "cutoff" not in analysis:
        raise ValueError("缺少配置项：cutoff（信息截止日）")
    check_date(analysis["cutoff"])
    config["cutoff"] = analysis["cutoff"]

    weights = analysis.get("target_weights")
    if not isinstance(weights, dict) or not weights:
        raise ValueError("配置项target_weights必须为非空对象")
    for park, weight in weights.items():
        if not isinstance(park, str) or not park:
            raise ValueError("目标权重的园区标识必须为非空字符串")
        if not is_number(weight) or weight <= 0:
            raise ValueError("目标权重必须为正数：" + repr(park))
    config["target_weights"] = dict(weights)

    config["anticipation_months"] = _check_int(config["anticipation_months"], "anticipation_months", 0)
    config["base_months"] = _check_int(config["base_months"], "base_months", 1)
    config["min_controls"] = _check_int(config["min_controls"], "min_controls", 1)

    if config["estimator"] not in ESTIMATORS:
        raise ValueError("未知估计器：" + repr(config["estimator"]))
    covariates = config["covariates"]
    if not isinstance(covariates, list) or not all(isinstance(c, str) and c for c in covariates):
        raise ValueError("配置项covariates必须为字符串列表")
    if len(set(covariates)) != len(covariates):
        raise ValueError("配置项covariates存在重复")
    if config["estimator"] == "did_ipw" and not covariates:
        raise ValueError("did_ipw估计器需要至少一个协变量")

    allow = config["allow_outcome_adjustment"]
    if not isinstance(allow, bool):
        raise ValueError("配置项allow_outcome_adjustment必须为布尔值")
    note = config["adjustment_note"]
    if note is not None and (not isinstance(note, str) or not note.strip()):
        raise ValueError("配置项adjustment_note必须为非空字符串或null")
    if allow and not note:
        raise ValueError("开启allow_outcome_adjustment必须提供adjustment_note说明")
    for name in covariates:
        if name in OUTCOME_NAMES and not (allow and note):
            raise ValueError(
                "协变量%s属于结果变量，可能受改造影响，不得用于组间调平；"
                "如确需使用，请设置allow_outcome_adjustment并在adjustment_note中说明" % name
            )

    config["support_epsilon"] = _check_probability(
        config["support_epsilon"], "support_epsilon", 0.0, 0.5, high_open=True)
    config["max_weight_share"] = _check_probability(
        config["max_weight_share"], "max_weight_share", 0.0, 1.0, low_open=True)
    config["balance_threshold"] = _check_probability(
        config["balance_threshold"], "balance_threshold", 0.0, float("inf"), low_open=True)

    bootstrap = config["bootstrap"]
    if not isinstance(bootstrap, dict):
        raise ValueError("配置项bootstrap必须为对象")
    unknown_bootstrap = sorted(set(bootstrap) - set(_BOOTSTRAP_KEYS))
    if unknown_bootstrap:
        raise ValueError("未知bootstrap配置项：" + ",".join(unknown_bootstrap))
    merged = dict(DEFAULTS["bootstrap"])
    merged.update(bootstrap)
    merged["reps"] = _check_int(merged["reps"], "bootstrap.reps", 0)
    merged["seed"] = _check_int(merged["seed"], "bootstrap.seed", 0)
    merged["level"] = _check_probability(merged["level"], "bootstrap.level", 0.0, 1.0,
                                         low_open=True, high_open=True)
    config["bootstrap"] = merged

    window = config["event_window"]
    if window is not None:
        if not isinstance(window, dict) or set(window) != {"min", "max"}:
            raise ValueError("配置项event_window必须为{min, max}对象或null")
        _check_int(window["min"], "event_window.min", 0)
        _check_int(window["max"], "event_window.max", 0)
        if window["max"] < window["min"]:
            raise ValueError("event_window.max不得小于event_window.min")
        config["event_window"] = {"min": window["min"], "max": window["max"]}

    secondary = config["secondary_outcomes"]
    if not isinstance(secondary, list) or not all(isinstance(s, str) and s for s in secondary):
        raise ValueError("配置项secondary_outcomes必须为字符串列表")
    if "energy" in secondary:
        raise ValueError("耗电已是主要结果，不得重复列入secondary_outcomes")

    balanced = config["balanced_window"]
    if balanced is not None:
        if not isinstance(balanced, list) or len(balanced) != 2:
            raise ValueError("配置项balanced_window必须为[最小相对月, 最大相对月]或null")
        _check_int(balanced[0], "balanced_window[0]", 0)
        _check_int(balanced[1], "balanced_window[1]", 0)
        if balanced[1] < balanced[0]:
            raise ValueError("balanced_window上界不得小于下界")
        config["balanced_window"] = list(balanced)

    return config


def validate_adoption(bundle, config, warnings):
    """校验改造生效月表：不推断缺失园区的改造状态。"""
    adoption = bundle["adoption"]
    if not isinstance(adoption, dict) or not adoption:
        raise ValueError("adoption字段必须为非空对象")
    for park, month in adoption.items():
        if not isinstance(park, str) or not park:
            raise ValueError("adoption的园区标识必须为非空字符串")
        if month is not None:
            parse_month(month)
    panel_parks = {record["park"] for record in bundle["panel"]}
    missing = sorted(panel_parks - set(adoption))
    if missing:
        raise ValueError("adoption缺少园区的改造信息，拒绝推断：" + ",".join(missing))
    for park in sorted(set(adoption) - panel_parks):
        warnings.append("adoption中的园区%s没有面板记录，相关单元将无法识别" % park)
    unknown_target = sorted(set(config["target_weights"]) - set(adoption))
    if unknown_target:
        raise ValueError("目标权重包含未知园区：" + ",".join(unknown_target))
    return adoption


def base_months_of(cohort, config):
    """队列的基期月份列表：提前影响窗口之外最近的base_months个月。"""
    anticipation = config["anticipation_months"]
    span = config["base_months"]
    start = -(anticipation + span)
    return [month_add(cohort, start + i) for i in range(span)]


def _entry(park, values, base, current_record, weight=None):
    base_records = [values[(park, month)] for month in base]
    base_mean = mean(record["energy"] for record in base_records)
    entry = {
        "park": park,
        "change": current_record["energy"] - base_mean,
        "base_mean": base_mean,
        "current": current_record["energy"],
        "_base": base_records,
        "_current": current_record,
    }
    if weight is not None:
        entry["weight"] = weight
    return entry


def build_cells(snapshot, adoption, config):
    """构建队列×相对月单元并判定对照资格。"""
    values = snapshot["values"]
    if not snapshot["months"]:
        raise NoDataError("截止日前无可用观测，无法构造分析单元")
    max_month = snapshot["months"][-1]
    cohorts = sorted({month for month in adoption.values() if month is not None})
    if not cohorts:
        raise ValueError("没有任何已改造园区，无法构造队列")
    weights = config["target_weights"]
    anticipation = config["anticipation_months"]
    min_controls = config["min_controls"]
    window = config["event_window"]
    e_min = window["min"] if window else 0

    cells = []
    for cohort in cohorts:
        base = base_months_of(cohort, config)
        e_max = month_diff(max_month, cohort)
        if window:
            e_max = min(e_max, window["max"])
        for event_month in range(e_min, e_max + 1):
            month = month_add(cohort, event_month)
            treated = []
            treated_excluded = []
            controls = []
            controls_excluded = []
            for park in sorted(adoption):
                if adoption[park] != cohort:
                    continue
                weight = weights.get(park, 0.0)
                if weight <= 0:
                    treated_excluded.append({
                        "park": park,
                        "reason": "not_in_target",
                        "detail": "目标权重未覆盖该园区，不属于估计目标总体",
                    })
                    continue
                missing_base = [m for m in base if (park, m) not in values]
                if missing_base:
                    treated_excluded.append({
                        "park": park,
                        "reason": "missing_base",
                        "detail": "基期缺测：" + ",".join(missing_base),
                    })
                    continue
                current = values.get((park, month))
                if current is None:
                    treated_excluded.append({
                        "park": park,
                        "reason": "missing_current",
                        "detail": "比较月缺测（可能停机或退出），不按零耗电处理",
                    })
                    continue
                treated.append(_entry(park, values, base, current, weight))
            for park in sorted(adoption):
                other = adoption[park]
                if other is not None:
                    gap = month_diff(other, month)
                    if gap <= 0:
                        controls_excluded.append({
                            "park": park,
                            "reason": "already_treated",
                            "detail": "该园区已于%s改造，不得作为对照" % other,
                        })
                        continue
                    if gap <= anticipation:
                        controls_excluded.append({
                            "park": park,
                            "reason": "in_anticipation",
                            "detail": "该园区处于提前影响期（%s改造），不得作为对照" % other,
                        })
                        continue
                missing_base = [m for m in base if (park, m) not in values]
                if missing_base:
                    controls_excluded.append({
                        "park": park,
                        "reason": "missing_base",
                        "detail": "基期缺测：" + ",".join(missing_base),
                    })
                    continue
                current = values.get((park, month))
                if current is None:
                    controls_excluded.append({
                        "park": park,
                        "reason": "missing_current",
                        "detail": "比较月缺测（可能停机或退出），不按零耗电处理",
                    })
                    continue
                controls.append(_entry(park, values, base, current))
            reasons = []
            if not treated:
                reasons.append("no_legal_treated")
            if len(controls) < min_controls:
                reasons.append("no_legal_controls")
            cells.append({
                "cohort": cohort,
                "event_month": event_month,
                "month": month,
                "base_months": list(base),
                "status": "identified" if not reasons else "not_identified",
                "reasons": reasons,
                "effect": None,
                "treated": treated,
                "treated_excluded": treated_excluded,
                "controls": controls,
                "controls_excluded": controls_excluded,
            })
    return cells
