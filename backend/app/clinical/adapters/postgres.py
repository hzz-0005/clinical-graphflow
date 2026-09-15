from __future__ import annotations

from typing import Any, Protocol

from app.clinical.canonical import CanonicalAggregateResult, CanonicalClinicalQuery
from app.tools.metric_query import QueryResult


# This is the complete set of dimensions that can be interpolated into the
# governed subgroup query.  Values come from a Literal-validated request and
# are mapped to fixed SQL identifiers; user/model text never becomes SQL.
SUBGROUP_DIMENSION_COLUMNS = {
    "region": "region",
    "site_id": "site_id",
    "age_band": "age_band",
    "sex": "sex",
    "severity_band": "severity_band",
}

MISSINGNESS_DIMENSION_COLUMNS = {
    "region": "region",
    "site_id": "site_id",
    "age_band": "age_band",
    "sex": "sex",
    "severity_band": "severity_band",
}


class ReadonlyClinicalQueryExecutor(Protocol):
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> QueryResult: ...


class PostgresClinicalAdapter:
    """Compile canonical aggregate queries into governed PostgreSQL/dbt SQL."""

    #: Domains each non-batch-bound operation needs from the selected published batch.
    DOMAIN_REQUIREMENTS: dict[str, frozenset[str]] = {
        "inspect_treatment_exposure": frozenset({"EX"}),
        "inspect_protocol_quality": frozenset({"SITE_QUALITY"}),
        "inspect_safety_summary": frozenset({"AE"}),
        "analyze_safety_trend": frozenset({"AE"}),
        "analyze_visit_windows": frozenset({"ADEFF"}),
        "rank_sites": frozenset({"SITE_QUALITY"}),
    }

    #: Marts that carry ``source_batch_id`` and can therefore serve a selected published batch.
    BATCH_BOUND_OPERATIONS: frozenset[str] = frozenset(
        {
            "inspect_trial",
            "compare_treatment_effect",
            "check_randomization_balance",
            "analyze_missingness",
            "profile_sites",
            "build_subgroup_forest",
            "run_sensitivity_analysis",
            "inspect_data_quality",
        }
    )

    def __init__(self, readonly_tool: ReadonlyClinicalQueryExecutor) -> None:
        self._readonly_tool = readonly_tool

    def inspect_trial(self, trial_id: str, source_batch_id: str | None = None) -> CanonicalAggregateResult:
        where = "trial_id = %s"
        params: list[Any] = [trial_id]
        if source_batch_id:
            where += " and source_batch_id = %s"
            params.append(source_batch_id)
        return self._execute("""select trial_id, array_agg(distinct arm order by arm) as arms,
            count(distinct site_id) as site_count, count(distinct region) as region_count,
            count(*) as sample_size from analytics_clinical_marts.mart_trial_population
            where """ + where + " group by trial_id", tuple(params))

    def compare_treatment_effect(self, query: CanonicalClinicalQuery) -> CanonicalAggregateResult:
        where, params = self._where(query)
        return self._execute(f"""select arm, count(*) filter (where assessment_status = 'completed') as sample_size,
            avg(week12_improvement_score) filter (where assessment_status = 'completed') as mean_improvement,
            variance(week12_improvement_score) filter (where assessment_status = 'completed') as variance
            from analytics_clinical_marts.mart_week12_efficacy where {where}
            group by arm order by arm""", params)

    def check_randomization_balance(self, query: CanonicalClinicalQuery) -> CanonicalAggregateResult:
        where, params = self._where(query)
        return self._execute(f"""select arm, count(*) as sample_size, avg(baseline_score) as baseline_mean,
            variance(baseline_score) as baseline_variance, avg(disease_duration_months) as duration_mean
            from analytics_clinical_marts.mart_randomization_balance where {where}
            group by arm order by arm""", params)

    def analyze_missingness(self, query: CanonicalClinicalQuery, group_by: str = "treatment_arm") -> CanonicalAggregateResult:
        if group_by == "treatment_arm":
            where, params = self._where(query)
            return self._execute(f"""select arm, count(*) as sample_size,
                sum(week12_missing::int) as missing_records,
                avg(week12_missing::int) as missing_rate from analytics_clinical_marts.mart_missingness
                where {where} group by arm order by arm""", params)
        try:
            group_column = MISSINGNESS_DIMENSION_COLUMNS[group_by]
        except KeyError as exc:
            raise ValueError(f"unsupported missingness dimension: {group_by}") from exc
        if query.source_batch_id and group_by in {"age_band", "sex", "severity_band"}:
            # ``dim_participants`` is the only source for these attributes and
            # is not batch-bound. Never mix it with a selected uploaded batch.
            return self.empty_for_unbound_mart("analyze_missingness", query.source_batch_id)
        params: list[Any] = [query.trial_id]
        clauses = ["m.trial_id = %s"]
        if query.source_batch_id:
            clauses.append("m.source_batch_id = %s")
            params.append(query.source_batch_id)
        if query.subgroup is not None:
            subgroup_column = query.subgroup.dimension
            subgroup_alias = "d" if subgroup_column in {"age_band", "sex", "severity_band"} else "m"
            clauses.append(f"{subgroup_alias}.{subgroup_column} = %s")
            params.append(query.subgroup.value)
        where = " and ".join(clauses)
        if group_by in {"region", "site_id"}:
            selected = f"m.{group_column}"
            from_sql = "analytics_clinical_marts.mart_missingness m"
            group_sql = f"m.{group_column}"
        else:
            selected = f"d.{group_column}"
            from_sql = "analytics_clinical_marts.mart_missingness m join analytics_clinical_core.dim_participants d on d.trial_id = m.trial_id and d.participant_id = m.participant_id"
            group_sql = f"d.{group_column}"
        return self._execute(f"""select {selected} as subgroup, m.arm,
            count(*) as sample_size,
            sum(m.week12_missing::int) as missing_records,
            avg(m.week12_missing::int) as missing_rate
            from {from_sql} where {where}
            group by {group_sql}, m.arm order by {group_sql}, m.arm""", tuple(params))

    def profile_sites(self, query: CanonicalClinicalQuery) -> CanonicalAggregateResult:
        where, params = self._where(query)
        return self._execute(f"""select site_id, region, arm, count(*) as sample_size,
            avg(week12_improvement_score) filter (where assessment_status = 'completed') as mean_improvement
            from analytics_clinical_marts.mart_week12_efficacy where {where}
            group by site_id, region, arm order by site_id, arm""", params)

    def inspect_treatment_exposure(self, trial_id: str, site_id: str | None, source_batch_id: str | None = None, source_domains: frozenset[str] = frozenset(), group_by: str = "site") -> CanonicalAggregateResult:
        if source_batch_id:
            return self._degrade("inspect_treatment_exposure", source_batch_id, source_domains)
        if group_by == "region" and site_id is None:
            return self._execute("""select trial_id, region, count(*) as sample_size,
                sum(actual_dose) as actual_dose, sum(planned_dose) as planned_dose,
                sum(actual_dose) / nullif(sum(planned_dose), 0) as adherence_rate,
                sum(missed_doses) as missed_doses from analytics_clinical_marts.mart_treatment_exposure
                where trial_id = %s group by trial_id, region order by region""",(trial_id,))
        site_filter=" and site_id = %s" if site_id else ""
        params=(trial_id,site_id) if site_id else (trial_id,)
        return self._execute(f"""select trial_id, site_id, arm, count(*) as sample_size,
            sum(actual_dose) as actual_dose, sum(planned_dose) as planned_dose,
            sum(actual_dose) / nullif(sum(planned_dose), 0) as adherence_rate,
            sum(missed_doses) as missed_doses from analytics_clinical_marts.mart_treatment_exposure
            where trial_id = %s{site_filter} group by trial_id, site_id, arm order by site_id, arm""",params)

    def inspect_protocol_quality(self, trial_id: str, site_id: str | None, source_batch_id: str | None = None, source_domains: frozenset[str] = frozenset(), group_by: str = "site", measure: str | None = None) -> CanonicalAggregateResult:
        if source_batch_id:
            return self._degrade("inspect_protocol_quality", source_batch_id, source_domains)
        if group_by == "treatment_arm" and site_id is None:
            if measure == "temperature_excursion":
                # Specimen handling events are recorded at site grain, not participant/arm grain.
                # The mart repeats the site event on each arm row so site reports retain their
                # population split; summing those rows would falsely double-count the event.  Keep
                # one explicitly unattributed row instead of inventing an arm assignment.
                result = self._execute("""with site_quality as (
                    select trial_id, site_id,
                        sum(participant_count) as sample_size,
                        max(temperature_excursions) as temperature_excursions,
                        max(maximum_excursion_temperature_c) as maximum_excursion_temperature_c
                    from analytics_clinical_marts.mart_site_quality
                    where trial_id = %s
                    group by trial_id, site_id
                )
                select trial_id, null::text as arm,
                    sum(sample_size) as sample_size,
                    null::numeric as major_deviation_participants,
                    sum(temperature_excursions) as temperature_excursions,
                    max(maximum_excursion_temperature_c) as maximum_excursion_temperature_c,
                    'site_only'::text as attribution_status
                from site_quality
                group by trial_id
                order by trial_id""", (trial_id,))
                result.warnings.append(
                    "温度偏离只有研究中心粒度，未记录治疗臂归属；不能将中心事件分摊到治疗组或对照组。"
                )
                return result
            if measure is None:
                # An unqualified arm request must never mix a site-grain event into
                # arm-grain counts.  The mart repeats site handling rows on every arm
                # so the arm view can retain population context, but that repetition
                # is not an arm attribution.  Keep the protocol-deviation measure and
                # explicitly withhold temperature fields until the caller asks for the
                # site-grain-safe ``temperature_excursion`` measure.
                result = self._execute("""select trial_id, arm,
                    sum(participant_count) as sample_size,
                    sum(major_deviation_participants) as major_deviation_participants,
                    null::numeric as temperature_excursions,
                    null::numeric as maximum_excursion_temperature_c,
                    'protocol_arm_only'::text as attribution_status
                    from analytics_clinical_marts.mart_site_quality where trial_id = %s
                    group by trial_id, arm order by arm""", (trial_id,))
                result.warnings.append(
                    "该治疗臂结果只支持按臂统计方案偏离；温控事件只有研究中心粒度，未分配到治疗臂。"
                )
                return result
            return self._execute("""select trial_id, arm,
                sum(participant_count) as sample_size,
                sum(major_deviation_participants) as major_deviation_participants,
                sum(temperature_excursions) as temperature_excursions,
                max(maximum_excursion_temperature_c) as maximum_excursion_temperature_c
                from analytics_clinical_marts.mart_site_quality where trial_id = %s
                group by trial_id, arm order by arm""", (trial_id,))
        if group_by == "region" and site_id is None:
            if measure == "temperature_excursion":
                return self._execute("""with site_quality as (
                    select trial_id, site_id, region,
                        sum(participant_count) as sample_size,
                        max(temperature_excursions) as temperature_excursions,
                        max(maximum_excursion_temperature_c) as maximum_excursion_temperature_c
                    from analytics_clinical_marts.mart_site_quality
                    where trial_id = %s
                    group by trial_id, site_id, region
                )
                select trial_id, region,
                    sum(sample_size) as sample_size,
                    null::numeric as major_deviation_participants,
                    sum(temperature_excursions) as temperature_excursions,
                    max(maximum_excursion_temperature_c) as maximum_excursion_temperature_c,
                    'site_only'::text as attribution_status
                from site_quality
                group by trial_id, region
                order by region""", (trial_id,))
            return self._execute("""select trial_id, region,
                sum(participant_count) as sample_size,
                sum(major_deviation_participants) as major_deviation_participants,
                max(temperature_excursions) as temperature_excursions,
                max(maximum_excursion_temperature_c) as maximum_excursion_temperature_c
                from analytics_clinical_marts.mart_site_quality where trial_id = %s
                group by trial_id, region order by region""", (trial_id,))
        site_filter = " and site_id = %s" if site_id else ""
        params = (trial_id, site_id) if site_id else (trial_id,)
        return self._execute(f"""select trial_id, site_id, region,
            sum(participant_count) as sample_size,
            sum(major_deviation_participants) as major_deviation_participants,
            max(temperature_excursions) as temperature_excursions,
            max(maximum_excursion_temperature_c) as maximum_excursion_temperature_c
            from analytics_clinical_marts.mart_site_quality where trial_id = %s{site_filter}
            group by trial_id, site_id, region order by site_id""", params)

    def analyze_safety_trend(self, query: CanonicalClinicalQuery) -> CanonicalAggregateResult:
        where, params = self._where(query)
        return self._execute(f"""select event_month, arm, sum(participant_count) as sample_size,
            sum(adverse_event_count) as adverse_event_count,
            sum(serious_event_count) as serious_event_count,
            avg(participant_event_rate) as participant_event_rate,
            sum(serious_event_count)::numeric / nullif(sum(participant_count), 0) as serious_event_rate
            from analytics_clinical_marts.mart_safety_trend where {where}
            group by event_month, arm order by event_month, arm""", params)

    def inspect_safety_summary(self, query: CanonicalClinicalQuery) -> CanonicalAggregateResult:
        """Return one governed safety denominator and event proportion per treatment arm.

        ``mart_safety_trend`` is intentionally not used here: it is a monthly event-count
        series whose denominator is the number of participant-months.  A question asking for
        each arm's overall safety-event proportion needs the trial-level summary mart instead,
        where the denominator is exposed participants.
        """

        where, params = self._where(query)
        return self._execute(f"""select arm,
            randomized_participants, exposed_participants,
            participants_with_adverse_event, participants_with_serious_adverse_event,
            participants_with_adverse_event::numeric / nullif(exposed_participants, 0) as adverse_event_rate,
            participants_with_serious_adverse_event::numeric / nullif(exposed_participants, 0) as serious_adverse_event_rate,
            exposed_participants as sample_size
            from analytics_clinical_marts.mart_safety_summary
            where {where}
            order by arm""", params)

    def analyze_visit_windows(self, query: CanonicalClinicalQuery) -> CanonicalAggregateResult:
        where, params = self._where(query)
        return self._execute(f"""select arm, visit_week,
            sum(participant_count) as sample_size, sum(missed_visits) as missed_visits,
            sum(missed_visits)::numeric / nullif(sum(participant_count), 0) as missing_rate,
            sum(outside_window_visits) as outside_window_visits,
            sum(outside_window_visits)::numeric / nullif(sum(participant_count), 0) as outside_window_rate
            from analytics_clinical_marts.mart_visit_windows where {where}
            group by arm, visit_week
            order by outside_window_rate desc, arm, visit_week""", params)

    def inspect_data_quality(self, query: CanonicalClinicalQuery) -> CanonicalAggregateResult:
        where, params = self._where(query)
        return self._execute(f"""select arm, count(*) as sample_size,
            sum(week12_missing::int) as missing_records,
            avg(week12_missing::int) as missing_rate
            from analytics_clinical_marts.mart_missingness where {where}
            group by arm order by arm""", params)

    def rank_sites(
        self,
        query: CanonicalClinicalQuery,
        group_by: str = "site",
        source_batch_id: str | None = None,
        source_domains: frozenset[str] = frozenset(),
    ) -> CanonicalAggregateResult:
        if source_batch_id:
            return self._degrade("rank_sites", source_batch_id, source_domains)
        where, params = self._where(query)
        if group_by == "region":
            return self._execute(f"""select region, sum(participant_count) as sample_size,
                sum(major_deviation_participants) as major_deviations,
                max(temperature_excursions) as temperature_excursions
                from analytics_clinical_marts.mart_site_quality where {where}
                group by region order by major_deviations desc nulls last, temperature_excursions desc nulls last, region""", params)
        return self._execute(f"""select site_id, region, sum(participant_count) as sample_size,
            sum(major_deviation_participants) as major_deviations,
            max(temperature_excursions) as temperature_excursions
            from analytics_clinical_marts.mart_site_quality where {where}
            group by site_id, region order by major_deviations desc, temperature_excursions desc, site_id""", params)

    def build_subgroup_forest(self, query: CanonicalClinicalQuery, group_by: str = "region") -> CanonicalAggregateResult:
        try:
            group_column = SUBGROUP_DIMENSION_COLUMNS[group_by]
        except KeyError as exc:
            raise ValueError(f"unsupported subgroup dimension: {group_by}") from exc
        where, params = self._where(query)
        return self._execute(f"""select {group_column} as subgroup, arm,
            count(*) filter (where assessment_status = 'completed') as sample_size,
            avg(week12_improvement_score) filter (where assessment_status = 'completed') as mean_improvement,
            variance(week12_improvement_score) filter (where assessment_status = 'completed') as variance
            from analytics_clinical_marts.mart_week12_efficacy where {where}
            group by {group_column}, arm order by {group_column}, arm""", params)

    def run_sensitivity_analysis(
        self,
        query: CanonicalClinicalQuery,
        analysis_method: str = "leave_one_site_out",
    ) -> CanonicalAggregateResult:
        """Run one of the allow-listed sensitivity populations.

        ``analysis_method`` is supplied by a validated request model, never interpolated into SQL.
        The three methods share one governed outcome base and expose the excluded population in
        the result so the synthesizer cannot confuse an ITT mean with the sensitivity mean.
        """

        if analysis_method == "exclude_major_protocol_deviation":
            if query.source_batch_id:
                return self.empty_for_unbound_mart("run_sensitivity_analysis", query.source_batch_id)
            where, params = self._where(query)
            return self._execute(f"""with base as (
                select m.participant_id, m.site_id, m.arm, m.week12_improvement_score, m.assessment_status,
                    exists (
                        select 1 from analytics_clinical_core.fct_protocol_deviations d
                        where d.participant_id = m.participant_id
                          and d.severity in ('major', 'critical')
                    ) as has_major_protocol_deviation
                from analytics_clinical_marts.mart_week12_efficacy m
                where {where}
            )
            select arm,
                count(*) filter (where assessment_status = 'completed') as sample_size,
                count(*) filter (where assessment_status = 'completed' and not has_major_protocol_deviation) as eligible_sample_size,
                count(*) filter (where has_major_protocol_deviation) as excluded_participants,
                avg(week12_improvement_score) filter (where assessment_status = 'completed') as itt_mean,
                avg(week12_improvement_score) filter (where assessment_status = 'completed' and not has_major_protocol_deviation) as sensitivity_mean,
                'exclude_major_protocol_deviation'::text as analysis_method
            from base
            group by arm order by arm""", params)

        if analysis_method == "exclude_highest_quality_burden":
            where, params = self._where(query)
            return self._execute(f"""with site_burden as (
                select site_id,
                    coalesce(sum(major_deviation_participants), 0)
                    + coalesce(sum(temperature_excursions), 0) as quality_burden
                from analytics_clinical_marts.mart_site_quality
                group by site_id
            ), worst_site as (
                select site_id from site_burden
                order by quality_burden desc, site_id
                limit 1
            ), base as (
                select site_id, arm, week12_improvement_score, assessment_status
                from analytics_clinical_marts.mart_week12_efficacy
                where {where}
            )
            select m.arm,
                count(*) filter (where m.assessment_status = 'completed') as sample_size,
                count(*) filter (where m.assessment_status = 'completed' and m.site_id not in (select site_id from worst_site)) as eligible_sample_size,
                count(*) filter (where m.site_id in (select site_id from worst_site)) as excluded_participants,
                avg(m.week12_improvement_score) filter (where m.assessment_status = 'completed') as itt_mean,
                avg(m.week12_improvement_score) filter (where m.assessment_status = 'completed' and m.site_id not in (select site_id from worst_site)) as sensitivity_mean,
                'exclude_highest_quality_burden'::text as analysis_method,
                (select site_id from worst_site) as excluded_site_id
            from base m
            group by m.arm order by m.arm""", params)

        if analysis_method != "leave_one_site_out":
            raise ValueError(f"unsupported sensitivity analysis method: {analysis_method}")
        where, params = self._where(query)
        # ``{where}`` is interpolated exactly once, into the ``base`` CTE. Each extra copy would
        # consume its own positional parameters, so a single shared tuple silently desynchronises
        # the placeholder count; the CTE keeps one filter and one parameter list.
        return self._execute(f"""with base as (
            select site_id, arm, week12_improvement_score, assessment_status
            from analytics_clinical_marts.mart_week12_efficacy where {where}
        ), per_site as (
            select site_id, arm,
                avg(week12_improvement_score) filter (where assessment_status = 'completed') as site_mean
            from base
            group by site_id, arm
        ), centered as (
            select site_id, arm, site_mean,
                site_mean - avg(site_mean) over (partition by arm) as deviation
            from per_site
        ), ranked as (
            select site_id, arm,
                row_number() over (partition by arm order by abs(deviation) desc nulls last, site_id) as rank
            from centered
        ), divergent as (
            select distinct site_id from ranked where rank = 1
        )
        select m.arm,
            count(*) filter (where m.assessment_status = 'completed') as sample_size,
            count(*) filter (where m.assessment_status = 'completed' and m.site_id not in (select site_id from divergent)) as eligible_sample_size,
            count(*) filter (where m.site_id in (select site_id from divergent)) as excluded_participants,
            avg(m.week12_improvement_score) filter (where m.assessment_status = 'completed') as itt_mean,
            avg(m.week12_improvement_score) filter (where m.assessment_status = 'completed' and m.site_id not in (select site_id from divergent)) as sensitivity_mean,
            avg(m.week12_improvement_score) filter (where m.assessment_status = 'completed' and m.site_id not in (select site_id from divergent)) as leave_one_site_out_mean,
            'leave_one_site_out'::text as analysis_method
            from base m
            group by m.arm order by m.arm""", params)

    def _execute(self, sql: str, params: tuple[Any, ...]) -> CanonicalAggregateResult:
        result = self._readonly_tool.execute(sql, params)
        return CanonicalAggregateResult(source=result.source, sql=result.sql, params=params, rows=result.rows)

    @staticmethod
    def empty_for_unavailable_batch(operation: str, source_batch_id: str) -> CanonicalAggregateResult:
        return CanonicalAggregateResult(
            source=f"published batch {source_batch_id}",
            sql=f"-- {operation}: domain not provided by selected published batch",
            warnings=[
                "Selected published batch does not provide this domain; batch-bound query refused; "
                "no fallback data was used."
            ],
        )

    @staticmethod
    def empty_for_unbound_mart(operation: str, source_batch_id: str) -> CanonicalAggregateResult:
        return CanonicalAggregateResult(
            source=f"published batch {source_batch_id}",
            sql=f"-- {operation}: mart has no source_batch_id binding",
            warnings=[
                "Selected published batch provides this domain, but the mart serving it is not batch-bound; "
                "no unbound fallback data was used."
            ],
        )

    def _degrade(self, operation: str, source_batch_id: str, source_domains: frozenset[str]) -> CanonicalAggregateResult:
        """Refuse to answer from unbound data; explain *why* the batch cannot serve this operation."""

        missing = self.DOMAIN_REQUIREMENTS.get(operation, frozenset()) - source_domains
        if missing:
            return self.empty_for_unavailable_batch(operation, source_batch_id)
        return self.empty_for_unbound_mart(operation, source_batch_id)

    @staticmethod
    def _where(query: CanonicalClinicalQuery) -> tuple[str, tuple[Any, ...]]:
        clauses = ["trial_id = %s"]
        params: list[Any] = [query.trial_id]
        if query.source_batch_id:
            clauses.append("source_batch_id = %s")
            params.append(query.source_batch_id)
        if query.subgroup is not None:
            clauses.append(f"{query.subgroup.dimension} = %s")
            params.append(query.subgroup.value)
        return " and ".join(clauses), tuple(params)

