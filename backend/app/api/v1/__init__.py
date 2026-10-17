"""All /v1 routes. Endpoint numbers match frontend/lib/core/api_endpoints.dart."""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    api_keys,
    assistant,
    cases,
    connectors,
    evidence,
    findings,
    internal,
    me,
    notifications,
    reports,
    rules_providers,
    webhooks,
)

router = APIRouter()
router.include_router(me.router)
router.include_router(notifications.router)
router.include_router(cases.router)
router.include_router(evidence.router)
router.include_router(findings.router)
router.include_router(assistant.router)
router.include_router(reports.router)
router.include_router(api_keys.router)
router.include_router(admin.router)
router.include_router(admin.directory)
router.include_router(rules_providers.router)
router.include_router(webhooks.router)
router.include_router(connectors.router)
router.include_router(connectors.public)
router.include_router(internal.router)
