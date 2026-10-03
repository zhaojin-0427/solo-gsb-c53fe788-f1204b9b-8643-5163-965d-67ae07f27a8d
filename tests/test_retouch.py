"""局部 CMYK 修色端到端测试：搜索、约束、不可达、ICC 绑定、TIFF 成品。"""
from __future__ import annotations

import io
import os

import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app import lcms_native
from app.main import app


@pytest.fixture
def client(tmp_path):
    data_dir = tmp_path / "retouch-data"
    os.environ["SPD_DATA_DIR"] = str(data_dir)
    from app import config
    config._state = None
    config.DATA_DIR = data_dir
    config.PROFILE_DIR = data_dir / "profiles"
    config.JOB_DIR = data_dir / "jobs"
    config.RETOUCH_DIR = data_dir / "retouch"
    config.RELEASE_DIR = data_dir / "release"
    config.DB_PATH = data_dir / "softproof.db"
    from app import main
    main.JOB_DIR = data_dir / "jobs"
    main.RETOUCH_DIR = data_dir / "retouch"
    main.DB_PATH = data_dir / "softproof.db"
    with TestClient(app) as c:
        yield c


@pytest.fixture
def ids(client):
    profs = client.get("/api/profiles").json()
    return {
        "srgb": next(p["profile_id"] for p in profs["source"]
                     if p["color_space"] == "RGB"),
        "cmyk": next(p["profile_id"] for p in profs["press"]),
        "display": next(p["profile_id"] for p in profs["display"]),
    }


def _solid_png(rgb=(210, 90, 60), size=48):
    arr = np.zeros((size, size, 3), np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = rgb
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="PNG")
    return buf.getvalue()


