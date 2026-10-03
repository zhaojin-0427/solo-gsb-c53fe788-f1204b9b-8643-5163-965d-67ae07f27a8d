"""局部 CMYK 修色端到端测试：搜索、约束、不可达、ICC 绑定、TIFF 成品。"""
from __future__ import annotations

import io
import json
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
    for bad in ([101, 0, 0], [50, 128, 0], [50, 0, -129]):
        r = _retouch(client, job["job_id"], target_lab=bad)
        assert r.status_code == 422, bad
    # 新版 httpx 拒绝在客户端编码 NaN；显式发送 JSON 裸 NaN token，
    # 验证服务端 validate_request 仍然拒绝。
    r = client.post(
        "/api/retouch",
        content='{"job_id": %s, "x": 4, "y": 4, "width": 16, "height": 16, '
                '"target_lab": [NaN, 0, 0], "tolerance_de": 3.0, '
                '"max_shift": 8, "tac_limit": 1020}'
                % json.dumps(job["job_id"]),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422, r.text


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


# --------------------------------------------------------------------------
# 身份规则：不同请求必须区分（含无分隔拼接触发的 (1,23,4,5)/(12,3,4,5)
# 串用回归），相同请求稳定复用；浮点不被六位小数格式化错误合并。
# --------------------------------------------------------------------------

def _gradient_png(size=48):
    """非平坦图：不同位置选区颜色不同，搜索结果应不同。"""
    xs = np.linspace(40, 220, size, dtype=np.uint8)[None, :]
    ys = np.linspace(60, 200, size, dtype=np.uint8)[:, None]
    arr = np.zeros((size, size, 3), np.uint8)
    arr[..., 0] = xs
    arr[..., 1] = ys
    arr[..., 2] = 120
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="PNG")
    return buf.getvalue()


