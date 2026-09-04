"""Amazon "Order History" import endpoints.

The export is not a bank statement: it carries no card transactions, only
purchases. So unlike /api/transactions/import (which creates rows), these
endpoints match parsed purchases against the card charges that already exist
and enrich the matched ones — the preview is read-only, the commit writes
purchase rows, links, and notes. See docs/amazon-order-history-matching.md.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.core.workspace_context import (
    WorkspaceContext,
    current_workspace,
    current_writable_workspace,
)
from app.schemas.amazon import AmazonMatchReport
from app.services import amazon_import_service, purchase_match_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/amazon", tags=["amazon"])


async def _parse_and_match(
    content: bytes,
    session: AsyncSession,
    ctx: WorkspaceContext,
    *,
    account_id: str | None,
    apply: bool,
) -> AmazonMatchReport:
    if not amazon_import_service.detect_format(content):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unrecognized file: expected an Amazon Order History export "
                "(Request Your Data format, one row per purchased item)"
            ),
        )

    charges = amazon_import_service.parse_order_history(content)
    try:
        report = await purchase_match_service.match_purchases(
            session,
            ctx.workspace.id,
            charges,
            account_id=_account_uuid(account_id),
            apply=apply,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    logger.info(
        "Amazon order import: charges=%d matched=%d suggested=%d apply=%s",
        report.charges_parsed, report.auto_matched, report.suggestions, apply,
    )
    return report


def _account_uuid(account_id: str | None):
    if not account_id:
        return None
    try:
        return uuid.UUID(account_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid account_id",
        )


@router.post("/orders/preview", response_model=AmazonMatchReport)
async def preview_order_import(
    file: UploadFile = File(...),
    account_id: str | None = Form(None),
    # Read-gated like the statement-import preview: nothing is persisted,
    # the report only shows which card charges *would* be linked.
    ctx: WorkspaceContext = Depends(current_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    content = await file.read()
    return await _parse_and_match(content, session, ctx, account_id=account_id, apply=False)


@router.post("/orders", response_model=AmazonMatchReport, status_code=status.HTTP_201_CREATED)
async def import_orders(
    file: UploadFile = File(...),
    account_id: str | None = Form(None),
    ctx: WorkspaceContext = Depends(current_writable_workspace),
    session: AsyncSession = Depends(get_async_session),
):
    content = await file.read()
    return await _parse_and_match(content, session, ctx, account_id=account_id, apply=True)
