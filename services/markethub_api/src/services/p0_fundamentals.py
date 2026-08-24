from __future__ import annotations

from platform_models.migration_contracts import MigrationRequest
from platform_models.p0_fundamentals import P0Request
from platform_models.provider_contracts import AuditedPage
from quotemux.migration_contracts import query as query_migration
from quotemux.p0_fundamentals import query as query_p0
from quotemux.p0_fundamentals.policy import P0_REQUIRED_PROVIDER_BY_CAPABILITY


def query(request: P0Request | MigrationRequest) -> AuditedPage:
    if request.capability_id in P0_REQUIRED_PROVIDER_BY_CAPABILITY:
        return query_p0(request)
    return query_migration(request)