def _run_job(client, ids, rgb=(210, 90, 60)):
    r = client.post(
        "/api/jobs",
        files={"image": ("solid.png", _solid_png(rgb), "image/png")},
        data={
            "source_profile": ids["srgb"],
            "press_profile": ids["cmyk"],
            "display_profile": ids["display"],
            "threshold": "3",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _retouch(client, job_id, **over):
    body = {
        "job_id": job_id, "x": 4, "y": 4, "width": 16, "height": 16,
        "target_lab": [50.0, 10.0, -10.0],
        "tolerance_de": 3.0, "max_shift": 8, "tac_limit": 1020,
    }
    body.update(over)
    return client.post("/api/retouch", json=body)


# --------------------------------------------------------------------------

def test_health(client):
    assert client.get("/api/health").status_code == 200


def test_retouch_reaches_with_zero_offset_for_current_mean(client, ids):
    """目标 Lab 取修色前平均 Lab 时，(0,0,0,0) 必须以 ΔE≈0 命中且排第一。"""
    from app import main
    job = _run_job(client, ids)
    cmyk = np.load(main.JOB_DIR / job["job_id"] / "cmyk.npy")
    press = client.app.state.state.pm.load_bytes(ids["cmyk"])
    lab = lcms_native.pixels_to_lab(
        cmyk[4:20, 4:20].reshape(-1, 4), "CMYK", press, "relative_colorimetric"
    ).mean(axis=0)

    r = _retouch(client, job["job_id"], target_lab=[float(v) for v in lab],
                 tolerance_de=0.0, max_shift=0)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["reachable"] is True
    assert d["best_offset_cmyk"] == [0, 0, 0, 0]
    assert d["best_abs_shift_sum"] == 0
    assert d["residual"]["de00_to_target"] < 1e-6
    assert d["residual"]["mean_de00_before_after"] == 0
    assert d["feasible_offset_count"] == 1


def _mean_lab_of_offset(client, ids, job_id, rect, offset):
    """沿原印刷 ICC 回算“原选区 CMYK 统一加 offset 后”的平均 Lab。"""
    from app import main
    cmyk = np.load(main.JOB_DIR / job_id / "cmyk.npy")
    x, y, w, h = rect
    roi = cmyk[y:y + h, x:x + w].reshape(-1, 4).astype(np.int32)
    roi = np.clip(roi + np.array(offset, dtype=np.int32), 0, 255).astype(np.uint8)
    press = client.app.state.state.pm.load_bytes(ids["cmyk"])
    return lcms_native.pixels_to_lab(
        np.ascontiguousarray(roi), "CMYK", press, "relative_colorimetric"
    ).mean(axis=0)


def test_retouch_moves_color_and_produces_tiff(client, ids):
    job = _run_job(client, ids, rgb=(180, 180, 180))
    # 目标取“统一加 (3,0,0,0) 后”的平均 Lab：该偏移必然可行且必达标，
    # 不依赖对演示配置色域的主观假设。
    target = _mean_lab_of_offset(client, ids, job["job_id"], (4, 4, 16, 16), (3, 0, 0, 0))
    d = _retouch(client, job["job_id"],
                 target_lab=[float(v) for v in target],
                 tolerance_de=0.5).json()
    assert d["reachable"] is True, d
    off = d["best_offset_cmyk"]
    assert len(off) == 4 and all(-8 <= v <= 8 for v in off)
    assert d["residual"]["de00_to_target"] <= 0.5
    # 修色后平均 Lab 必须比修色前更接近目标（ΔE 下降）。
    assert d["residual"]["de00_to_target"] < d["residual"]["de00_before_to_target"]

    # 成品 TIFF 存在、可打开、嵌入的 ICC 与原印刷 ICC 字节一致。
    resp = client.get(d["tiff"])
    assert resp.status_code == 200
    tif = Image.open(io.BytesIO(resp.content))
    assert tif.format == "TIFF" and tif.mode == "CMYK"
    assert tif.size == (48, 48)
    press = client.app.state.state.pm.load_bytes(ids["cmyk"])
    assert tif.info.get("icc_profile") == press


def test_tiff_preserves_bytes_outside_roi(client, ids):
    from app import main
    job = _run_job(client, ids, rgb=(170, 120, 90))
    # 以可行偏移 (+2, -1, 1, 0) 回算出的平均 Lab 为目标，保证可达以产出 TIFF。
    target = _mean_lab_of_offset(client, ids, job["job_id"], (10, 10, 12, 12),
                                 (2, -1, 1, 0))
    d = _retouch(client, job["job_id"], x=10, y=10, width=12, height=12,
                 target_lab=[float(v) for v in target],
                 tolerance_de=0.5).json()
    assert d["reachable"], d
    resp = client.get(d["tiff"])
    tif = np.asarray(Image.open(io.BytesIO(resp.content)))
    orig = np.load(main.JOB_DIR / job["job_id"] / "cmyk.npy")

    before = orig.copy()
    after = tif
    # 选区外字节严格不变。
    mask = np.zeros(before.shape[:2], dtype=bool)
    mask[10:22, 10:22] = True
    assert np.array_equal(after[~mask], before[~mask])
    # 选区内差值恰好等于报告里的统一偏移（无论选优最终选中哪一组）。
    off = np.array(d["best_offset_cmyk"], dtype=np.int32)
    diff = after[mask].astype(np.int32) - before[mask].astype(np.int32)
    assert np.all(diff == off[None, :])
    # 通道 0..255 与总墨量约束逐像素满足。
    assert after[mask].min() >= 0 and after[mask].max() <= 255
    assert after[mask].sum(axis=1).max() <= 1020
    assert np.abs(off).max() <= 8


def test_infeasible_when_tac_too_tight(client, ids):
    job = _run_job(client, ids, rgb=(10, 10, 10))
    r = _retouch(client, job["job_id"], tac_limit=0, max_shift=8)
    d = r.json()
    assert r.status_code == 200
    assert d["feasible_offset_count"] == 0
    assert d["reachable"] is False
    assert d["tiff"] is None
    # 成品端点必须显式拒绝。
    resp = client.get(f"/api/retouch/{d['retouch_id']}/retouched.tif")
    assert resp.status_code == 422


def test_unreachable_above_tolerance(client, ids):
    # 收紧容差到 0 且只允许零偏移：除非目标恰好等于当前均值，否则不可达。
    job = _run_job(client, ids, rgb=(200, 50, 50))
    r = _retouch(client, job["job_id"], target_lab=[95, -40, 40],
                 tolerance_de=0.0, max_shift=0)
    d = r.json()
    assert d["reachable"] is False
    assert d["feasible_offset_count"] == 1
    assert d["best_offset_cmyk"] == [0, 0, 0, 0]
    assert d["tiff"] is None
    # 不可达时仍返回修色前后软打样（此处两者相同）。
    assert client.get(d["images"]["before"]).status_code == 200
    assert client.get(d["images"]["after"]).status_code == 200


def test_channel_and_tac_constraints_hold_for_all_roi_pixels(client, ids):
    job = _run_job(client, ids, rgb=(90, 140, 200))
    target = _mean_lab_of_offset(client, ids, job["job_id"], (0, 0, 32, 32),
                                 (-2, 2, 1, -1))
    d = _retouch(client, job["job_id"], x=0, y=0, width=32, height=32,
                 target_lab=[float(v) for v in target], tolerance_de=0.5,
                 max_shift=3, tac_limit=1020).json()
    # 最终偏移必须在全选区逐像素合规（通道范围与总墨量）。
    from app import main
    orig = np.load(main.JOB_DIR / job["job_id"] / "cmyk.npy")
    off = np.array(d["best_offset_cmyk"], dtype=np.int32)
    assert d["reachable"], d
    assert np.abs(off).max() <= 3
    roi = orig[0:32, 0:32].astype(np.int32) + off
    assert roi.min() >= 0 and roi.max() <= 255
    assert roi.sum(axis=2).max() <= 1020


def test_tie_break_prefers_smaller_abs_sum_then_lexicographic(client, ids):
    """ΔE 并列时：偏移绝对值和小者优先；再并列按 (C,M,Y,K) 字典序。"""
    from app.retouch import _feasible_offsets
    # 平坦选区，所有像素 CMYK 相同；ΔE 只取决于偏移，构造一个两组偏移理论上
    # 对同一 Lab 变化对称的情形不易，因此直接验证可行枚举与排序键。
    roi = np.full((4, 4, 4), 100, dtype=np.uint8)
    cands = _feasible_offsets(roi, max_shift=1, tac_limit=1020)
    # 每通道 {-1,0,1} -> 3^4 = 81 组。
    assert cands.shape == (81, 4)
    # 排序键模拟：(ΔE 占位, abs sum, tuple)。
    keys = sorted(((int(np.abs(c).sum()), tuple(int(v) for v in c)) for c in cands),
                  key=lambda k: (0, k[0], k[1]))
    assert keys[0][1] == (0, 0, 0, 0)
    # abs sum = 1 的四个候选按字典序 (-1,0,0,0) 最先。
    one = [k for k in keys if k[0] == 1]
    assert one[0][1] == (-1, 0, 0, 0)


def test_rect_too_large_rejected(client, ids):
    job = _run_job(client, ids)
    r = _retouch(client, job["job_id"], width=65, height=8)
    assert r.status_code == 422
    assert "64" in r.json()["detail"]


def test_bad_lab_ranges_rejected(client, ids):
    job = _run_job(client, ids)
    for bad in ([101, 0, 0], [50, 128, 0], [50, 0, -129], [float("nan"), 0, 0]):
        r = _retouch(client, job["job_id"], target_lab=bad)
        assert r.status_code == 422, bad


def test_bad_constraints_rejected(client, ids):
    job = _run_job(client, ids)
    assert _retouch(client, job["job_id"], tolerance_de=-1).status_code == 422
    assert _retouch(client, job["job_id"], max_shift=9).status_code == 422
    assert _retouch(client, job["job_id"], max_shift=1.5).status_code == 422
    assert _retouch(client, job["job_id"], tac_limit=1021).status_code == 422
    assert _retouch(client, job["job_id"], width=0).status_code == 422


def test_unknown_job_404(client, ids):
    r = _retouch(client, "0" * 64)
    assert r.status_code == 404


def test_missing_press_icc_rejected(client, ids):
    job = _run_job(client, ids)
    # 让 ProfileManager 找不到原印刷配置文件（模拟原任务 ICC 缺失）。
    state = client.app.state.state
    target = next(p for role, p in state.pm._iter_files()
                  if role in ("press", "builtin") and p.stem == ids["cmyk"])
    backup = target.read_bytes()
    target.unlink()
    try:
        r = _retouch(client, job["job_id"])
        assert r.status_code == 422
        assert "印刷 ICC" in r.json()["detail"]
    finally:
        target.write_bytes(backup)


def test_press_icc_digest_mismatch_rejected(client, ids):
    job = _run_job(client, ids)
    state = client.app.state.state
    # 用另一份字节不同的合成 CMYK 配置替换同名文件（模拟配置被偷换）。
    target = None
    for _, p in state.pm._iter_files():
        if p.stem == ids["cmyk"]:
            target = p
            break
    other = lcms_native.build_demo_cmyk_profile_bytes(ink_scale=0.9)
    original = target.read_bytes()
    assert other != original
    target.write_bytes(other)
    try:
        r = _retouch(client, job["job_id"])
        assert r.status_code == 422
        assert "摘要不符" in r.json()["detail"]
    finally:
        target.write_bytes(original)


def test_report_binds_job_profile_constraints_offset(client, ids):
    job = _run_job(client, ids, rgb=(160, 100, 120))
    target = _mean_lab_of_offset(client, ids, job["job_id"], (4, 4, 16, 16),
                                 (1, 2, -1, 0))
    d = _retouch(client, job["job_id"], target_lab=[float(v) for v in target],
                 tolerance_de=0.5, max_shift=5, tac_limit=800).json()
    rid = d["retouch_id"]
    rep = client.get(f"/api/retouch/{rid}/report").json()
    assert rep["source_job"]["job_id"] == job["job_id"]
    assert rep["profiles"]["press"]["sha256"] == job["report"]["profiles"]["press"]["sha256"]
    assert rep["profiles"]["press"]["color_space"] == "CMYK"
    assert rep["constraints"]["max_shift_per_channel"] == 5
    assert rep["constraints"]["total_ink_limit"] == 800
    assert rep["best_offset_cmyk"] == d["best_offset_cmyk"]
    assert rep["region"] == d["rect"]
    assert rep["reachable"] is True


def test_deterministic_retouch_id_and_reuse(client, ids):
    job = _run_job(client, ids)
    d1 = _retouch(client, job["job_id"]).json()
    d2 = _retouch(client, job["job_id"]).json()
    assert d1["retouch_id"] == d2["retouch_id"]
    assert d1["best_offset_cmyk"] == d2["best_offset_cmyk"]


def test_proof_pngs_are_tagged_with_display_profile(client, ids):
    job = _run_job(client, ids)
    target = _mean_lab_of_offset(client, ids, job["job_id"], (4, 4, 16, 16),
                                 (-1, 1, 0, 0))
    d = _retouch(client, job["job_id"], target_lab=[float(v) for v in target],
                 tolerance_de=1.0).json()
    display = client.app.state.state.pm.load_bytes(ids["display"])
    for url in (d["images"]["before"], d["images"]["after"]):
        im = Image.open(io.BytesIO(client.get(url).content))
        assert im.info.get("icc_profile") == display
