"""回归门禁 / 版本发布 / 撤回恢复 / 审计 的端到端测试。"""
from __future__ import annotations

import io
import os
import sqlite3
import tempfile

import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app import lcms_native
from app.main import app


@pytest.fixture
def client(tmp_path):
    data_dir = tmp_path / "release-data"
    os.environ["SPD_DATA_DIR"] = str(data_dir)
    from app import config
    # 重置共享单例，使每个测试使用独立的临时数据目录与 ProfileManager。
    config._state = None
    config.DATA_DIR = data_dir
    config.PROFILE_DIR = data_dir / "profiles"
    config.JOB_DIR = data_dir / "jobs"
    config.RELEASE_DIR = data_dir / "release"
    config.DB_PATH = data_dir / "softproof.db"
    from app import main
    from app import releases
    main.JOB_DIR = data_dir / "jobs"
    main.DB_PATH = data_dir / "softproof.db"
    # releases.py 在导入时绑定了路径常量，需要一并重定向。
    releases.JOB_DIR = data_dir / "jobs"
    releases.RELEASE_DIR = data_dir / "release"
    releases.SNAPSHOT_DIR = data_dir / "release" / "snapshots"
    releases.BASELINE_DIR = data_dir / "release" / "baselines"
    releases.GATE_DIR = data_dir / "release" / "gates"
    releases.PRESS_LIVE_DIR = data_dir / "profiles" / "press"
    with TestClient(app) as c:
        yield c


@pytest.fixture
def ids(client):
    profs = client.get("/api/profiles").json()
    return {
        "srgb": next(p["profile_id"] for p in profs["source"] if p["color_space"] == "RGB"),
        "cmyk": next(p["profile_id"] for p in profs["press"]),
        "display": next(p["profile_id"] for p in profs["display"]),
    }


def _gradient_png() -> bytes:
    arr = np.zeros((40, 40, 3), np.uint8)
    arr[..., 0] = np.linspace(0, 255, 40, dtype=np.uint8)[None, :]
    arr[..., 1] = np.linspace(0, 255, 40, dtype=np.uint8)[:, None]
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, format="PNG")
    return buf.getvalue()


def _run_job(client, ids, press=None, threshold="3.0"):
    return client.post(
        "/api/jobs",
        files={"image": ("img.png", _gradient_png(), "image/png")},
        data={
            "source_profile": ids["srgb"],
            "press_profile": press or ids["cmyk"],
            "display_profile": ids["display"],
            "threshold": threshold,
        },
    ).json()


def _make_family(client, name="pytest 族"):
    return client.post("/api/release/families", json={"name": name}).json()


def _loose_baseline(client, fid, job_id):
    return client.post(
        f"/api/release/families/{fid}/baselines",
        json={
            "name": "base", "job_id": job_id,
            "limit_de_mean": 100.0, "limit_de_p95": 100.0,
            "limit_over_ratio": 1.0, "limit_changed_pixels": 1_000_000,
        },
    )


def _upload_draft(client, fid, data, label=""):
    return client.post(
        f"/api/release/families/{fid}/drafts",
        files={"file": ("press.icc", data, "application/octet-stream")},
        data={"label": label},
    )


def _demo_icc(**kw):
    return lcms_native.build_demo_cmyk_profile_bytes(**kw)


# --------------------------------------------------------------------------

