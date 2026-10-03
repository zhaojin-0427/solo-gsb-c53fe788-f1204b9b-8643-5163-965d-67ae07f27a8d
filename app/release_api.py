"""HTTP API: 印刷配置版本发布与回归门禁。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import database as db
from . import releases as rl

router = APIRouter(prefix="/api/release", tags=["release"])


def _conn(req: Request):
    return req.app.state.db


def _state(req: Request):
    return req.app.state.state


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, rl.ReleaseNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, db.RevisionConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, rl.ReleaseError):
        return HTTPException(422, str(exc))
    return HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

class FamilyBody(BaseModel):
    name: str


class BaselineBody(BaseModel):
    name: str
    job_id: str
    limit_de_mean: float
    limit_de_p95: float
    limit_over_ratio: float
    limit_changed_pixels: int


class GateBody(BaseModel):
    draft_version_id: str


class PublishBody(BaseModel):
    draft_version_id: str
    expected_revision: int
    label: str | None = None


class VersionAction(BaseModel):
    expected_revision: int
    reason: str | None = None


# ---------------------------------------------------------------------------
# 配置族 / 草稿
# ---------------------------------------------------------------------------

@router.get("/families")
def families(req: Request):
    return rl.list_families(_conn(req))


@router.post("/families", status_code=201)
def create_family(req: Request, body: FamilyBody):
    try:
        return rl.create_family(_conn(req), body.name)
    except rl.ReleaseError as exc:
        raise _http_error(exc)


@router.get("/families/{family_id}")
def family_detail(family_id: str, req: Request):
    try:
        return rl.get_family_detail(_conn(req), family_id)
    except rl.ReleaseError as exc:
        raise _http_error(exc)


@router.post("/families/{family_id}/drafts", status_code=201)
async def upload_draft(
    family_id: str,
    req: Request,
    file: UploadFile = File(...),
    label: str = Form(""),
):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "ICC 配置文件过大（上限 10MB）")
    try:
        return rl.upload_draft(
            _conn(req), family_id, data, file.filename or "draft.icc",
            label or None,
        )
    except rl.ReleaseError as exc:
        raise _http_error(exc)


@router.get("/versions/{version_id}")
def version_detail(version_id: str, req: Request):
    conn = _conn(req)
    try:
        v = conn.execute(
            "SELECT * FROM press_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if not v:
            raise rl.ReleaseNotFound(f"版本/草稿不存在: {version_id}")
        out = dict(v)
        if v["gate_run_id"]:
            out["gate_run"] = rl.get_gate_run(conn, v["gate_run_id"])
        return out
    except rl.ReleaseError as exc:
        raise _http_error(exc)


@router.get("/versions/{version_id}/icc")
def download_version_icc(version_id: str, req: Request):
    try:
        path, fname = rl.download_draft(_conn(req), version_id)
    except rl.ReleaseError as exc:
        raise _http_error(exc)
    return FileResponse(path, media_type="application/vnd.iccprofile",
                        filename=fname)


# ---------------------------------------------------------------------------
# 基线
# ---------------------------------------------------------------------------

@router.get("/families/{family_id}/baselines")
def baselines(family_id: str, req: Request):
    try:
        return rl.list_baselines(_conn(req), family_id)
    except rl.ReleaseError as exc:
        raise _http_error(exc)


@router.post("/families/{family_id}/baselines", status_code=201)
def create_baseline(family_id: str, body: BaselineBody, req: Request):
    try:
        limits = rl.GateLimits.parse({
            "de_mean": body.limit_de_mean,
            "de_p95": body.limit_de_p95,
            "over_ratio": body.limit_over_ratio,
            "changed_pixels": body.limit_changed_pixels,
        })
        return rl.create_baseline(
            _conn(req), _state(req).pm, family_id,
            body.name, body.job_id, limits,
        )
    except rl.ReleaseError as exc:
        raise _http_error(exc)


# ---------------------------------------------------------------------------
# 门禁运行
# ---------------------------------------------------------------------------

@router.post("/families/{family_id}/gates", status_code=201)
def run_gate(family_id: str, body: GateBody, req: Request):
    st = _state(req)
    try:
        return rl.run_gate(
            _conn(req), st.pm, st.srgb_bytes, family_id, body.draft_version_id
        )
    except rl.ReleaseError as exc:
        raise _http_error(exc)


@router.get("/gates/{run_id}")
def gate_detail(run_id: str, req: Request):
    try:
        return rl.get_gate_run(_conn(req), run_id)
    except rl.ReleaseError as exc:
        raise _http_error(exc)


@router.get("/gates/{run_id}/baselines/{baseline_id}/{name}")
def gate_artifact(run_id: str, baseline_id: str, name: str, req: Request):
    media = "text/csv" if name.endswith(".csv") else "image/png"
    try:
        path = rl.gate_artifact(_conn(req), run_id, baseline_id, name)
    except rl.ReleaseError as exc:
        raise _http_error(exc)
    return FileResponse(path, media_type=media, filename=name)


# ---------------------------------------------------------------------------
# 发布 / 撤回 / 恢复（回滚）
# ---------------------------------------------------------------------------

@router.post("/families/{family_id}/publish")
def publish(family_id: str, body: PublishBody, req: Request):
    st = _state(req)
    try:
        return rl.publish(
            _conn(req), st.pm, st.srgb_bytes, family_id,
            body.draft_version_id, body.expected_revision, body.label,
        )
    except (rl.ReleaseError, db.RevisionConflict) as exc:
        raise _http_error(exc)


@router.post("/versions/{version_id}/withdraw")
def withdraw(version_id: str, body: VersionAction, req: Request):
    conn = _conn(req)
    try:
        v = conn.execute(
            "SELECT family_id FROM press_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if not v:
            raise rl.ReleaseNotFound(f"版本不存在: {version_id}")
        return rl.withdraw(conn, v["family_id"], version_id,
                          body.expected_revision, body.reason)
    except (rl.ReleaseError, db.RevisionConflict) as exc:
        raise _http_error(exc)


@router.post("/versions/{version_id}/restore")
def restore(version_id: str, body: VersionAction, req: Request):
    conn = _conn(req)
    try:
        v = conn.execute(
            "SELECT family_id FROM press_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if not v:
            raise rl.ReleaseNotFound(f"版本不存在: {version_id}")
        return rl.restore(conn, v["family_id"], version_id,
                          body.expected_revision, body.reason)
    except (rl.ReleaseError, db.RevisionConflict) as exc:
        raise _http_error(exc)


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

@router.get("/audit")
def audit(req: Request, family_id: str | None = None, limit: int = 100):
    return rl.list_audit(_conn(req), family_id, limit)
