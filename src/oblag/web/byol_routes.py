"""BYOL (bring-your-own-license) web surface — strictly org-scoped (spec 07 §6).

An org uploads copies of licensed standards it owns and runs identifier-level diffs
between versions. Documents live under org-partitioned storage and every query is
filtered by the caller's org; one tenant can never read another's BYOL content.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from oblag.byol import ByolError, add_document, diff_versions, list_documents
from oblag.db.models import Obligation
from oblag.web.deps import Context, check_csrf, get_context, get_db, login_redirect

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _page(request: Request, db: Session, ctx: Context, **extra) -> HTMLResponse:
    assert ctx.org is not None
    obligations = [
        {"slug": s, "name": n}
        for s, n in db.query(Obligation.slug, Obligation.name).order_by(Obligation.name)
    ]
    slug_name = {o["slug"]: o["name"] for o in obligations}
    docs = [
        {
            "obligation": slug_name.get(_obl_slug(db, d.obligation_id), str(d.obligation_id)),
            "obligation_slug": _obl_slug(db, d.obligation_id),
            "version_label": d.version_label,
            "sha256": d.sha256,
            "uploaded_at": d.uploaded_at,
        }
        for d in list_documents(db, ctx.org.id)
    ]
    return templates.TemplateResponse(
        request,
        "byol.html",
        {"ctx": ctx, "obligations": obligations, "docs": docs, **extra},
    )


def _obl_slug(db: Session, obligation_id: int) -> str:
    row = db.query(Obligation.slug).filter_by(id=obligation_id).one_or_none()
    return row[0] if row else str(obligation_id)


@router.get("/byol", response_class=HTMLResponse)
def byol_page(request: Request, db: Session = Depends(get_db), ctx: Context = Depends(get_context)):
    if r := login_redirect(ctx):
        return r
    assert ctx.org is not None
    return _page(request, db, ctx)


@router.post("/byol/upload", response_class=HTMLResponse)
async def byol_upload(
    request: Request,
    obligation: str = Form(...),
    version: str = Form(...),
    attest_license: str = Form(""),
    csrf_token: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if r := login_redirect(ctx):
        return r
    assert ctx.org is not None
    check_csrf(ctx, csrf_token)
    from oblag.auth import QuotaError, enforce_quota

    try:
        enforce_quota(db, ctx.org.id, "byol_docs")
    except QuotaError as exc:
        return _page(request, db, ctx, error=str(exc))
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "doc").name) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        add_document(
            db,
            obligation,
            version.strip(),
            tmp_path,
            license_attested=bool(attest_license),
            org_id=ctx.org.id,
        )
    except ByolError as exc:
        return _page(request, db, ctx, error=str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)
    return RedirectResponse("/byol", status_code=303)


@router.post("/byol/diff", response_class=HTMLResponse)
def byol_diff(
    request: Request,
    obligation: str = Form(...),
    from_version: str = Form(...),
    to_version: str = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
    ctx: Context = Depends(get_context),
):
    if r := login_redirect(ctx):
        return r
    assert ctx.org is not None
    check_csrf(ctx, csrf_token)
    try:
        diff = diff_versions(
            db, obligation, from_version.strip(), to_version.strip(), org_id=ctx.org.id
        )
    except ByolError as exc:
        return _page(request, db, ctx, error=str(exc))
    return _page(request, db, ctx, diff=diff, diff_obligation=obligation)