def test_release_health_and_empty_state(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/release/families").json() == []
    assert client.get("/api/release/audit").json() == []


def test_family_lifecycle_and_404(client):
    f = _make_family(client, "生命周期族")
    assert f["revision"] == 1
    detail = client.get(f"/api/release/families/{f['family_id']}").json()
    assert detail["versions"] == [] and detail["baselines"] == []

    # 重名拒绝
    r = client.post("/api/release/families", json={"name": "生命周期族"})
    assert r.status_code == 422
    # 未知族 404
    assert client.get("/api/release/families/nope").status_code == 404
    assert client.get("/api/release/versions/nope").status_code == 404


def test_draft_must_be_cmyk_output(client):
    f = _make_family(client, "草稿校验族")
    # sRGB 显示器配置不能作为印刷草稿
    r = _upload_draft(client, f["family_id"], client.app.state.state.srgb_bytes)
    assert r.status_code == 422
    assert "CMYK" in r.json()["detail"]


def test_gate_requires_baseline(client):
    f = _make_family(client, "无基线族")
    d = _upload_draft(client, f["family_id"], _demo_icc())
    assert d.status_code == 201
    r = client.post(
        f"/api/release/families/{f['family_id']}/gates",
        json={"draft_version_id": d.json()["version_id"]},
    )
    assert r.status_code == 422 and "基线" in r.json()["detail"]


def test_identical_snapshot_passes_and_artifacts_exist(client, ids):
    job = _run_job(client, ids)
    f = _make_family(client, "同快照族")
    fid = f["family_id"]
    bl = _loose_baseline(client, fid, job["job_id"])
    assert bl.status_code == 201
    d = _upload_draft(client, fid, _demo_icc(), label="same").json()

    g = client.post(f"/api/release/families/{fid}/gates",
                    json={"draft_version_id": d["version_id"]}).json()
    assert g["status"] == "passed" and g["n_passed"] == 1
    r0 = g["results"][0]
    assert {c["key"] for c in r0["checks"]} == {
        "de_mean", "de_p95", "over_ratio", "changed_pixels"
    }
    assert r0["actual"]["changed_pixels"] == 0
    for nm in ("softproof.png", "mask.png", "diff.png", "changed_pixels.csv"):
        resp = client.get(
            f"/api/release/gates/{g['run_id']}/baselines/{r0['baseline_id']}/{nm}"
        )
        assert resp.status_code == 200, nm
    # 未知产物 404/422
    bad = client.get(
        f"/api/release/gates/{g['run_id']}/baselines/{r0['baseline_id']}/evil.bin"
    )
    assert bad.status_code in (404, 422)


def test_changed_pixels_detected_for_different_profile(client, ids):
    job = _run_job(client, ids)
    stats = job["stats"]
    f = _make_family(client, "异配置族")
    fid = f["family_id"]
    # 参考量级的严格上限：与基线不同的草稿必然失败。
    client.post(
        f"/api/release/families/{fid}/baselines",
        json={
            "name": "tight", "job_id": job["job_id"],
            "limit_de_mean": stats["mean"],
            "limit_de_p95": stats["p95"],
            "limit_over_ratio": stats["over_ratio"],
            "limit_changed_pixels": 0,
        },
    )
    d = _upload_draft(
        client, fid, _demo_icc(description="variant", ink_scale=0.8)
    ).json()
    g = client.post(f"/api/release/families/{fid}/gates",
                    json={"draft_version_id": d["version_id"]}).json()
    assert g["status"] == "failed"
    r0 = g["results"][0]
    assert r0["actual"]["changed_pixels"] > 0
    keys = {c["key"]: c for c in r0["checks"]}
    assert keys["changed_pixels"]["passed"] is False
    assert "超过上限" in keys["changed_pixels"]["reason"]
    # 失败原因逐项可读
    assert any(not c["passed"] for c in r0["checks"])
    assert r0["reason"]


def test_gate_run_is_deterministic_and_reproducible(client, ids):
    job = _run_job(client, ids)
    f = _make_family(client, "确定性族")
    fid = f["family_id"]
    _loose_baseline(client, fid, job["job_id"])
    d = _upload_draft(client, fid, _demo_icc()).json()
    g1 = client.post(f"/api/release/families/{fid}/gates",
                     json={"draft_version_id": d["version_id"]}).json()
    g2 = client.post(f"/api/release/families/{fid}/gates",
                     json={"draft_version_id": d["version_id"]}).json()
    assert g1["run_id"] == g2["run_id"]
    # 历史报告可按 run id 复现
    got = client.get(f"/api/release/gates/{g1['run_id']}").json()
    assert got["results"] == g1["results"]
    assert client.get("/api/release/gates/gate_nope").status_code == 404


def test_publish_then_failed_gate_keeps_draft_and_current(client, ids):
    job = _run_job(client, ids)
    f = _make_family(client, "发布拦截族")
    fid = f["family_id"]
    _loose_baseline(client, fid, job["job_id"])

    d1 = _upload_draft(client, fid, _demo_icc()).json()
    detail = client.get(f"/api/release/families/{fid}").json()
    r = client.post(
        f"/api/release/families/{fid}/publish",
        json={"draft_version_id": d1["version_id"],
              "expected_revision": detail["revision"]},
    )
    assert r.status_code == 200, r.text
    fam = r.json()
    assert fam["current"]["version_no"] == 1
    assert fam["current"]["gate_run_id"]

    # 错误期望版本号 -> 409（发布后 revision 已前进）
    conflict = client.post(
        f"/api/release/families/{fid}/publish",
        json={"draft_version_id": d1["version_id"],
              "expected_revision": detail["revision"]},
    )
    assert conflict.status_code == 409

    # 新异配置草稿门禁失败：发布被拒、草稿保留、current 不变。
    # 追加一个卡在参考量级的严格基线（族内任一基线失败即拒绝发布）。
    stats = job["stats"]
    client.post(
        f"/api/release/families/{fid}/baselines",
        json={
            "name": "tight", "job_id": job["job_id"],
            "limit_de_mean": stats["mean"],
            "limit_de_p95": stats["p95"],
            "limit_over_ratio": stats["over_ratio"],
            "limit_changed_pixels": 0,
        },
    )
    d2 = _upload_draft(
        client, fid, _demo_icc(description="v2", ink_scale=0.85), label="v2"
    ).json()
    detail = client.get(f"/api/release/families/{fid}").json()
    blocked = client.post(
        f"/api/release/families/{fid}/publish",
        json={"draft_version_id": d2["version_id"],
              "expected_revision": detail["revision"]},
    )
    assert blocked.status_code == 422 and "门禁" in blocked.json()["detail"]
    detail = client.get(f"/api/release/families/{fid}").json()
    assert detail["current"]["version_no"] == 1
    assert detail["open_draft"]["version_id"] == d2["version_id"]


def test_published_profile_is_selectable_and_immutable(client, ids):
    job = _run_job(client, ids)
    f = _make_family(client, "上线族")
    fid = f["family_id"]
    _loose_baseline(client, fid, job["job_id"])
    d = _upload_draft(client, fid, _demo_icc()).json()
    detail = client.get(f"/api/release/families/{fid}").json()
    pub = client.post(
        f"/api/release/families/{fid}/publish",
        json={"draft_version_id": d["version_id"],
              "expected_revision": detail["revision"]},
    ).json()
    sha = pub["current"]["profile_sha256"]
    live = [p for p in client.get("/api/profiles").json()["press"]
            if p["profile_id"] == f"release-{sha[:16]}"]
    assert len(live) == 1
    # 用已发布配置跑软打样：任务可正常生成（进行中/历史任务绑定该不可变文件）
    j = _run_job(client, ids, press=live[0]["profile_id"])
    assert client.get(f"/api/jobs/{j['job_id']}/report").status_code == 200
    # 版本 ICC 可下载且字节与快照一致
    blob = client.get(f"/api/release/versions/{pub['current']['version_id']}/icc").content
    import hashlib
    assert hashlib.sha256(blob).hexdigest() == sha


def test_supersede_withdraw_restore_and_concurrency(client, ids):
    job = _run_job(client, ids)
    f = _make_family(client, "回滚族")
    fid = f["family_id"]
    _loose_baseline(client, fid, job["job_id"])

    def publish_draft(icc, label):
        d = _upload_draft(client, fid, icc, label=label).json()
        detail = client.get(f"/api/release/families/{fid}").json()
        r = client.post(
            f"/api/release/families/{fid}/publish",
            json={"draft_version_id": d["version_id"],
                  "expected_revision": detail["revision"], "label": label},
        )
        assert r.status_code == 200, r.text
        return r.json()

    p1 = publish_draft(_demo_icc(), "v1")
    p2 = publish_draft(_demo_icc(ink_scale=0.9), "v2")
    assert p2["current"]["version_no"] == 2
    detail = client.get(f"/api/release/families/{fid}").json()
    statuses = {v["version_no"]: v["status"] for v in detail["versions"]}
    assert statuses == {1: "superseded", 2: "current"}

    v1 = next(v["version_id"] for v in detail["versions"] if v["version_no"] == 1)
    v2 = next(v["version_id"] for v in detail["versions"] if v["version_no"] == 2)

    # 撤回非当前版本被拒
    r = client.post(f"/api/release/versions/{v1}/withdraw",
                    json={"expected_revision": detail["revision"]})
    assert r.status_code == 422

    # 期望版本号过期 -> 409
    stale = client.post(
        f"/api/release/versions/{v2}/withdraw",
        json={"expected_revision": detail["revision"] - 1},
    )
    assert stale.status_code == 409

    # 撤回当前 v2（v1 仍为 superseded，族暂时没有 current）
    wr = client.post(
        f"/api/release/versions/{v2}/withdraw",
        json={"expected_revision": detail["revision"], "reason": "撤回测试"},
    ).json()
    assert wr["current"] is None
    detail = client.get(f"/api/release/families/{fid}").json()
    statuses = {v["version_no"]: v["status"] for v in detail["versions"]}
    assert statuses == {1: "superseded", 2: "withdrawn"}

    # 恢复（回滚）到 v1（已撤回的 v2 保留 withdrawn 状态）
    rs = client.post(
        f"/api/release/versions/{v1}/restore",
        json={"expected_revision": detail["revision"], "reason": "回滚"},
    ).json()
    assert rs["current"]["version_no"] == 1
    detail = client.get(f"/api/release/families/{fid}").json()
    statuses = {v["version_no"]: v["status"] for v in detail["versions"]}
    assert statuses == {1: "current", 2: "withdrawn"}
    # 已恢复版本重复恢复被拒
    r = client.post(f"/api/release/versions/{v1}/restore",
                    json={"expected_revision": detail["revision"]})
    assert r.status_code == 422

    # 撤回的版本也可以再次恢复（重新上线）
    detail = client.get(f"/api/release/families/{fid}").json()
    rs2 = client.post(
        f"/api/release/versions/{v2}/restore",
        json={"expected_revision": detail["revision"], "reason": "重新上线 v2"},
    ).json()
    assert rs2["current"]["version_no"] == 2
    detail = client.get(f"/api/release/families/{fid}").json()
    statuses = {v["version_no"]: v["status"] for v in detail["versions"]}
    assert statuses == {1: "superseded", 2: "current"}


def test_audit_log_is_immutable_and_chained(client, ids):
    job = _run_job(client, ids)
    f = _make_family(client, "审计族")
    fid = f["family_id"]
    _loose_baseline(client, fid, job["job_id"])
    d = _upload_draft(client, fid, _demo_icc()).json()
    detail = client.get(f"/api/release/families/{fid}").json()
    client.post(
        f"/api/release/families/{fid}/publish",
        json={"draft_version_id": d["version_id"],
              "expected_revision": detail["revision"]},
    )
    rows = client.get(f"/api/release/audit?family_id={fid}").json()
    actions = [r["action"] for r in reversed(rows)]
    assert actions == ["family_created", "baseline_created",
                       "draft_uploaded", "published"]
    # 哈希链
    prev = None
    for r in reversed(rows):
        assert r["prev_hash"] == prev
        prev = r["entry_hash"]

    # SQLite 层禁止 UPDATE / DELETE
    from app import config as _config
    conn = sqlite3.connect(str(_config.DB_PATH))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET action='tampered'")
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log")
    conn.rollback()
    conn.close()


def test_baseline_validation(client, ids):
    job = _run_job(client, ids)
    f = _make_family(client, "基线参数族")
    fid = f["family_id"]
    # 负的 ΔE 上限
    r = client.post(
        f"/api/release/families/{fid}/baselines",
        json={"name": "bad", "job_id": job["job_id"],
              "limit_de_mean": -1, "limit_de_p95": 1,
              "limit_over_ratio": 0.5, "limit_changed_pixels": 1},
    )
    assert r.status_code == 422
    # 超界面积比例越界
    r = client.post(
        f"/api/release/families/{fid}/baselines",
        json={"name": "bad", "job_id": job["job_id"],
              "limit_de_mean": 1, "limit_de_p95": 1,
              "limit_over_ratio": 2.0, "limit_changed_pixels": 1},
    )
    assert r.status_code == 422
    # 来源任务不存在
    r = client.post(
        f"/api/release/families/{fid}/baselines",
        json={"name": "bad", "job_id": "deadbeef" * 8,
              "limit_de_mean": 1, "limit_de_p95": 1,
              "limit_over_ratio": 0.5, "limit_changed_pixels": 1},
    )
    assert r.status_code == 422 and "代表性任务" in r.json()["detail"]
