"""信息截止快照：固定截止日，对每条（园区, 月份）记录取当时已公布的最新修订。

规则：
- 只使用公布日不晚于截止日的修订；更晚公布的修订进入 excluded_unpublished 追溯。
- 重放时传入 max_month：观测月份晚于该月的记录一律排除（即使提前公布），
  进入 excluded_future_month 追溯，防止全期数据向早期比较泄漏。
- 同一（园区, 月份）存在多条已公布修订时取修订号最大者，被取代的旧值全部留痕。
- 缺测保持缺测：不做任何插补，停机月份绝不按零耗电处理。
"""
from __future__ import annotations
from src.retrofit_cohort_effects_util import (
    check_date,
    is_number,
    month_diff,
    parse_month,
)

REQUIRED_FIELDS = ("park", "month", "published", "revision", "energy")


def _check_record(record, index):
    if not isinstance(record, dict):
        raise ValueError("面板记录必须为对象：第%d条" % index)
    for field in REQUIRED_FIELDS:
        if field not in record:
            raise ValueError("面板记录缺少字段%s：第%d条" % (field, index))
    if not isinstance(record["park"], str) or not record["park"]:
        raise ValueError("园区标识必须为非空字符串：第%d条" % index)
    parse_month(record["month"])
    check_date(record["published"])
    revision = record["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("修订号必须为正整数：第%d条" % index)
    if not is_number(record["energy"]):
        raise ValueError("耗电必须为有限数值：第%d条" % index)
    if "output" in record and not is_number(record["output"]):
        raise ValueError("产出必须为有限数值：第%d条" % index)


def _brief(record):
    brief = {
        "revision": record["revision"],
        "published": record["published"],
        "energy": record["energy"],
    }
    if "output" in record:
        brief["output"] = record["output"]
    return brief


def build_snapshot(bundle, cutoff, max_month=None):
    """按信息截止构造快照，返回{values, parks, months, cutoff, max_month, trace}。"""
    check_date(cutoff)
    if max_month is not None:
        parse_month(max_month)
    groups = {}
    excluded_unpublished = []
    excluded_future = []
    total = 0
    for index, record in enumerate(bundle["panel"]):
        _check_record(record, index)
        total += 1
        key = (record["park"], record["month"])
        if max_month is not None and month_diff(record["month"], max_month) > 0:
            excluded_future.append({
                "park": record["park"],
                "month": record["month"],
                "revision": record["revision"],
                "published": record["published"],
            })
            continue
        if record["published"] > cutoff:
            excluded_unpublished.append({
                "park": record["park"],
                "month": record["month"],
                "revision": record["revision"],
                "published": record["published"],
            })
            continue
        groups.setdefault(key, []).append(record)

    values = {}
    revisions_trace = []
    eligible_records = 0
    for key in sorted(groups):
        records = groups[key]
        eligible_records += len(records)
        by_revision = {}
        for record in records:
            if record["revision"] in by_revision:
                raise ValueError(
                    "同一园区同一月份存在重复修订号：%s %s 修订%d"
                    % (key[0], key[1], record["revision"])
                )
            by_revision[record["revision"]] = record
        chosen_revision = max(by_revision)
        chosen = by_revision[chosen_revision]
        values[key] = chosen
        superseded = [by_revision[rev] for rev in sorted(by_revision) if rev != chosen_revision]
        if superseded:
            revisions_trace.append({
                "park": key[0],
                "month": key[1],
                "chosen": _brief(chosen),
                "superseded": [_brief(record) for record in superseded],
            })

    parks = sorted({park for park, _ in values})
    months = sorted({month for _, month in values})
    trace = {
        "records_total": total,
        "records_eligible": eligible_records,
        "cells_with_data": len(values),
        "excluded_unpublished": sorted(
            excluded_unpublished,
            key=lambda item: (item["park"], item["month"], item["revision"]),
        ),
        "excluded_future_month": sorted(
            excluded_future,
            key=lambda item: (item["park"], item["month"], item["revision"]),
        ),
        "revisions": revisions_trace,
    }
    return {
        "values": values,
        "parks": parks,
        "months": months,
        "cutoff": cutoff,
        "max_month": max_month,
        "trace": trace,
    }
