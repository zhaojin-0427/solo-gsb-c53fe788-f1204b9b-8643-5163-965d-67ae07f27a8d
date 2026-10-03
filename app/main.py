"""FastAPI application: 印前色彩软打样台."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import database as db
from . import pipeline as pl
from . import regions as regions_mod
from . import releases
from .release_api import router as release_router
from .config import (
    DB_PATH,
    JOB_DIR,
    MAX_DIMENSION,
    MAX_UPLOAD_MB,
    RETOUCH_DIR,
    init_state,
)
from .profiles import ProfileError
from . import retouch as rt


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = init_state()
    releases.ensure_dirs()
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    for rec in state.pm.list_profiles():
        db.upsert_profile(conn, rec)
    app.state.db = conn
    app.state.state = state
    yield
    conn.close()


app = FastAPI(title="印前色彩软打样台", version="1.2.0", lifespan=lifespan)
app.include_router(release_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def job_dir(job_id: str) -> Path:
    d = JOB_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_job_arrays(job_id: str) -> dict:
    base = JOB_DIR / job_id
    out = {}
    for name in ("lab_src", "lab_press", "cmyk", "de", "heatmap", "mask3"):
        out[name] = np.load(base / f"{name}.npy", mmap_mode="r")
    meta = json.loads((base / "meta.json").read_text("utf-8"))
    out["meta"] = meta
    return out


# ---------------------------------------------------------------------------
# API: health / profiles
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/profiles")
def list_profiles():
    state = app.state.state
    records = state.pm.list_profiles()
    for rec in records:
        db.upsert_profile(app.state.db, rec)
    groups = {"source": [], "press": [], "display": []}
    for rec in records:
        if rec.role in groups:
            d = rec.to_dict()
            groups[rec.role].append(d)
    for items in groups.values():
        items.sort(key=lambda d: (not d["builtin"], d["name"]))
    return groups


@app.post("/api/profiles/{role}")
async def upload_profile(role: str, file: UploadFile = File(...)):
    if role not in ("source", "press", "display"):
        raise HTTPException(400, "配置角色必须是 source / press / display")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "ICC 配置文件过大（上限 10MB）")
    state = app.state.state
    try:
        rec = state.pm.save_upload(role, file.filename or "uploaded.icc", data)
    except ProfileError as exc:
        raise HTTPException(422, str(exc))
    db.upsert_profile(app.state.db, rec)
    return rec.to_dict()


# ---------------------------------------------------------------------------
# API: run pipeline
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
async def create_job(
    image: UploadFile = File(...),
    source_profile: str = Form(...),
    press_profile: str = Form(...),
    display_profile: str = Form(...),
    source_intent: str = Form("relative_colorimetric"),
    proof_intent: str = Form("relative_colorimetric"),
    threshold: float = Form(3.0),
    use_embedded: bool = Form(False),
):
    data = await image.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"图像超过上限 {MAX_UPLOAD_MB}MB")

    state = app.state.state
    inputs = pl.PipelineInputs(
        image_data=data,
        source_profile_id=source_profile,
        press_profile_id=press_profile,
        display_profile_id=display_profile,
        source_intent=source_intent,
        proof_intent=proof_intent,
        threshold=threshold,
        use_embedded=use_embedded,
        max_dimension=MAX_DIMENSION,
    )
    try:
        result = pl.run_pipeline(inputs, state.pm)
    except (pl.PipelineError, ProfileError) as exc:
        raise HTTPException(422, str(exc))

    job_id = pl.compute_job_id(result)
    base = job_dir(job_id)

    # Persist pixel arrays for region inspection at full double precision so
    # inspection and report statistics come from exactly the same Lab/ΔE
    # values that decided mask membership.
    np.save(base / "lab_src.npy", result.lab_src.astype(np.float64))
    np.save(base / "lab_press.npy", result.lab_press.astype(np.float64))
    np.save(base / "cmyk.npy", result.cmyk.astype(np.uint8))
    np.save(base / "de.npy", result.de.astype(np.float64))
    np.save(base / "heatmap.npy", result.heatmap.astype(np.uint8))
    np.save(base / "mask3.npy", result.mask.astype(np.uint8))
    (base / "original.bin").write_bytes(data)

    display_icc_bytes = state.pm.load_bytes(display_profile)
    pngs = pl.export_pngs(result, state.srgb_bytes, display_icc_bytes)
    for name, blob in pngs.items():
        (base / name).write_bytes(blob)

    report = pl.build_report(result, job_id)
    (base / "report.json").write_text(
        json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2), "utf-8"
    )

    meta = {
        "job_id": job_id,
        "width": result.img.size[0],
        "height": result.img.size[1],
        "mode": result.mode,
        "image_sha256": result.image_sha,
        "embedded_sha256": result.embedded_sha,
        "source_profile": source_profile,
        "press_profile": press_profile,
        "display_profile": display_profile,
        "source_intent": source_intent,
        "proof_intent": proof_intent,
        "threshold": threshold,
        "use_embedded": use_embedded,
        # 全 64 位配置摘要随任务固化：局部修色必须用同一 ICC 字节，
        # 摘要不符即拒绝。
        "source_profile_sha256": result.profile_summaries["source"]["sha256"],
        "press_profile_sha256": result.profile_summaries["press"]["sha256"],
        "display_profile_sha256": result.profile_summaries["display"]["sha256"],
    }
    (base / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")

    db.insert_job(app.state.db, {
        "job_id": job_id,
        "image_sha256": result.image_sha,
        "width": result.img.size[0],
        "height": result.img.size[1],
        "image_mode": result.mode,
        "source_profile": source_profile if not use_embedded else "<embedded>",
        "press_profile": press_profile,
        "display_profile": display_profile,
        "source_intent": source_intent,
        "proof_intent": proof_intent,
        "threshold": threshold,
        "use_embedded": use_embedded,
        "embedded_sha256": result.embedded_sha,
        "parameters": report["parameters"],
        "profile_digests": {k: v["sha256"] for k, v in result.profile_summaries.items()},
        "stats": result.stats,
    })

    return {
        "job_id": job_id,
        "width": result.img.size[0],
        "height": result.img.size[1],
        "stats": result.stats,
        "report": report,
        "images": {
            "preview": f"/api/jobs/{job_id}/images/preview.png",
            "softproof": f"/api/jobs/{job_id}/images/softproof.png",
            "heatmap": f"/api/jobs/{job_id}/images/heatmap.png",
            "overlay": f"/api/jobs/{job_id}/images/overlay.png",
            "mask": f"/api/jobs/{job_id}/images/mask.png",
        },
    }


# ---------------------------------------------------------------------------
# API: job artifacts
# ---------------------------------------------------------------------------

def _require_job(job_id: str) -> Path:
    base = JOB_DIR / job_id
    if not (base / "meta.json").exists():
        raise HTTPException(404, "任务不存在或已被删除")
    return base


@app.get("/api/jobs")
def list_jobs():
    rows = db.recent_jobs(app.state.db)
    return [dict(r) for r in rows]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    _require_job(job_id)
    return JSONResponse(json.loads((JOB_DIR / job_id / "report.json").read_text("utf-8")))


@app.get("/api/jobs/{job_id}/images/{name}")
def get_job_image(job_id: str, name: str):
    base = _require_job(job_id)
    allowed = {"preview.png", "softproof.png", "heatmap.png", "overlay.png", "mask.png"}
    if name not in allowed:
        raise HTTPException(404, "图像不存在")
    return FileResponse(base / name, media_type="image/png")


@app.get("/api/jobs/{job_id}/report")
def get_job_report(job_id: str):
    base = _require_job(job_id)
    return FileResponse(
        base / "report.json", media_type="application/json", filename="report.json"
    )


@app.get("/api/jobs/{job_id}/export.zip")
def get_job_zip(job_id: str):
    base = _require_job(job_id)
    cache = base / "export.zip"
    if not cache.exists():
        pngs = {}
        for name in ("preview.png", "softproof.png", "heatmap.png", "overlay.png", "mask.png"):
            pngs[name] = (base / name).read_bytes()
        report = json.loads((base / "report.json").read_text("utf-8"))
        cache.write_bytes(pl.export_zip(pngs, report))
    return FileResponse(cache, media_type="application/zip", filename=f"{job_id[:12]}-softproof.zip")


@app.post("/api/jobs/{job_id}/region")
async def inspect_region(job_id: str, payload: dict):
    _require_job(job_id)
    try:
        arrays = load_job_arrays(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "任务像素数据不存在")

    class R:  # light adapter
        pass

    r = R()
    r.img = type("I", (), {"size": (arrays["meta"]["width"], arrays["meta"]["height"])})()
    r.lab_src = np.asarray(arrays["lab_src"])
    r.lab_press = np.asarray(arrays["lab_press"])
    r.cmyk = np.asarray(arrays["cmyk"])
    r.de = np.asarray(arrays["de"])
    r.inputs = type("P", (), {"threshold": arrays["meta"]["threshold"]})()

    try:
        return regions_mod.inspect_region(
            r,
            int(payload.get("x", 0)),
            int(payload.get("y", 0)),
            int(payload.get("width", 10)),
            int(payload.get("height", 10)),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# ---------------------------------------------------------------------------
# API: 局部 CMYK 修色（以已保存任务分色结果为起点）
# ---------------------------------------------------------------------------

def _retouch_dir(retouch_id: str) -> Path:
    d = RETOUCH_DIR / retouch_id
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.post("/api/retouch")
def create_retouch(payload: dict | None = None):
    if not isinstance(payload, dict):
        raise HTTPException(422, "请求体必须是 JSON 对象")
    try:
        req = rt.validate_request(payload)
    except rt.RetouchError as exc:
        raise HTTPException(422, str(exc))

    job_base = JOB_DIR / req.job_id
    if not (job_base / "meta.json").exists():
        raise HTTPException(404, "原任务不存在或已被删除")

    try:
        ctx = rt.load_job_context(job_base)
    except rt.RetouchError as exc:
        raise HTTPException(422, str(exc))
    meta = ctx["meta"]

    state = app.state.state
    # 原任务印刷 ICC：必须存在且 SHA-256 与任务记录严格一致。
    row = db.get_job(app.state.db, req.job_id)
    recorded_press_digest = None
    if row is not None and row["profile_digests_json"]:
        try:
            recorded_press_digest = json.loads(row["profile_digests_json"]).get("press")
        except (json.JSONDecodeError, TypeError, AttributeError):
            recorded_press_digest = None
    recorded_press_digest = recorded_press_digest or meta.get("press_profile_sha256")

    press_profile_id = meta["press_profile"]
    try:
        press_bytes = state.pm.load_bytes(press_profile_id)
        rt.verify_press_profile(press_bytes, recorded_press_digest)
        # 摘要通过后补名称等元数据，报告仍以哈希为绑定凭据。
        press_summary_full = pl.summary_from_bytes(press_bytes, press_profile_id)
    except ProfileError as exc:
        raise HTTPException(422, f"原任务印刷 ICC 缺失，无法修色: {exc}")
    except rt.RetouchError as exc:
        raise HTTPException(422, str(exc))

    display_profile_id = meta["display_profile"]
    recorded_display_digest = meta.get("display_profile_sha256")
    try:
        display_bytes = state.pm.load_bytes(display_profile_id)
        display_summary_full = pl.summary_from_bytes(display_bytes, display_profile_id)
    except ProfileError as exc:
        raise HTTPException(422, f"原任务显示 ICC 缺失，无法生成软打样: {exc}")
    if recorded_display_digest and display_summary_full["sha256"] != recorded_display_digest:
        raise HTTPException(
            422,
            "原任务显示 ICC 摘要不符，无法按原显示配置呈现修色软打样，拒绝修色",
        )

    retouch_id = rt.compute_retouch_id(meta, req)
    fingerprint = rt.request_fingerprint(meta, req)
    base = _retouch_dir(retouch_id)
    report_path = base / "report.json"
    report = None
    if report_path.exists():
        try:
            cached = json.loads(report_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            cached = None
        # 命中缓存必须核对报告、预览与成品同本次任务、矩形及约束的绑定；
        # 不符（或报告不可读）时拒绝复用，落入下方按本次请求重新计算。
        if cached is not None and rt.cache_entry_valid(
            cached, retouch_id, fingerprint, base
        ):
            report = cached
    if report is None:
        try:
            result = rt.run_retouch(req, ctx, press_bytes)
        except rt.RetouchError as exc:
            raise HTTPException(422, str(exc))
        except RuntimeError as exc:
            raise HTTPException(422, f"LittleCMS 色彩转换失败: {exc}")

        try:
            pngs = rt.render_proof_crops(
                result, ctx["cmyk"], press_bytes, display_bytes,
                meta.get("proof_intent", "relative_colorimetric"),
            )
        except RuntimeError as exc:
            raise HTTPException(422, f"软打样渲染失败: {exc}")
        (base / "before.png").write_bytes(pngs["before.png"])
        (base / "after.png").write_bytes(pngs["after.png"])

        report = rt.build_report(
            retouch_id, meta, press_summary_full, display_summary_full, req, result
        )
        if result["reachable"]:
            tiff = rt.export_retouched_tiff(result["_cmyk_adjusted"], press_bytes)
            (base / "retouched.tif").write_bytes(tiff)
        else:
            # 不可达：显式不生成成品（若历史脏文件存在则移除）。
            stale = base / "retouched.tif"
            if stale.exists():
                stale.unlink()

        report_path.write_text(
            json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2),
            "utf-8",
        )

    return {
        "retouch_id": retouch_id,
        "reachable": report["reachable"],
        "rect": report["region"],
        "best_offset_cmyk": report["best_offset_cmyk"],
        "best_abs_shift_sum": report["best_abs_shift_sum"],
        "feasible_offset_count": report["search"]["feasible_offset_count"],
        "residual": report["residual"],
        "constraints": report["constraints"],
        "images": {
            "before": f"/api/retouch/{retouch_id}/before.png",
            "after": f"/api/retouch/{retouch_id}/after.png",
        },
        "tiff": (
            f"/api/retouch/{retouch_id}/retouched.tif"
            if report["reachable"] else None
        ),
        "report_url": f"/api/retouch/{retouch_id}/report",
        "report": report,
    }


def _require_retouch(retouch_id: str) -> Path:
    base = RETOUCH_DIR / retouch_id
    if not (base / "report.json").exists():
        raise HTTPException(404, "修色任务不存在")
    return base


@app.get("/api/retouch/{retouch_id}/report")
def get_retouch_report(retouch_id: str):
    base = _require_retouch(retouch_id)
    return FileResponse(
        base / "report.json", media_type="application/json", filename="retouch-report.json"
    )


@app.get("/api/retouch/{retouch_id}/{name}")
def get_retouch_artifact(retouch_id: str, name: str):
    base = _require_retouch(retouch_id)
    if name in ("before.png", "after.png"):
        return FileResponse(base / name, media_type="image/png")
    if name == "retouched.tif":
        report = json.loads((base / "report.json").read_text("utf-8"))
        if not report.get("reachable") or not (base / "retouched.tif").exists():
            # 不可达不产出成品。
            raise HTTPException(422, "无可行解或未达容差：标记为不可达，未生成成品 TIFF")
        return FileResponse(
            base / "retouched.tif", media_type="image/tiff",
            filename=f"retouched-{retouch_id[:12]}.tif",
        )
    raise HTTPException(404, "产物不存在")


# Static frontend (mounted last so /api routes win).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
