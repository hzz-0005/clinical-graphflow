from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _available(rows: list[dict[str, Any]], *keys: str) -> bool:
    """Whether any row actually carries a measurement for ``keys``.

    Small-cell suppression keeps identifiers and sample sizes but blanks out every measurement
    column for cells below the minimum participant threshold. A blanked cell therefore means
    "withheld", not "measured as zero". Reading it as zero would turn a data gap into a fake
    refutation, so callers must gate on availability before deriving an adverse/benign claim.
    """

    return any(row.get(key) is not None for row in rows for key in keys)


SUPPRESSION_NOTE = "各单元格样本量低于 10 例抑制阈值，聚合值被抑制，无法据此判断"


@dataclass(frozen=True)
class ClinicalObservationInterpreter:
    """Turn bounded aggregate query rows into facts the planner can reason over."""

    def interpret(self, tool: str, rows: list[dict[str, Any]], measure: str | None = None) -> dict[str, Any]:
        observation: dict[str, Any] = {"tool": tool, "row_count": len(rows)}
        if not rows:
            return {**observation, "signal": "no_data", "human_summary": "该查询没有返回可分析的聚合数据"}

        if tool == "search_clinical_metrics":
            names = [
                str(row.get("metric", {}).get("name"))
                for row in rows
                if isinstance(row.get("metric"), dict) and row.get("metric", {}).get("name")
            ]
            summary = "、".join(names[:5]) if names else f"{len(rows)} 个候选指标"
            return {
                **observation,
                "signal": "metric_context",
                "metric_names": names,
                "human_summary": f"受治理语义层匹配到指标：{summary}；指标定义用于限定调查口径，不单独构成根因证据",
            }

        if tool == "inspect_trial":
            row = rows[0]
            return {**observation, "signal": "trial_available", "sample_size": row.get("sample_size"),
                    "site_count": row.get("site_count"), "human_summary": f"试验总体包含 {row.get('sample_size', '未知')} 名受试者和 {row.get('site_count', '未知')} 个中心"}

        if tool in {"compare_treatment_effect", "build_subgroup_forest"}:
            grouped: dict[str, list[float]] = {}
            for row in rows:
                arm, value = str(row.get("arm", "")).lower(), _number(row.get("mean_improvement"))
                if arm and value is not None:
                    grouped.setdefault(arm, []).append(value)
            control = grouped.get("control", [])
            treatment = grouped.get("treatment", [])
            delta = (sum(treatment) / len(treatment) - sum(control) / len(control)) if control and treatment else None
            text = "治疗效应数据不足，无法计算组间差异" if delta is None else f"治疗组相对对照组的平均改善差为 {delta:.2f} 分"
            return {**observation, "signal": "treatment_effect", "effect_delta": delta, "human_summary": text}

        if tool == "check_randomization_balance":
            by_arm={str(row.get("arm","")).casefold(): row for row in rows}
            control,treatment=by_arm.get("control",{}),by_arm.get("treatment",{})
            control_baseline,treatment_baseline=_number(control.get("baseline_mean")),_number(treatment.get("baseline_mean"))
            control_duration,treatment_duration=_number(control.get("duration_mean")),_number(treatment.get("duration_mean"))
            baseline_delta=(treatment_baseline-control_baseline
                            if treatment_baseline is not None and control_baseline is not None else None)
            duration_delta=(treatment_duration-control_duration
                            if treatment_duration is not None and control_duration is not None else None)
            parts=[]
            if baseline_delta is None:
                parts.append("基线评分聚合值不足，无法比较随机化组")
            else:
                parts.append(
                    f"基线评分：治疗组 {treatment_baseline:.2f}，对照组 {control_baseline:.2f}，组间差 {baseline_delta:+.2f} 分"
                )
            if duration_delta is None:
                parts.append("疾病持续时间未提供完整的治疗组/对照组均值")
            else:
                parts.append(
                    f"疾病持续时间：治疗组 {treatment_duration:.1f} 个月，对照组 {control_duration:.1f} 个月，组间差 {duration_delta:+.1f} 个月"
                )
            parts.append(
                "限制：这是描述性组间比较；未提供预设平衡阈值、标准化差异或统计不确定性，不能仅凭均值差认定随机化不平衡，也不能认定它造成了 Week-12 疗效差异"
            )
            return {
                **observation,
                "signal":"baseline_balance",
                "baseline_delta":baseline_delta,
                "duration_delta":duration_delta,
                "human_summary":"；".join(parts),
            }

        if tool == "profile_sites" and measure == "site_population":
            by_site: dict[str, dict[str, Any]] = {}
            for row in rows:
                site = row.get("site_id")
                if not site:
                    continue
                item = by_site.setdefault(
                    str(site),
                    {"site_id": str(site), "region": row.get("region"), "sample_size": 0, "arms": {}},
                )
                count = _number(row.get("sample_size"))
                if count is None:
                    continue
                item["sample_size"] += count
                arm = str(row.get("arm") or "未分组").casefold()
                item["arms"][arm] = count
            candidates = list(by_site.values())
            if not candidates or not any(item["sample_size"] for item in candidates):
                return {**observation, "signal": "insufficient_data", "human_summary": f"研究中心样本构成：{SUPPRESSION_NOTE}"}
            focus = min(candidates, key=lambda item: item["sample_size"])
            details = []
            for item in sorted(candidates, key=lambda value: (value["sample_size"], value["site_id"])):
                arm_text = "、".join(
                    f"{'治疗组' if arm == 'treatment' else '对照组' if arm == 'control' else arm} {count:.0f}"
                    for arm, count in sorted(item["arms"].items())
                )
                details.append(f"{item['site_id']}（{item.get('region') or '地区未提供'}）共 {item['sample_size']:.0f} 例：{arm_text}")
            return {
                **observation,
                "signal": "site_population",
                "focus_site_id": focus["site_id"],
                "minimum_site_sample_size": focus["sample_size"],
                "supports_site_cause": False,
                "human_summary": "研究中心受试者规模与治疗臂构成：" + "；".join(details),
            }

        if tool in {"profile_sites", "rank_sites"}:
            by_site: dict[str, dict[str, Any]] = {}
            for row in rows:
                site = row.get("site_id")
                if not site:
                    continue
                item = by_site.setdefault(str(site), {"site_id": str(site), "region": row.get("region"), "arms": {}, "burden": 0.0})
                arm, value = str(row.get("arm", "")).lower(), _number(row.get("mean_improvement"))
                if arm and value is not None:
                    item["arms"][arm] = value
                item["burden"] += (_number(row.get("major_deviations")) or 0) + (_number(row.get("temperature_excursions")) or 0)
            candidates = []
            for item in by_site.values():
                arms = item.pop("arms")
                item["effect_delta"] = arms.get("treatment") - arms.get("control") if "treatment" in arms and "control" in arms else None
                candidates.append(item)
            effect_candidates = [x for x in candidates if x["effect_delta"] is not None]
            # A focus site must be justified by something the query actually returned: either a
            # computable effect contrast, or a non-zero quality burden. Falling back to "the first
            # site returned" would invent a drill-down target out of a fully suppressed result.
            burden_candidates = [x for x in candidates if x["burden"] > 0]
            if effect_candidates:
                focus = min(effect_candidates, key=lambda x: x["effect_delta"])
            elif burden_candidates:
                focus = max(burden_candidates, key=lambda x: x["burden"])
            else:
                focus = None
            if focus is None:
                return {**observation, "signal": "insufficient_data", "focus_site_id": None,
                        "human_summary": f"中心层面无可比较的效应差，且未观测到质量负担，无法定位异常中心（{SUPPRESSION_NOTE}）"}
            worse_effect = focus.get("effect_delta") is not None and focus["effect_delta"] < 0
            quality_burden = focus["burden"] > 0
            if quality_burden and not worse_effect:
                text = f"数据驱动下钻目标为中心 {focus['site_id']}；该中心质量负担为 {focus['burden']:.1f}"
            else:
                text = f"数据驱动下钻目标为中心 {focus['site_id']}；该中心效应差为 {focus.get('effect_delta')}，质量负担为 {focus['burden']:.1f}"
            return {**observation, "signal": "site_ranking", "focus_site_id": focus["site_id"],
                    "focus_site_effect_delta": focus.get("effect_delta"),
                    "supports_site_cause": worse_effect or quality_burden, "human_summary": text}

        if tool == "inspect_treatment_exposure":
            if not _available(rows, "adherence_rate"):
                return {**observation, "signal": "insufficient_data", "human_summary": f"检查治疗暴露：{SUPPRESSION_NOTE}"}
            rates = [x for row in rows if (x := _number(row.get("adherence_rate"))) is not None]
            minimum = min(rates) if rates else None
            adverse = minimum is not None and minimum < .9
            regional = []
            for row in rows:
                rate = _number(row.get("adherence_rate"))
                region = row.get("region") or row.get("site_id")
                if rate is None or region is None:
                    continue
                missed = _number(row.get("missed_doses"))
                missed_text = f"、漏服 {missed:g} 次" if missed is not None else ""
                regional.append(f"{region} {rate * 100:.1f}%{missed_text}")
            detail = "；".join(regional)
            if adverse:
                summary = "治疗暴露依从性不足，支持中心执行因素"
            else:
                summary = "治疗暴露依从性未见明显异常，构成对中心执行因素的反证"
            if detail:
                summary += f"；地区依从性：{detail}"
            return {**observation, "signal": "exposure", "minimum_adherence_rate": minimum, "supports_site_cause": adverse,
                     "human_summary": summary}

        if tool == "run_sensitivity_analysis":
            if not _available(rows, "sensitivity_mean"):
                return {
                    **observation,
                    "signal": "insufficient_data",
                    "human_summary": "敏感性分析未返回可用的替代人群疗效均值，不能判断重算结果",
                }
            by_arm: dict[str, dict[str, Any]] = {
                str(row.get("arm", "")).casefold(): row for row in rows if row.get("arm") is not None
            }
            control, treatment = by_arm.get("control", {}), by_arm.get("treatment", {})
            itt_delta = None
            sensitivity_delta = None
            if _number(treatment.get("itt_mean")) is not None and _number(control.get("itt_mean")) is not None:
                itt_delta = _number(treatment.get("itt_mean")) - _number(control.get("itt_mean"))
            if _number(treatment.get("sensitivity_mean")) is not None and _number(control.get("sensitivity_mean")) is not None:
                sensitivity_delta = _number(treatment.get("sensitivity_mean")) - _number(control.get("sensitivity_mean"))
            method = str(next((row.get("analysis_method") for row in rows if row.get("analysis_method")), "sensitivity"))
            excluded = next((row.get("excluded_site_id") for row in rows if row.get("excluded_site_id")), None)
            direction_preserved = (
                itt_delta is not None
                and sensitivity_delta is not None
                and (itt_delta == 0 or sensitivity_delta == 0 or (itt_delta > 0) == (sensitivity_delta > 0))
            )
            details = []
            if itt_delta is not None:
                details.append(f"ITT 组间改善差 {itt_delta:+.2f} 分")
            if sensitivity_delta is not None:
                details.append(f"替代人群组间改善差 {sensitivity_delta:+.2f} 分")
            if excluded:
                details.append(f"排除中心 {excluded}")
            excluded_count = sum(int(row.get("excluded_participants") or 0) for row in rows)
            if excluded_count:
                details.append(f"排除 {excluded_count} 名受试者")
            summary = "敏感性分析：" + "；".join(details) if details else "敏感性分析已完成，但组间差异未形成完整可比值"
            summary += "；与 ITT 同方向" if direction_preserved else "；方向是否保持无法由当前聚合值判断"
            return {
                **observation,
                "signal": "sensitivity",
                "analysis_method": method,
                "itt_effect_delta": itt_delta,
                "sensitivity_effect_delta": sensitivity_delta,
                "direction_preserved": direction_preserved,
                "human_summary": summary,
            }

        if tool == "analyze_safety_trend":
            event_rate_key = (
                "serious_event_rate"
                if measure == "serious_adverse_event_rate" and _available(rows, "serious_event_rate")
                else "participant_event_rate"
            )
            if not _available(rows, event_rate_key):
                return {**observation, "signal": "insufficient_data", "human_summary": f"安全性趋势：{SUPPRESSION_NOTE}"}
            series = [(str(row.get("event_month") or ""), x) for row in rows if (x := _number(row.get(event_rate_key))) is not None]
            series.sort(key=lambda item: item[0])
            rates = [value for _, value in series]
            change = rates[-1] - rates[0] if len(rates) >= 2 else None
            rising = change is not None and change > 0
            label = "严重不良事件率" if event_rate_key == "serious_event_rate" else "安全性事件发生率"
            text = ("按月度观察，安全性事件发生率未见上升趋势，构成对安全性信号的初步反证" if change is None or not rising
                    else f"按月度观察，{label}由 {rates[0]:.3f} 升至 {rates[-1]:.3f}，支持安全性事件上升")
            if event_rate_key == "serious_event_rate" and (change is None or not rising):
                text = "按月度观察，严重不良事件率未见上升趋势，构成对安全性信号的初步反证"
            return {**observation, "signal": "safety_trend", "event_rate_key": event_rate_key, "event_rate_change": change, "supports_safety_signal": rising, "human_summary": text}

        if tool == "inspect_safety_summary":
            if not _available(rows, "adverse_event_rate", "serious_adverse_event_rate"):
                return {
                    **observation,
                    "signal": "insufficient_data",
                    "human_summary": f"安全性事件比例：{SUPPRESSION_NOTE}",
                }
            arm_order = ("control", "treatment")
            by_arm = {str(row.get("arm", "")).casefold(): row for row in rows}
            summaries: list[dict[str, Any]] = []
            details: list[str] = []
            for arm in (*arm_order, *(key for key in by_arm if key not in arm_order)):
                row = by_arm.get(arm)
                if row is None:
                    continue
                exposed = row.get("exposed_participants") or row.get("sample_size")
                adverse_count = row.get("participants_with_adverse_event")
                serious_count = row.get("participants_with_serious_adverse_event")
                adverse_rate = _number(row.get("adverse_event_rate"))
                serious_rate = _number(row.get("serious_adverse_event_rate"))
                arm_label = {"control": "对照组", "treatment": "治疗组"}.get(arm, str(row.get("arm") or arm))
                count_text = f"{adverse_count}/{exposed}" if adverse_count is not None and exposed else "分子/分母未提供"
                rate_text = "未提供" if adverse_rate is None else f"{adverse_rate * 100:.1f}%"
                serious_text = "未提供" if serious_count is None else f"{serious_count:g}"
                details.append(f"{arm_label} {count_text}（{rate_text}），严重不良事件 {serious_text}")
                summaries.append(
                    {
                        "arm": arm,
                        "exposed_participants": exposed,
                        "participants_with_adverse_event": adverse_count,
                        "participants_with_serious_adverse_event": serious_count,
                        "adverse_event_rate": adverse_rate,
                        "serious_adverse_event_rate": serious_rate,
                    }
                )
            if not details:
                return {**observation, "signal": "insufficient_data", "human_summary": f"安全性事件比例：{SUPPRESSION_NOTE}"}
            return {
                **observation,
                "signal": "safety_summary",
                "arm_rates": summaries,
                "human_summary": "安全性事件比例：" + "；".join(details) + "。该结果是试验级描述性比较，不代表药物与事件存在因果关系。",
            }

        if tool == "analyze_visit_missingness":
            if not _available(rows, "missing_rate"):
                return {**observation, "signal": "insufficient_data", "human_summary": f"访视缺失率：{SUPPRESSION_NOTE}"}
            measured = [
                (row.get("visit_week"), row.get("arm"), rate)
                for row in rows
                if (rate := _number(row.get("missing_rate"))) is not None
            ]
            maximum = max((item[2] for item in measured), default=None)
            adverse = maximum is not None and maximum >= .1
            focus = max(measured, key=lambda item: item[2]) if measured else (None, None, None)
            detail = "、".join(
                f"Week-{week:g} {arm or '未分组'} {rate * 100:.1f}%"
                for week, arm, rate in measured[:12]
                if isinstance(week, (int, float)) and arm is not None
            )
            if maximum is None:
                text = f"访视缺失率：{SUPPRESSION_NOTE}"
            else:
                text = f"访视缺失率最高为 Week-{focus[0]:g} {focus[1] or '未分组'} 的 {maximum * 100:.1f}%"
                if adverse:
                    text += "，达到 10% 异常阈值，支持进一步核查"
                else:
                    text += "，未达到 10% 异常阈值"
                if detail:
                    text += f"；按访视/治疗臂：{detail}"
            return {
                **observation,
                "signal": "missingness",
                "maximum_missing_rate": maximum,
                "focus_visit_week": focus[0],
                "focus_arm": focus[1],
                "supports_data_quality_cause": adverse,
                "human_summary": text,
            }

        if tool == "analyze_visit_windows":
            if not _available(rows, "outside_window_rate"):
                return {**observation, "signal": "insufficient_data", "human_summary": f"访视窗口偏离：{SUPPRESSION_NOTE}"}
            rates = [x for row in rows if (x := _number(row.get("outside_window_rate"))) is not None]
            maximum = max(rates) if rates else None
            adverse = maximum is not None and maximum >= .1
            return {**observation, "signal": "visit_windows", "maximum_outside_window_rate": maximum, "supports_data_quality_cause": adverse,
                    "human_summary": "访视窗口偏离率超过 10%，支持数据采集质量因素" if adverse else "访视窗口偏离率未达到 10% 异常阈值"}

        if tool == "inspect_protocol_quality":
            deviations_available = _available(rows, "major_deviation_participants")
            excursions_available = _available(rows, "temperature_excursions")
            if measure == "temperature_excursion":
                required_available = excursions_available
            elif measure == "protocol_deviation":
                required_available = deviations_available
            else:
                required_available = deviations_available or excursions_available
            if not required_available:
                return {**observation, "signal": "insufficient_data", "human_summary": f"检查方案与处理质量：{SUPPRESSION_NOTE}"}
            deviations = (sum(_number(row.get("major_deviation_participants")) or 0 for row in rows)
                          if deviations_available else None)
            excursions = (sum(_number(row.get("temperature_excursions")) or 0 for row in rows)
                           if excursions_available else None)
            adverse = ((deviations is not None and deviations > 0)
                       or (excursions is not None and excursions > 0))
            site_only = any(row.get("attribution_status") == "site_only" for row in rows)
            arm_only = any(row.get("attribution_status") == "protocol_arm_only" for row in rows)
            parts: list[str] = []
            if deviations is not None:
                parts.append(f"{deviations:.0f} 名重大偏离受试者")
            else:
                parts.append("重大偏离值未提供")
            if excursions is not None:
                parts.append(f"{excursions:.0f} 次温控偏离")
            else:
                parts.append("温控事件未按治疗臂提供")
            summary = ("方案质量检查发现 " + "、".join(parts)
                       if adverse else "方案质量检查未观察到已提供指标的异常")
            if site_only:
                summary += "；温控事件只有研究中心粒度，未记录治疗臂归属，不能据此比较治疗组与对照组"
            if arm_only:
                summary += "；当前治疗臂结果未包含温控事件的臂归属"
            arm_unattributed = (site_only or arm_only) and any(row.get("arm") is None for row in rows)
            return {**observation, "signal": "unattributed" if arm_unattributed else "protocol_quality", "major_deviations": deviations, "temperature_excursions": excursions,
                    "supports_site_cause": adverse, "arm_attribution_available": not (site_only or arm_only), "human_summary": summary}

        if tool in {"inspect_data_quality", "analyze_missingness"}:
            if not _available(rows, "missing_rate"):
                return {**observation, "signal": "insufficient_data", "human_summary": f"结局缺失：{SUPPRESSION_NOTE}"}
            rates = [x for row in rows if (x := _number(row.get("missing_rate"))) is not None]
            maximum = max(rates) if rates else None
            details = []
            for row in rows[:20]:
                label = row.get("subgroup") or row.get("region") or row.get("arm") or "总体"
                rate = _number(row.get("missing_rate"))
                denominator = row.get("sample_size")
                missing_records = row.get("missing_records")
                if rate is None:
                    continue
                denominator_text = f"，分母 {denominator}" if denominator is not None else "，分母未提供"
                numerator_text = f"，缺失 {missing_records} 条" if missing_records is not None else ""
                details.append(f"{label} 缺失率 {rate * 100:.1f}%{numerator_text}{denominator_text}")
            if details:
                summary = "；".join(details)
                if maximum is not None and maximum >= .1:
                    summary = "结局缺失率达到异常阈值；" + summary + "；支持进一步核查"
                else:
                    summary += "；未达到 10% 异常阈值"
            else:
                summary = "结局缺失率达到异常阈值，支持数据质量因素" if maximum is not None and maximum >= .1 else "结局缺失率未达到 10% 异常阈值"
            return {**observation, "signal": "missingness", "maximum_missing_rate": maximum,
                    "supports_data_quality_cause": maximum is not None and maximum >= .1,
                    "human_summary": summary}

        return {**observation, "signal": "aggregate_result", "human_summary": f"查询返回 {len(rows)} 组受最小样本量保护的聚合结果"}

