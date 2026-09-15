from enum import StrEnum


class PublicationStatus(StrEnum):
    DRAFT="draft"
    PENDING_APPROVAL="pending_approval"
    PUBLISHED="published"
    REJECTED="rejected"


class PublicationPolicy:
    def decide(self,*,causal:bool,confidence:float,quality_flags:list[str],scheduled:bool)->PublicationStatus:
        if causal or confidence<.75 or quality_flags or scheduled:
            return PublicationStatus.PENDING_APPROVAL
        return PublicationStatus.PUBLISHED

    def decide_clinical(self)->PublicationStatus:
        """Clinical research conclusions can never bypass human approval."""
        return PublicationStatus.PENDING_APPROVAL

