"""单元估计：双向差分（did）与逆概率加权（did_ipw）。

- did：处理侧按目标权重加权，对照侧等权。
- did_ipw：以园区属性拟合倾向得分（逻辑回归），对照按 p/(1-p) 加权；
  倾向得分高于 1-support_epsilon 的对照判定为支撑外并剔除，
  剔除后对照不足时该单元降级为不可识别，绝不退回使用已改造对照。
- 两个估计器都在配置协变量时输出标准化均值差（SMD）平衡诊断；
  did_ipw另输出有效样本量、最大权重份额等极端权重诊断。
"""
from __future__ import annotations
from src.retrofit_cohort_effects_util import (
    fit_logistic,
    is_number,
    mean,
    sample_sd,
    sigmoid,
)

_PROPENSITY_CAP = 1e-6
_SEPARATION_COEF = 25.0


def validate_parks_attrs(bundle, cells, config):
    """校验园区属性表；配置协变量时，所有进入可识别单元的园区必须具备。"""
    attrs = bundle.get("parks", {})
    if not isinstance(attrs, dict):
        raise ValueError("parks字段必须为对象（园区属性表）")
    for park, row in attrs.items():
        if not isinstance(row, dict):
            raise ValueError("园区属性必须为对象：" + repr(park))
    covariates = config["covariates"]
    if not covariates:
        return attrs
    needed = set()
    for cell in cells:
        if cell["status"] != "identified":
            continue
        for entry in cell["treated"] + cell["controls"]:
            needed.add(entry["park"])
    gaps = []
    for park in sorted(needed):
        row = attrs.get(park)
        for name in covariates:
            if row is None or not is_number(row.get(name)):
                gaps.append("%s.%s" % (park, name))
    if gaps:
        raise ValueError("协变量缺失或非数值：" + ",".join(gaps[:5]))
    return attrs


def _covariate_row(attrs, park, covariates):
    return [float(attrs[park][name]) for name in covariates]


def _standardize(rows_treated, rows_control):
    """按合并样本标准化协变量，返回变换函数与被丢弃的零方差列号。"""
    combined = rows_treated + rows_control
    width = len(combined[0])
    means = [mean(row[j] for row in combined) for j in range(width)]
    sds = [sample_sd(row[j] for row in combined) for j in range(width)]
    dropped = [j for j in range(width) if sds[j] == 0]

    def transform(row):
        return [(row[j] - means[j]) / sds[j] for j in range(width) if j not in dropped]

    return transform, dropped


def _smd(treated_values, control_values, control_weights):
    """加权前后的标准化均值差。"""
    mean_t = mean(treated_values)
    mean_c = mean(control_values)
    var_t = sample_sd(treated_values) ** 2
    var_c = sample_sd(control_values) ** 2
    pooled = (var_t + var_c) / 2.0
    before = None if pooled == 0 else (mean_t - mean_c) / pooled ** 0.5
    total_w = sum(control_weights)
    mean_cw = sum(w * v for w, v in zip(control_weights, control_values)) / total_w
    var_cw = sum(w * (v - mean_cw) ** 2 for w, v in zip(control_weights, control_values)) / total_w
    pooled_w = (var_t + var_cw) / 2.0
    after = None if pooled_w == 0 else (mean_t - mean_cw) / pooled_w ** 0.5
    if pooled == 0 and mean_t == mean_c:
        before = 0.0
    if pooled_w == 0 and mean_t == mean_cw:
        after = 0.0
    return before, after


def _balance_table(treated, controls, control_weights, attrs, covariates):
    table = []
    for index, name in enumerate(covariates):
        t_values = [_covariate_row(attrs, e["park"], covariates)[index] for e in treated]
        c_values = [_covariate_row(attrs, e["park"], covariates)[index] for e in controls]
        before, after = _smd(t_values, c_values, control_weights)
        table.append({
            "covariate": name,
            "smd_before": before,
            "smd_after": after,
        })
    return table


def _weighted_term(entries):
    total = sum(entry["weight"] for entry in entries)
    return sum(entry["weight"] * entry["change"] for entry in entries) / total