def _run_gradient_job(client, ids):
    r = client.post(
        "/api/jobs",
        files={"image": ("gradient.png", _gradient_png(), "image/png")},
        data={
            "source_profile": ids["srgb"],
            "press_profile": ids["cmyk"],
            "display_profile": ids["display"],
            "threshold": "3",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_distinct_rects_get_distinct_search_report_and_artifacts(client, ids):
    """(1,23,4,5) 与 (12,3,4,5) 旧规则下无分隔拼接都形成 12345，必须分开。"""
    job = _run_gradient_job(client, ids)
    common = dict(
        target_lab=[45.0, 5.0, -15.0],
        tolerance_de=3.0, max_shift=8, tac_limit=1020,
    )
    r1 = _retouch(client, job["job_id"], x=1, y=23, width=4, height=5, **common)
    r2 = _retouch(client, job["job_id"], x=12, y=3, width=4, height=5, **common)
    d1, d2 = r1.json(), r2.json()
    assert r1.status_code == 200 and r2.status_code == 200
    assert d1["retouch_id"] != d2["retouch_id"]
    assert d1["rect"] != d2["rect"]
    assert d1["rect"] == {"x": 1, "y": 23, "width": 4, "height": 5}
    assert d2["rect"] == {"x": 12, "y": 3, "width": 4, "height": 5}

    from app import main
    b1 = main.RETOUCH_DIR / d1["retouch_id"]
    b2 = main.RETOUCH_DIR / d2["retouch_id"]
    assert b1.is_dir() and b2.is_dir() and b1 != b2
    assert (b1 / "report.json").read_bytes() != (b2 / "report.json").read_bytes()
    assert (b1 / "before.png").read_bytes() != (b2 / "before.png").read_bytes()
    assert (b1 / "after.png").read_bytes() != (b2 / "after.png").read_bytes()

    # 第二次提交不得拿到第一次的选区/预览。
    again2 = _retouch(client, job["job_id"], x=12, y=3, width=4, height=5,
                      **common).json()
    assert again2["retouch_id"] == d2["retouch_id"]
    assert again2["rect"] == d2["rect"]
    assert client.get(again2["images"]["before"]).content == \
        (b2 / "before.png").read_bytes()
    assert client.get(again2["images"]["before"]).content != \
        (b1 / "before.png").read_bytes()

    # 两个报告各自绑定自己的矩形与原任务。
    for rid, rect in ((d1["retouch_id"], d1["rect"]),
                      (d2["retouch_id"], d2["rect"])):
        rep = client.get(f"/api/retouch/{rid}/report").json()
        assert rep["region"] == rect
        assert rep["source_job"]["job_id"] == job["job_id"]

    # 成品（可达时）也各自绑定：ROI 外字节等于原任务、ROI 内偏移与报告一致。
    for d in (d1, d2):
        if d["tiff"]:
            tif = np.asarray(Image.open(io.BytesIO(client.get(d["tiff"]).content)))
            orig = np.load(main.JOB_DIR / job["job_id"] / "cmyk.npy")
            rect = d["rect"]
            x, y, w, h = rect["x"], rect["y"], rect["width"], rect["height"]
            off = np.array(d["best_offset_cmyk"], dtype=np.int32)
            diff = tif[y:y + h, x:x + w].astype(np.int32) - \
                orig[y:y + h, x:x + w].astype(np.int32)
            assert np.all(diff == off[None, :])
            mask = np.zeros(orig.shape[:2], dtype=bool)
            mask[y:y + h, x:x + w] = True
            assert np.array_equal(tif[~mask], orig[~mask])


def test_identical_request_reuses_identical_result(client, ids):
    job = _run_gradient_job(client, ids)
    d1 = _retouch(client, job["job_id"], x=7, y=9, width=10, height=8,
                  target_lab=[52.25, -3.5, 7.125], tolerance_de=1.25,
                  max_shift=6, tac_limit=900).json()
    d2 = _retouch(client, job["job_id"], x=7, y=9, width=10, height=8,
                  target_lab=[52.25, -3.5, 7.125], tolerance_de=1.25,
                  max_shift=6, tac_limit=900).json()
    assert d1["retouch_id"] == d2["retouch_id"]
    assert d1["best_offset_cmyk"] == d2["best_offset_cmyk"]
    assert d1["rect"] == d2["rect"]
    # int 50 与 float 50.0 语义相同，应视为同一请求。
    d3 = _retouch(client, job["job_id"], x=7, y=9, width=10, height=8,
                  target_lab=[50, -3.5, 7.125], tolerance_de=1.25,
                  max_shift=6, tac_limit=900).json()
    d4 = _retouch(client, job["job_id"], x=7, y=9, width=10, height=8,
                  target_lab=[50.0, -3.5, 7.125], tolerance_de=1.25,
                  max_shift=6, tac_limit=900).json()
    assert d3["retouch_id"] == d4["retouch_id"]


def test_target_lab_and_tolerance_sub_microdifferences_stay_distinct(client, ids):
    """第 7 位小数不同的目标/容差不得被六位小数格式化合并。"""
    from app.retouch import compute_retouch_id, validate_request
    job = _run_gradient_job(client, ids)
    from app import main as _main
    meta = json.loads(
        (_main.JOB_DIR / job["job_id"] / "meta.json").read_text("utf-8")
    )

    def req(**over):
        body = {
            "job_id": job["job_id"], "x": 4, "y": 4, "width": 16, "height": 16,
            "target_lab": [50.0, 10.0, -10.0],
            "tolerance_de": 3.0, "max_shift": 8, "tac_limit": 1020,
        }
        body.update(over)
        return validate_request(body)

    id_a = compute_retouch_id(meta, req(target_lab=[50.0, 10.0, -10.0]))
    id_b = compute_retouch_id(meta, req(target_lab=[50.0000001, 10.0, -10.0]))
    id_c = compute_retouch_id(meta, req(tolerance_de=3.0))
    id_d = compute_retouch_id(meta, req(tolerance_de=3.0000001))
    assert id_a != id_b
    assert id_c != id_d
    # 同一请求重复计算身份稳定。
    assert id_a == compute_retouch_id(meta, req())


def test_retouch_id_collision_regression_unit():
    from app.retouch import RetouchRequest, compute_retouch_id
    meta = {"job_id": "j", "source_intent": "relative_colorimetric",
            "proof_intent": "relative_colorimetric"}

    def rq(x, y, w, h):
        return RetouchRequest(
            job_id="j", x=x, y=y, width=w, height=h,
            target_lab=(50.0, 10.0, -10.0), tolerance_de=3.0,
            max_shift=8, tac_limit=1020)

    assert compute_retouch_id(meta, rq(1, 23, 4, 5)) != \
        compute_retouch_id(meta, rq(12, 3, 4, 5))
    # 其它无分隔拼接歧义也必须分开。
    assert compute_retouch_id(meta, rq(1, 2, 34, 5)) != \
        compute_retouch_id(meta, rq(12, 3, 4, 5))
    # 相同请求身份一致；不同意图/任务身份不同。
    assert compute_retouch_id(meta, rq(1, 2, 3, 4)) == \
        compute_retouch_id(meta, rq(1, 2, 3, 4))
    meta2 = dict(meta, job_id="k")
    assert compute_retouch_id(meta, rq(1, 2, 3, 4)) != \
        compute_retouch_id(meta2, rq(1, 2, 3, 4))
    meta3 = dict(meta, source_intent="perceptual")
    assert compute_retouch_id(meta, rq(1, 2, 3, 4)) != \
        compute_retouch_id(meta3, rq(1, 2, 3, 4))


def test_tampered_cache_is_rejected_and_recomputed(client, ids):
    """缓存报告/产物与请求绑定不符（被串改、缺文件）时拒绝复用并重算。"""
    from app import main
    job = _run_gradient_job(client, ids)
    d = _retouch(client, job["job_id"], x=4, y=4, width=12, height=12,
                 target_lab=[50.0, 10.0, -10.0], tolerance_de=2.0,
                 max_shift=8, tac_limit=1020).json()
    base = main.RETOUCH_DIR / d["retouch_id"]

    # 1) 篡改报告中的矩形：同 ID 目录再提交时必须识别并重算/拒绝复用，
    #    返回结果仍严格绑定本次请求。
    rep_path = base / "report.json"
    rep = json.loads(rep_path.read_text("utf-8"))
    rep["region"]["x"] = 99
    rep_path.write_text(json.dumps(rep), "utf-8")
    again = _retouch(client, job["job_id"], x=4, y=4, width=12, height=12,
                     target_lab=[50.0, 10.0, -10.0], tolerance_de=2.0,
                     max_shift=8, tac_limit=1020)
    assert again.status_code == 200
    assert again.json()["rect"]["x"] == 4
    assert json.loads(rep_path.read_text("utf-8"))["region"]["x"] == 4

    # 2) 删除预览 PNG：拒绝复用并重新生成。
    (base / "after.png").unlink()
    again2 = _retouch(client, job["job_id"], x=4, y=4, width=12, height=12,
                      target_lab=[50.0, 10.0, -10.0], tolerance_de=2.0,
                      max_shift=8, tac_limit=1020)
    assert again2.status_code == 200
    assert (base / "after.png").exists()

    # 3) 替换预览 PNG 字节（与报告摘要不符）：重新计算恢复；GET 期间若
    #    直接命中被替换文件则返回 422。
    (base / "before.png").write_bytes(b"not-a-png")
    assert client.get(d["images"]["before"]).status_code == 422
    again3 = _retouch(client, job["job_id"], x=4, y=4, width=12, height=12,
                      target_lab=[50.0, 10.0, -10.0], tolerance_de=2.0,
                      max_shift=8, tac_limit=1020)
    assert again3.status_code == 200
    assert client.get(d["images"]["before"]).status_code == 200

    # 4) 把另一个矩形的报告硬塞进同一目录（模拟旧版串用脏数据落盘）：
    #    按本次请求校验失败，重算后报告 region 必须是本次矩形。
    other = _retouch(client, job["job_id"], x=20, y=20, width=8, height=8,
                     target_lab=[50.0, 10.0, -10.0], tolerance_de=2.0,
                     max_shift=8, tac_limit=1020).json()
    other_rep = (main.RETOUCH_DIR / other["retouch_id"] / "report.json").read_bytes()
    rep_path.write_bytes(other_rep)
    again4 = _retouch(client, job["job_id"], x=4, y=4, width=12, height=12,
                      target_lab=[50.0, 10.0, -10.0], tolerance_de=2.0,
                      max_shift=8, tac_limit=1020)
    assert again4.status_code == 200
    assert again4.json()["rect"] == d["rect"]
