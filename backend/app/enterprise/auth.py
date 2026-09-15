from app.enterprise.models import Capability, Principal, Role


class AuthorizationError(PermissionError): pass


class AuthorizationPolicy:
    _allowed={
        Role.ADMIN:set(Capability),
        Role.ANALYST:{Capability.INVESTIGATION_CREATE,Capability.INVESTIGATION_READ,Capability.SCHEDULE_MANAGE,Capability.AUDIT_READ},
        Role.VIEWER:{Capability.INVESTIGATION_READ},
    }

    def require(self,principal:Principal,capability:Capability)->None:
        if not principal.active or capability not in self._allowed[principal.role]:
            raise AuthorizationError(f"Permission denied: {capability}")

