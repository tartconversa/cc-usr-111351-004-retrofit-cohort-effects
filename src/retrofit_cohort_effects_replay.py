"""按月滚动重放：每月收紧到该月信息截止，排除未来月份观测，防止全期预处理泄漏。

重放月份m的分析只使用：
- 公布日不晚于该月信息截止日的修订（默认月末，可用--day指定当月某日）；
- 观测月份不晚于m的记录（提前公布的未来月份观测也不进入）。
每次重放都从原始资料重新构造快照，修订选择、对照资格、目标权重与估计
  口径全部在该月信息集内重新计算，绝不沿用全期预处理的任何中间结果。
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from src.retrofit_cohort_effects_input import load_bundle
from src.retrofit_cohort_effects_util import (
    check_date,
    clean,
    fingerprint_of,
    month_end,
    parse_month,
)
from src.retrofit_cohort_effects_analysis import run_analysis
from src.retrofit_cohort_effects_design import NoDataError


def replay_cutoff(month, replay_day=None):
    """重放月份的信息截止日：默认月末，否则为当月指定日（超出月末则收紧）。"""
    end = month_end(month)
    if replay_day is None:
        return end
    if not isinstance(replay_day, int) or isinstance(replay_day, bool) or not 1 <= replay_day <= 31:
        raise ValueError("replay_day必须为1到31的整数")
    candidate = "%s-%02d" % (month, replay_day)
    return min(candidate, end)


def run_replay(bundle, months, replay_day=None):
    """对给定月份序列滚动重放，返回各月报告与透视序列。"""
    if not months:
        raise ValueError("重放月份列表不能为空")
    official = bundle["analysis"].get("cutoff")
    if official is None:
        raise ValueError("缺少配置项：cutoff（信息截止日）")
    check_date(official)
    entries = []
    for month in sorted(set(months)):
        parse_month(month)
        cutoff = replay_cutoff(month, replay_day)
        capped = False
        if cutoff > official:
            cutoff = official
            capped = True
        try:
            report = run_analysis(bundle, cutoff=cutoff, max_month=month)
        except NoDataError as error:
            entries.append({
                "month": month,
                "cutoff": cutoff,
                "capped_at_analysis_cutoff": capped,
                "status": "no_data",
                "message": str(error),
            })
            continue
        entries.append({
            "month": month,
            "cutoff": cutoff,
            "capped_at_analysis_cutoff": capped,
            "status": "ok",
            "report": report,
        })
    pivot_coverage = {}
    pivot_balanced = {}
    for entry in entries:
        if entry["status"] != "ok":
            continue
        report = entry["report"]
        pivot_coverage[entry["month"]] = {
            str(point["event_month"]): point["effect"]
            for point in report["event_study"]["coverage"]
        }
        balanced = report["event_study"]["balanced"]
        if balanced["status"] == "ok":
            pivot_balanced[entry["month"]] = {
                str(point["event_month"]): point["effect"]
                for point in balanced["series"]
            }
    return clean({
        "report_type": "retrofit_cohort_effects_replay",
        "fingerprint": fingerprint_of({
            "bundle": fingerprint_of(bundle),
            "months": sorted(set(months)),
            "replay_day": replay_day,
        }),
        "replay_day": replay_day,
        "entries": entries,
        "pivot": {"coverage": pivot_coverage, "balanced": pivot_balanced},
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--months", required=True,
                        help="逗号分隔的重放月份，如2026-10,2026-11")
    parser.add_argument("--day", type=int, default=None,
                        help="每月信息截止日（默认月末）")
    args = parser.parse_args()
    bundle = load_bundle(args.path)
    months = [part.strip() for part in args.months.split(",") if part.strip()]
    result = run_replay(bundle, months, replay_day=args.day)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
