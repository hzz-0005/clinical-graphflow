from datetime import datetime
from hashlib import sha256
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from app.enterprise.models import DataScope,InvestigationSchedule,Principal


class ScheduleValidationError(ValueError): pass


class ScheduleService:
    def create(self,*,owner:Principal,name:str,question:str,cron_expression:str,timezone_name:str,provider:str|None=None)->InvestigationSchedule:
        if not owner.active: raise PermissionError("Inactive owner cannot create schedules")
        fields=cron_expression.split()
        if len(fields)!=5: raise ScheduleValidationError("Cron must have five fields")
        minute=fields[0]
        if minute=="*" or minute.startswith("*/"): raise ScheduleValidationError("Schedule interval must be at least one hour")
        try: ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc: raise ScheduleValidationError("Unknown timezone") from exc
        return InvestigationSchedule(owner_user_id=owner.user_id,name=name,question=question,cron_expression=cron_expression,timezone=timezone_name,provider=provider,scope=DataScope.from_principal(owner))

    @staticmethod
    def run_key(schedule:InvestigationSchedule,scheduled_for:datetime)->str:
        value=f"{schedule.schedule_id}:{scheduled_for.isoformat()}".encode()
        return sha256(value).hexdigest()


CLAIM_DUE_SQL="""
SELECT schedule_id FROM enterprise.investigation_schedules
WHERE enabled AND next_run_at <= now()
ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT %s
""".strip()