def cell_effect(treated, controls, config, attrs):
    """由处理/对照条目计算单元效应，返回结果字典；不可行时返回None。

    该函数同时服务于点估计与园区整群重采样，保证两条路径口径一致。
    """
    if not treated or len(controls) < config["min_controls"]:
        return None
    treated_term = _weighted_term(treated)
    covariates = config["covariates"]
    if config["estimator"] == "did":
        control_term = mean(entry["change"] for entry in controls)
        result = {
            "effect": treated_term - control_term,
            "treated_term": treated_term,
            "control_term": control_term,
            "control_weights": {entry["park"]: 1.0 / len(controls) for entry in controls},
            "off_support": [],
            "diagnostics": None,
        }
        if covariates:
            weights = [1.0] * len(controls)
            result["balance"] = _balance_table(treated, controls, weights, attrs, covariates)
        return result

    rows_t = [_covariate_row(attrs, e["park"], covariates) for e in treated]
    rows_c = [_covariate_row(attrs, e["park"], covariates) for e in controls]
    transform, dropped = _standardize(rows_t, rows_c)
    features = [[1.0] + transform(row) for row in rows_t + rows_c]
    labels = [1.0] * len(treated) + [0.0] * len(controls)
    beta, converged = fit_logistic(features, labels)
    propensities = [
        min(max(sigmoid(sum(b * x for b, x in zip(beta, row))), _PROPENSITY_CAP), 1.0 - _PROPENSITY_CAP)
        for row in features
    ]
    p_controls = propensities[len(treated):]
    epsilon = config["support_epsilon"]
    keep = [i for i, p in enumerate(p_controls) if p <= 1.0 - epsilon]
    off_support = [controls[i]["park"] for i, p in enumerate(p_controls) if p > 1.0 - epsilon]
    kept = [controls[i] for i in keep]
    if len(kept) < config["min_controls"]:
        return None
    raw = [p_controls[i] / (1.0 - p_controls[i]) for i in keep]
    total = sum(raw)
    weights = [w / total for w in raw]
    control_term = sum(w * entry["change"] for w, entry in zip(weights, kept))
    ess = 1.0 / sum(w * w for w in weights)
    separation = (not converged) or max(abs(b) for b in beta) > _SEPARATION_COEF
    diagnostics = {
        "ess": ess,
        "max_weight_share": max(weights),
        "propensity_min_controls": min(p_controls),
        "propensity_max_controls": max(p_controls),
        "separation_warning": separation,
        "constant_covariates_dropped": [covariates[j] for j in dropped],
    }
    return {
        "effect": treated_term - control_term,
        "treated_term": treated_term,
        "control_term": control_term,
        "control_weights": {entry["park"]: w for w, entry in zip(weights, kept)},
        "off_support": off_support,
        "diagnostics": diagnostics,
        "balance": _balance_table(treated, kept, weights, attrs, covariates),
        "_kept_controls": kept,
    }


def estimate_cells(cells, config, attrs, warnings):
    """对所有设计可识别单元做点估计，并把估计结果写回单元。"""
    for cell in cells:
        if cell["status"] != "identified":
            continue
        result = cell_effect(cell["treated"], cell["controls"], config, attrs)
        if result is None:
            cell["status"] = "not_identified"
            cell["reasons"] = ["no_on_support_controls"]
            cell["effect"] = None
            warnings.append(
                "队列%s相对月%d：支撑判定后对照不足，单元不可识别"
                % (cell["cohort"], cell["event_month"])
            )
            continue
        for park in result["off_support"]:
            for entry in list(cell["controls"]):
                if entry["park"] == park:
                    cell["controls"].remove(entry)
                    cell["controls_excluded"].append({
                        "park": park,
                        "reason": "off_support",
                        "detail": "倾向得分超出共同支撑，剔除出本单元对照",
                    })
        if result.get("_kept_controls") is not None:
            cell["controls"] = result["_kept_controls"]
        cell["effect"] = result["effect"]
        cell["treated_term"] = result["treated_term"]
        cell["control_term"] = result["control_term"]
        estimation = {
            "estimator": config["estimator"],
            "control_weights": result["control_weights"],
        }
        if result.get("diagnostics"):
            estimation["diagnostics"] = result["diagnostics"]
            diag = result["diagnostics"]
            if diag["separation_warning"]:
                warnings.append(
                    "队列%s相对月%d：倾向得分疑似分离，权重诊断需谨慎解读"
                    % (cell["cohort"], cell["event_month"])
                )
            if diag["max_weight_share"] > config["max_weight_share"]:
                warnings.append(
                    "队列%s相对月%d：单一对照权重份额%.3f超过阈值%.3f"
                    % (cell["cohort"], cell["event_month"],
                       diag["max_weight_share"], config["max_weight_share"])
                )
        if result.get("balance"):
            estimation["balance"] = result["balance"]
            threshold = config["balance_threshold"]
            for row in result["balance"]:
                if row["smd_after"] is not None and abs(row["smd_after"]) > threshold:
                    warnings.append(
                        "队列%s相对月%d：协变量%s加权后SMD=%.3f超过阈值%.3f"
                        % (cell["cohort"], cell["event_month"], row["covariate"],
                           row["smd_after"], threshold)
                    )
        cell["estimation"] = estimation
        _add_secondary(cell, config)


def _add_secondary(cell, config):
    """次要结果（如产量）：同一对照口径下的描述性对比，绝不用于调平。"""
    for field in config["secondary_outcomes"]:
        treated = [
            entry for entry in cell["treated"]
            if field in entry["_current"] and all(field in r for r in entry["_base"])
        ]
        controls = [
            entry for entry in cell["controls"]
            if field in entry["_current"] and all(field in r for r in entry["_base"])
        ]
        if not treated or not controls:
            cell.setdefault("secondary", {})[field] = {
                "effect": None,
                "treated_n": len(treated),
                "control_n": len(controls),
                "note": "次要结果数据不足；产量属结果指标，未用于组间调平",
            }
            continue

        def change(entry):
            base_mean = mean(r[field] for r in entry["_base"])
            return entry["_current"][field] - base_mean

        total_w = sum(entry["weight"] for entry in treated)
        treated_term = sum(entry["weight"] * change(entry) for entry in treated) / total_w
        weights = cell["estimation"]["control_weights"]
        weight_sum = sum(weights.get(entry["park"], 0.0) for entry in controls)
        if weight_sum > 0:
            control_term = sum(
                weights.get(entry["park"], 0.0) * change(entry) for entry in controls
            ) / weight_sum
        else:
            control_term = mean(change(entry) for entry in controls)
        cell.setdefault("secondary", {})[field] = {
            "effect": treated_term - control_term,
            "treated_n": len(treated),
            "control_n": len(controls),
            "note": "次要结果（可能受改造影响），仅作描述，未用于组间调平",
        }
