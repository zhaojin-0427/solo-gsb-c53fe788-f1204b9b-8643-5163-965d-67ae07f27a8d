"""印刷配置版本发布与回归门禁（Press-profile release & regression gate）.

核心概念
--------
* **配置族 (family)**：同一台印刷机/同一套工艺的一组配置草稿与已发布版本。
  族上的 ``revision`` 是单调递增的期望版本号（乐观锁），发布/撤回/恢复都必须
  带上调用方期望的 revision，不一致即 409。
* **草稿 (draft)**：上传到族上、尚未发布的候选 ICC。ICC 字节按其 SHA-256
  内容寻址存于 ``/data/release/snapshots/<sha>.icc``，**不可变**。
* **基线 (baseline)**：从已保存的代表性任务固化而来的自包含夹具——图像字节、
  源/显示 ICC 字节（或内嵌 ICC）、渲染意图、阈值与 ΔE/P95/超阈面积/遮罩变化
  像素上限全部固定，存于 ``/data/release/baselines/<id>/``。
* **门禁运行 (gate run)**：服务按草稿快照 SHA-256 取出固定 ICC，对族内每个
  基线重跑流水线，逐项给出 pass/fail、度量值、变化像素定位（坐标 CSV + PNG）
  与中文原因；所有基线全部通过才允许在单个事务中发布。
* **版本发布**：当前版本在事务内变为 superseded，新版本成为 current，并把
  该版 ICC 以不可变文件名 ``release-<sha16>.icc`` 写入 press 配置目录；历史
  任务按其 job_id 绑定原文件/快照，因此“进行中的任务继续绑定原快照”。
* **审计**：每次状态迁移追加一条哈希链记录；audit_log 上有触发器，任何
  UPDATE/DELETE 都被 SQLite 直接拒绝。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from . import database as db
from . import lcms_native, pipeline as pl
from .config import JOB_DIR, PROFILE_DIR, RELEASE_DIR
from .profiles import ProfileManager

SNAPSHOT_DIR = RELEASE_DIR / "snapshots"
BASELINE_DIR = RELEASE_DIR / "baselines"
GATE_DIR = RELEASE_DIR / "gates"
PRESS_LIVE_DIR = PROFILE_DIR / "press"

VERSION_DRAFT = "draft"
VERSION_CURRENT = "current"
VERSION_SUPERSEDED = "superseded"
VERSION_WITHDRAWN = "withdrawn"

# 每次门禁运行定位变化像素时，坐标文件最多保留多少行（像素数上限本身由门禁
# 阈值约束；这里是坐标清单的展示/存储安全阀）。
COORD_CAP = 200_000


class ReleaseError(ValueError):
    """请求语义非法（族/版本/基线不存在、状态不允许等），映射到 400/404/422。"""


class ReleaseNotFound(ReleaseError):
    """资源不存在，映射到 HTTP 404。"""


# ---------------------------------------------------------------------------
# 存储与小工具
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z一-鿿._-]+", "-", name.strip()).strip("-._")
    return s or "family"


def ensure_dirs() -> None:
    for d in (SNAPSHOT_DIR, BASELINE_DIR, GATE_DIR, PRESS_LIVE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def snapshot_path(sha: str) -> Path:
    return SNAPSHOT_DIR / f"{sha}.icc"


def store_snapshot(data: bytes) -> str:
    """内容寻址写入草稿 ICC 快照；同 SHA 重复上传不会产生新文件。"""
    ensure_dirs()
    sha = _sha256(data)
    p = snapshot_path(sha)
    if not p.exists():
        p.write_bytes(data)
    return sha


def validate_press_icc(data: bytes) -> dict:
    """严格校验候选草稿必须是合法的 CMYK 输出配置。"""
    try:
        header = lcms_native.read_header(data)
    except Exception as exc:
        raise ReleaseError(f"ICC 配置无效: {exc}") from exc
    if header["color_space"] != "CMYK" or header["device_class"] not in ("prtr", "spac"):
        raise ReleaseError(
            f"草稿必须是 CMYK 输出配置 (prtr/spac + CMYK)，实际为 "
            f"{header['device_class']}/{header['color_space']}"
        )
    # 让 lcms 真正打开一次，捕获损坏的标签/CLUT。
    try:
        from PIL import ImageCms
        ImageCms.ImageCmsProfile(io.BytesIO(data))
    except Exception as exc:
        raise ReleaseError(f"ICC 配置无法被 LittleCMS 打开: {exc}") from exc
    return header


def profile_name_of(data: bytes) -> str:
    try:
        from PIL import ImageCms
        name = ImageCms.getProfileName(ImageCms.ImageCmsProfile(io.BytesIO(data))).strip()
        if name:
            return name
    except Exception:
        pass
    return "unnamed-press-profile"


# ---------------------------------------------------------------------------
# 审计哈希链
# ---------------------------------------------------------------------------

def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def append_audit(conn: sqlite3.Connection, family_id: str, action: str,
                 revision: int, payload: dict) -> dict:
    """在当前事务内追加一条哈希链审计记录（调用方必须已开启事务）。"""
    prev = conn.execute(
        "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    prev_hash = prev["entry_hash"] if prev else None
    event_id = _new_id("evt")
    body = {
        "event_id": event_id,
        "family_id": family_id,
        "action": action,
        "revision": revision,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    entry_hash = hashlib.sha256(_canonical(body)).hexdigest()
    conn.execute(
        """INSERT INTO audit_log
           (event_id, family_id, action, revision, payload_json, prev_hash, entry_hash)
           VALUES (?,?,?,?,?,?,?)""",
        (event_id, family_id, action, revision,
         json.dumps(payload, sort_keys=True, ensure_ascii=False),
         prev_hash, entry_hash),
    )
    return {"event_id": event_id, "entry_hash": entry_hash}


def list_audit(conn: sqlite3.Connection, family_id: str | None = None,
               limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    if family_id:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE family_id=? ORDER BY seq DESC LIMIT ?",
            (family_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 行序列化
# ---------------------------------------------------------------------------

def _family_dict(conn, row: sqlite3.Row) -> dict:
    d = dict(row)
    cur = None
    if d.get("current_version_id"):
        v = conn.execute(
            "SELECT * FROM press_versions WHERE version_id=?",
            (d["current_version_id"],),
        ).fetchone()
        if v:
            cur = _version_dict(v)
    d["current"] = cur
    d.pop("current_version_id", None)
    draft = conn.execute(
        "SELECT * FROM press_versions WHERE family_id=? AND status=? "
        "ORDER BY created_at DESC, version_id DESC LIMIT 1",
        (d["family_id"], VERSION_DRAFT),
    ).fetchone()
    d["open_draft"] = _version_dict(draft) if draft else None
    d["baseline_count"] = conn.execute(
        "SELECT COUNT(*) c FROM gate_baselines WHERE family_id=?", (d["family_id"],)
    ).fetchone()["c"]
    d["version_count"] = conn.execute(
        "SELECT COUNT(*) c FROM press_versions WHERE family_id=? AND version_no IS NOT NULL",
        (d["family_id"],),
    ).fetchone()["c"]
    return d


def _version_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    return d


def _baseline_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["use_embedded"] = bool(d["use_embedded"])
    d["limits"] = {
        "de_mean": d.pop("limit_de_mean"),
        "de_p95": d.pop("limit_de_p95"),
        "over_ratio": d.pop("limit_over_ratio"),
        "changed_pixels": d.pop("limit_changed_pixels"),
    }
    return d


def _get_family(conn, family_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM press_families WHERE family_id=?", (family_id,)
    ).fetchone()
    if not row:
        raise ReleaseNotFound(f"配置族不存在: {family_id}")
    return row


def _get_version(conn, version_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM press_versions WHERE version_id=?", (version_id,)
    ).fetchone()
    if not row:
        raise ReleaseNotFound(f"版本/草稿不存在: {version_id}")
    return row


# ---------------------------------------------------------------------------
# 配置族与草稿
# ---------------------------------------------------------------------------

def list_families(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM press_families ORDER BY created_at DESC").fetchall()
    return [_family_dict(conn, r) for r in rows]


def get_family_detail(conn: sqlite3.Connection, family_id: str) -> dict:
    fam = _family_dict(conn, _get_family(conn, family_id))
    versions = conn.execute(
        "SELECT * FROM press_versions WHERE family_id=? "
        "ORDER BY COALESCE(version_no, 0) DESC, created_at DESC",
        (family_id,),
    ).fetchall()
    fam["versions"] = [_version_dict(v) for v in versions]
    baselines = conn.execute(
        "SELECT * FROM gate_baselines WHERE family_id=? ORDER BY created_at DESC",
        (family_id,),
    ).fetchall()
    fam["baselines"] = [_baseline_dict(b) for b in baselines]
    runs = conn.execute(
        "SELECT run_id, draft_version_id, draft_profile_sha256, status, "
        "n_baselines, n_passed, created_at FROM gate_runs "
        "WHERE family_id=? ORDER BY created_at DESC LIMIT 30",
        (family_id,),
    ).fetchall()
    fam["recent_runs"] = [dict(r) for r in runs]
    return fam


def create_family(conn: sqlite3.Connection, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ReleaseError("配置族名称不能为空")
    family_id = f"{_slug(name)[:40]}-{secrets.token_hex(4)}"
    with db.transaction(conn):
        exists = conn.execute(
            "SELECT 1 FROM press_families WHERE name=?", (name,)
        ).fetchone()
        if exists:
            raise ReleaseError(f"配置族名称已存在: {name}")
        conn.execute(
            "INSERT INTO press_families (family_id, name, revision) VALUES (?, ?, 1)",
            (family_id, name),
        )
        append_audit(conn, family_id, "family_created", 1, {"name": name})
    return _family_dict(conn, _get_family(conn, family_id))


def upload_draft(conn: sqlite3.Connection, family_id: str, data: bytes,
                 filename: str, label: str | None = None) -> dict:
    """把同一配置族的新候选 ICC 存为草稿；旧草稿保留记录但不再开放。"""
    ensure_dirs()
    _get_family(conn, family_id)
    validate_press_icc(data)
    sha = store_snapshot(data)

    with db.transaction(conn):
        fam = _get_family(conn, family_id)
        # 同一族重复上传完全相同的快照：幂等返回已有草稿/版本。
        dup = conn.execute(
            "SELECT * FROM press_versions WHERE family_id=? AND profile_sha256=?",
            (family_id, sha),
        ).fetchone()
        if dup:
            return _version_dict(dup)
        # 关闭旧开放草稿（记录保留，状态标 superseded_draft 语义由“非 draft”表达）。
        conn.execute(
            "UPDATE press_versions SET status=? WHERE family_id=? AND status=?",
            (VERSION_SUPERSEDED, family_id, VERSION_DRAFT),
        )
        version_id = _new_id("ver")
        conn.execute(
            """INSERT INTO press_versions
               (version_id, family_id, version_no, status, profile_sha256,
                profile_size, profile_name, label)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?)""",
            (version_id, family_id, VERSION_DRAFT, sha, len(data),
             profile_name_of(data), (label or "").strip() or None),
        )
        new_rev = fam["revision"] + 1
        conn.execute(
            "UPDATE press_families SET revision=?, updated_at=? WHERE family_id=?",
            (new_rev, _now(), family_id),
        )
        append_audit(conn, family_id, "draft_uploaded", new_rev, {
            "version_id": version_id,
            "profile_sha256": sha,
            "profile_size": len(data),
            "filename": filename,
            "label": (label or "").strip() or None,
        })
    return _version_dict(_get_version(conn, version_id))


def download_draft(conn, version_id: str) -> tuple[Path, str]:
    v = _get_version(conn, version_id)
    p = snapshot_path(v["profile_sha256"])
    if not p.exists():
        raise ReleaseError("草稿快照文件缺失（存储不可用）")
    return p, f"{v['version_id']}.icc"


# ---------------------------------------------------------------------------
# 基线（从已保存的代表性任务固化）
# ---------------------------------------------------------------------------

@dataclass
class GateLimits:
    de_mean: float
    de_p95: float
    over_ratio: float
    changed_pixels: int

    @staticmethod
    def parse(d: dict) -> "GateLimits":
        try:
            lim = GateLimits(
                de_mean=float(d["de_mean"]),
                de_p95=float(d["de_p95"]),
                over_ratio=float(d["over_ratio"]),
                changed_pixels=int(d["changed_pixels"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseError(f"门禁上限参数无效: {exc}") from exc
        if lim.de_mean < 0 or lim.de_p95 < 0 or not (0.0 <= lim.over_ratio <= 1.0):
            raise ReleaseError("ΔE 上限不能为负，超阈面积比例必须在 0..1 之间")
        if lim.changed_pixels < 0:
            raise ReleaseError("遮罩变化像素上限不能为负")
        return lim


def create_baseline(conn: sqlite3.Connection, pm: ProfileManager, family_id: str,
                    name: str, job_id: str, limits: GateLimits) -> dict:
    """从一个已保存任务固化自包含基线夹具。

    固定内容：原始图像字节、源 ICC（按 ID 解析或图像内嵌块）、显示 ICC、
    两个渲染意图与阈值；以及基线自己的印刷 ICC 摘要（仅用于校验，重跑时
    总是替换为草稿快照）。
    """
    _get_family(conn, family_id)
    name = (name or "").strip()
    if not name:
        raise ReleaseError("基线名称不能为空")

    job_base = JOB_DIR / job_id
    meta_path = job_base / "meta.json"
    if not meta_path.exists():
        raise ReleaseError(f"代表性任务不存在或已被删除: {job_id}")
    meta = json.loads(meta_path.read_text("utf-8"))
    image_data = (job_base / "original.bin").read_bytes()
    mask_ref = np.load(job_base / "mask3.npy")[..., 0]

    use_embedded = bool(meta.get("use_embedded"))
    if use_embedded:
        embedded = meta.get("embedded_sha256")
        if not embedded:
            raise ReleaseError("该任务使用内嵌源配置但缺少内嵌 ICC 摘要，无法固化基线")
        # 原始内嵌块就嵌在上传图像里，重跑时由 use_embedded 直接取出。
        source_icc = b""
        source_id = "<embedded>"
        source_sha = embedded
    else:
        source_id = meta["source_profile"]
        source_icc = pm.load_bytes(source_id)
        source_sha = _sha256(source_icc)
    display_id = meta["display_profile"]
    display_icc = pm.load_bytes(display_id)
    display_sha = _sha256(display_icc)
    press_id = meta["press_profile"]
    press_icc = pm.load_bytes(press_id)
    press_sha = _sha256(press_icc)

    baseline_id = _new_id("base")
    bdir = BASELINE_DIR / baseline_id
    bdir.mkdir(parents=True, exist_ok=False)
    (bdir / "image.bin").write_bytes(image_data)
    (bdir / "source.icc").write_bytes(source_icc)
    (bdir / "display.icc").write_bytes(display_icc)
    (bdir / "press-ref.icc").write_bytes(press_icc)
    np.save(bdir / "mask_ref.npy", mask_ref.astype(np.uint8))

    try:
        # 尺寸/模式取自流水线实际处理结果（RGBA 已合成为 RGB），
        # 这里打开原图仅做完整性确认。
        with Image.open(io.BytesIO(image_data)) as im:
            im.verify()
    except Exception as exc:
        raise ReleaseError(f"基线图像无法解析: {exc}") from exc
    width, height, mode = int(meta["width"]), int(meta["height"]), meta["mode"]

    with db.transaction(conn):
        conn.execute(
            """INSERT INTO gate_baselines
               (baseline_id, family_id, name, source_job_id, image_sha256,
                width, height, image_mode, source_profile_id, press_profile_id,
                display_profile_id, source_profile_sha256, press_profile_sha256,
                display_profile_sha256, source_intent, proof_intent, threshold,
                use_embedded, limit_de_mean, limit_de_p95, limit_over_ratio,
                limit_changed_pixels)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (baseline_id, family_id, name, job_id, _sha256(image_data),
             width, height, mode, source_id, press_id, display_id,
             source_sha, press_sha, display_sha,
             meta["source_intent"], meta["proof_intent"], float(meta["threshold"]),
             1 if use_embedded else 0,
             limits.de_mean, limits.de_p95, limits.over_ratio,
             limits.changed_pixels),
        )
        fam = _get_family(conn, family_id)
        new_rev = fam["revision"] + 1
        conn.execute(
            "UPDATE press_families SET revision=?, updated_at=? WHERE family_id=?",
            (new_rev, _now(), family_id),
        )
        append_audit(conn, family_id, "baseline_created", new_rev, {
            "baseline_id": baseline_id,
            "name": name,
            "source_job_id": job_id,
            "image_sha256": _sha256(image_data),
            "limits": {
                "de_mean": limits.de_mean,
                "de_p95": limits.de_p95,
                "over_ratio": limits.over_ratio,
                "changed_pixels": limits.changed_pixels,
            },
        })
    return _baseline_dict(conn.execute(
        "SELECT * FROM gate_baselines WHERE baseline_id=?", (baseline_id,)
    ).fetchone())


def list_baselines(conn, family_id: str) -> list[dict]:
    _get_family(conn, family_id)
    rows = conn.execute(
        "SELECT * FROM gate_baselines WHERE family_id=? ORDER BY created_at DESC",
        (family_id,),
    ).fetchall()
    return [_baseline_dict(r) for r in rows]


def _load_fixture(conn, b: sqlite3.Row) -> dict:
    bdir = BASELINE_DIR / b["baseline_id"]
    if not bdir.exists():
        raise ReleaseError(f"基线夹具目录缺失: {b['baseline_id']}")
    image_data = (bdir / "image.bin").read_bytes()
    if _sha256(image_data) != b["image_sha256"]:
        raise ReleaseError(f"基线图像哈希校验失败: {b['baseline_id']}")
    source_icc = (bdir / "source.icc").read_bytes()
    display_icc = (bdir / "display.icc").read_bytes()
    if not b["use_embedded"]:
        if not source_icc or _sha256(source_icc) != b["source_profile_sha256"]:
            raise ReleaseError(f"基线源配置快照校验失败: {b['baseline_id']}")
    if _sha256(display_icc) != b["display_profile_sha256"]:
        raise ReleaseError(f"基线显示配置快照校验失败: {b['baseline_id']}")
    return {
        "image_data": image_data,
        "source_icc": source_icc if not b["use_embedded"] else None,
        "display_icc": display_icc,
        "mask_ref": np.load(bdir / "mask_ref.npy"),
        "dir": bdir,
    }


# ---------------------------------------------------------------------------
# 门禁重跑
# ---------------------------------------------------------------------------

def _gate_run_id(draft_sha: str, baseline_ids: list[str]) -> str:
    h = hashlib.sha256()
    h.update(draft_sha.encode())
    for bid in sorted(baseline_ids):
        h.update(bid.encode())
    return "gate_" + h.hexdigest()[:32]


def _evaluate_checks(stats: dict, ref: dict, mask_changed: np.ndarray,
                     limits: GateLimits) -> list[dict]:
    """逐项判定四个门禁指标，返回 pass/fail + 实测/上限/原因。

    浮点指标的实测值与“允许上限”比较时留 1e-9 的舍入容差（显示值仍是
    四舍五入后的结果）：上限 0 的语义是“与基线完全一致”，同字节快照重跑
    得到位级相同结果，不会因为 JSON 序列化精度而误判。
    """
    n_changed = int(mask_changed.sum())
    eps = 1e-9
    checks = [
        {
            "key": "de_mean",
            "name": "ΔE 均值",
            "actual": round(stats["mean"], 6),
            "reference": round(ref["de_mean"], 6),
            "limit": limits.de_mean,
            "passed": stats["mean"] <= limits.de_mean + eps,
        },
        {
            "key": "de_p95",
            "name": "ΔE P95",
            "actual": round(stats["p95"], 6),
            "reference": round(ref["de_p95"], 6),
            "limit": limits.de_p95,
            "passed": stats["p95"] <= limits.de_p95 + eps,
        },
        {
            "key": "over_ratio",
            "name": "超阈面积比例",
            "actual": round(stats["over_ratio"], 8),
            "reference": round(ref["over_ratio"], 8),
            "limit": limits.over_ratio,
            "passed": stats["over_ratio"] <= limits.over_ratio + eps,
        },
        {
            "key": "changed_pixels",
            "name": "遮罩变化像素",
            "actual": n_changed,
            "reference": 0,
            "limit": limits.changed_pixels,
            "passed": n_changed <= limits.changed_pixels,
        },
    ]
    for c in checks:
        if c["passed"]:
            c["reason"] = f"{c['name']} {c['actual']} ≤ 上限 {c['limit']}"
        else:
            if c["key"] == "changed_pixels":
                c["reason"] = (
                    f"遮罩发生变化的像素 {c['actual']} 个，超过上限 "
                    f"{c['limit']} 个（与基线二值遮罩逐像素异或）"
                )
            else:
                c["reason"] = (
                    f"{c['name']} 实测 {c['actual']}，基线参考 {c['reference']}，"
                    f"超过允许上限 {c['limit']}"
                )
    return checks


def _diff_mask_png(mask_ref: np.ndarray, mask_new: np.ndarray,
                   mask_changed: np.ndarray, srgb_icc: bytes) -> bytes:
    """三色定位图：灰=一致，红=新增超阈像素，蓝=消失超阈像素。"""
    h, w = mask_changed.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    both = (mask_ref > 0) & (mask_new > 0)
    gone = (mask_ref > 0) & (mask_new == 0)   # 基线超阈、候选不再超阈
    new = (mask_ref == 0) & (mask_new > 0)    # 候选新增超阈
    rgb[both] = (150, 150, 150)
    rgb[gone] = (60, 120, 230)
    rgb[new] = (230, 60, 60)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", icc_profile=srgb_icc)
    return buf.getvalue()


def run_gate(conn: sqlite3.Connection, pm: ProfileManager, srgb_bytes: bytes,
             family_id: str, draft_version_id: str) -> dict:
    """按草稿 ICC 的 SHA-256 固定快照，对族内全部基线重跑并逐项判定。

    结果确定性：run_id = SHA(草稿快照, 基线集合)；同输入重跑复用同一记录与
    产物目录，历史门禁报告因此可按版本复现。
    """
    ensure_dirs()
    with db.transaction(conn):
        fam = _get_family(conn, family_id)
        draft = _get_version(conn, draft_version_id)
        if draft["family_id"] != family_id or draft["status"] != VERSION_DRAFT:
            raise ReleaseError("只能对当前开放草稿运行门禁")
        snap = snapshot_path(draft["profile_sha256"])
        if not snap.exists():
            raise ReleaseError("草稿 ICC 快照缺失，无法重跑")
        candidate_icc = snap.read_bytes()
        if _sha256(candidate_icc) != draft["profile_sha256"]:
            raise ReleaseError("草稿快照 SHA-256 校验失败（快照被篡改）")

        baselines = conn.execute(
            "SELECT * FROM gate_baselines WHERE family_id=? ORDER BY created_at",
            (family_id,),
        ).fetchall()
        if not baselines:
            raise ReleaseError("该配置族还没有基线，请先从代表性任务固化一个基线")

        run_id = _gate_run_id(
            draft["profile_sha256"], [b["baseline_id"] for b in baselines]
        )
        existing = conn.execute(
            "SELECT * FROM gate_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing:
            return get_gate_run(conn, run_id)

        rdir = GATE_DIR / run_id
        rdir.mkdir(parents=True, exist_ok=False)

        results = []
        n_passed = 0
        for b in baselines:
            fx = _load_fixture(conn, b)
            inputs = pl.PipelineInputs(
                image_data=fx["image_data"],
                source_profile_id=b["source_profile_id"],
                press_profile_id="<draft-snapshot>",
                display_profile_id=b["display_profile_id"],
                source_intent=b["source_intent"],
                proof_intent=b["proof_intent"],
                threshold=float(b["threshold"]),
                use_embedded=bool(b["use_embedded"]),
                source_profile_bytes=fx["source_icc"],
                press_profile_bytes=candidate_icc,
                display_profile_bytes=fx["display_icc"],
            )
            result = pl.run_pipeline(inputs, pm)

            mask_ref = fx["mask_ref"].astype(np.uint8)
            mask_new = result.mask[..., 0].astype(np.uint8)
            mask_changed = (mask_ref > 0) != (mask_new > 0)
            n_changed = int(mask_changed.sum())

            # 参考统计：基线任务在“原印刷配置”下的度量（直接取自已保存任务）。
            ref_job = b["source_job_id"]
            ref_report = json.loads(
                (JOB_DIR / ref_job / "report.json").read_text("utf-8")
            ) if (JOB_DIR / ref_job / "report.json").exists() else None
            if ref_report:
                ref_stats = ref_report["stats"]
                ref = {
                    "de_mean": ref_stats["mean"],
                    "de_p95": ref_stats["p95"],
                    "over_ratio": ref_stats["over_ratio"],
                }
            else:
                # 任务目录被清理时，仍可用基线夹具里的参考印刷 ICC 复算参考值。
                ref_icc = (fx["dir"] / "press-ref.icc").read_bytes()
                ref_inputs = pl.PipelineInputs(
                    image_data=fx["image_data"],
                    source_profile_id=b["source_profile_id"],
                    press_profile_id=b["press_profile_id"],
                    display_profile_id=b["display_profile_id"],
                    source_intent=b["source_intent"],
                    proof_intent=b["proof_intent"],
                    threshold=float(b["threshold"]),
                    use_embedded=bool(b["use_embedded"]),
                    source_profile_bytes=fx["source_icc"],
                    press_profile_bytes=ref_icc,
                    display_profile_bytes=fx["display_icc"],
                )
                ref_result = pl.run_pipeline(ref_inputs, pm)
                ref = {
                    "de_mean": ref_result.stats["mean"],
                    "de_p95": ref_result.stats["p95"],
                    "over_ratio": ref_result.stats["over_ratio"],
                }

            limits = GateLimits(
                b["limit_de_mean"], b["limit_de_p95"],
                b["limit_over_ratio"], b["limit_changed_pixels"],
            )
            checks = _evaluate_checks(result.stats, ref, mask_changed, limits)
            passed = all(c["passed"] for c in checks)
            n_passed += 1 if passed else 0

            # 产物：候选软打样 / 候选超阈遮罩 / 变化像素定位图 / 坐标清单。
            bdir_out = rdir / b["baseline_id"]
            bdir_out.mkdir(parents=True, exist_ok=True)
            pngs = pl.export_pngs(result, srgb_bytes, fx["display_icc"])
            (bdir_out / "softproof.png").write_bytes(pngs["softproof.png"])
            (bdir_out / "mask.png").write_bytes(pngs["mask.png"])
            (bdir_out / "diff.png").write_bytes(
                _diff_mask_png(mask_ref, mask_new, mask_changed, srgb_bytes)
            )
            coords = np.argwhere(mask_changed)  # N x 2 (y, x)
            truncated = False
            if len(coords) > COORD_CAP:
                coords = coords[:COORD_CAP]
                truncated = True
            with (bdir_out / "changed_pixels.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["y", "x", "kind"])
                ref_flat = mask_ref > 0
                new_flat = mask_new > 0
                for y, x in coords:
                    kind = "new_over_threshold" if (not ref_flat[y, x] and new_flat[y, x]) else "cleared"
                    w.writerow([int(y), int(x), kind])
            np.save(bdir_out / "mask_changed.npy", mask_changed.astype(np.uint8))

            item = {
                "baseline_id": b["baseline_id"],
                "baseline_name": b["name"],
                "source_job_id": b["source_job_id"],
                "image": {
                    "sha256": b["image_sha256"],
                    "width": b["width"],
                    "height": b["height"],
                    "mode": b["image_mode"],
                },
                "parameters": {
                    "source_intent": b["source_intent"],
                    "proof_intent": b["proof_intent"],
                    "threshold": b["threshold"],
                    "use_embedded": bool(b["use_embedded"]),
                },
                "limits": {
                    "de_mean": limits.de_mean,
                    "de_p95": limits.de_p95,
                    "over_ratio": limits.over_ratio,
                    "changed_pixels": limits.changed_pixels,
                },
                "reference": ref,
                "actual": {
                    "de_mean": result.stats["mean"],
                    "de_p95": result.stats["p95"],
                    "de_max": result.stats["max"],
                    "over_ratio": result.stats["over_ratio"],
                    "over_pixels": result.stats["over_pixels"],
                    "total_pixels": result.stats["total_pixels"],
                    "changed_pixels": n_changed,
                },
                "checks": checks,
                "passed": passed,
                "reason": "全部检查项通过" if passed else "；".join(
                    c["reason"] for c in checks if not c["passed"]
                ),
            }
            results.append(item)

        overall = n_passed == len(baselines)
        report = {
            "run_id": run_id,
            "family_id": family_id,
            "family_name": fam["name"],
            "draft_version_id": draft_version_id,
            "draft_profile_sha256": draft["profile_sha256"],
            "draft_profile_name": draft["profile_name"],
            "status": "passed" if overall else "failed",
            "n_baselines": len(baselines),
            "n_passed": n_passed,
            "results": results,
        }
        (rdir / "report.json").write_text(
            json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2),
            "utf-8",
        )
        conn.execute(
            """INSERT INTO gate_runs
               (run_id, family_id, draft_version_id, draft_profile_sha256,
                status, n_baselines, n_passed, results_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, family_id, draft_version_id, draft["profile_sha256"],
             report["status"], len(baselines), n_passed,
             json.dumps(results, sort_keys=True, ensure_ascii=False)),
        )

    return get_gate_run(conn, run_id)


def get_gate_run(conn: sqlite3.Connection, run_id: str) -> dict:
    row = conn.execute("SELECT * FROM gate_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise ReleaseNotFound(f"门禁运行不存在: {run_id}")
    report_path = GATE_DIR / run_id / "report.json"
    if report_path.exists():
        return json.loads(report_path.read_text("utf-8"))
    d = dict(row)
    d["results"] = json.loads(d.pop("results_json"))
    return d


def gate_artifact(conn: sqlite3.Connection, run_id: str, baseline_id: str,
                  name: str) -> Path:
    get_gate_run(conn, run_id)
    allowed = {"softproof.png", "mask.png", "diff.png", "changed_pixels.csv"}
    if name not in allowed:
        raise ReleaseError("门禁产物不存在")
    p = GATE_DIR / run_id / baseline_id / name
    if not p.exists():
        raise ReleaseError("门禁产物不存在")
    return p


# ---------------------------------------------------------------------------
# 发布 / 撤回 / 恢复（期望版本号并发控制 + 事务 + 不可变审计）
# ---------------------------------------------------------------------------

def _check_expected(fam: sqlite3.Row, expected_revision: int | None) -> None:
    if expected_revision is None:
        raise ReleaseError("缺少 expected_revision（期望版本号）")
    if int(expected_revision) != int(fam["revision"]):
        from .database import RevisionConflict
        raise RevisionConflict(
            f"期望版本号 {expected_revision} 与当前版本号 {fam['revision']} 不一致，"
            "状态已被其他操作改变，请刷新后重试"
        )


def publish(conn: sqlite3.Connection, pm: ProfileManager, srgb_bytes: bytes,
            family_id: str, draft_version_id: str,
            expected_revision: int | None, label: str | None = None) -> dict:
    """全部门禁通过后，在单个事务内把草稿发布为当前版本。

    门禁重跑（CPU 密集）单独成事务先持久化结果——失败的门禁也必须留下可
    复现的报告；发布本身是一个短事务：二次校验期望版本号与草稿状态后，
    原子地完成 current 切换、版本号分配与审计追加。
    """
    fam = _get_family(conn, family_id)
    _check_expected(fam, expected_revision)
    draft = _get_version(conn, draft_version_id)
    if draft["family_id"] != family_id:
        raise ReleaseError("草稿不属于该配置族")

    report = run_gate(conn, pm, srgb_bytes, family_id, draft_version_id)
    if report["status"] != "passed":
        failed = [r["baseline_name"] for r in report["results"] if not r["passed"]]
        raise ReleaseError(
            "门禁未全部通过，保留草稿，拒绝发布；失败基线：" + "、".join(failed)
        )

    sha = draft["profile_sha256"]
    with db.transaction(conn):
        fam = _get_family(conn, family_id)
        _check_expected(fam, expected_revision)
        draft = _get_version(conn, draft_version_id)
        if draft["status"] != VERSION_DRAFT:
            raise ReleaseError("草稿状态已改变（可能已发布或被新草稿取代），请刷新后重试")

        # 把该版 ICC 以不可变文件名投入 press 目录（内容寻址；旧文件永不覆盖）。
        ensure_dirs()
        live_name = f"release-{sha[:16]}.icc"
        live_path = PRESS_LIVE_DIR / live_name
        if not live_path.exists():
            live_path.write_bytes(snapshot_path(sha).read_bytes())

        next_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM press_versions "
            "WHERE family_id=?",
            (family_id,),
        ).fetchone()[0]

        # 当前版本 -> 被取代（同事务）。
        conn.execute(
            "UPDATE press_versions SET status=? WHERE family_id=? AND status=?",
            (VERSION_SUPERSEDED, family_id, VERSION_CURRENT),
        )
        conn.execute(
            """UPDATE press_versions
               SET status=?, version_no=?, label=COALESCE(?, label),
                   published_at=?, gate_run_id=?
               WHERE version_id=?""",
            (VERSION_CURRENT, next_no, (label or "").strip() or None,
             _now(), report["run_id"], draft_version_id),
        )
        new_rev = fam["revision"] + 1
        conn.execute(
            "UPDATE press_families SET revision=?, current_version_id=? "
            "WHERE family_id=?",
            (new_rev, draft_version_id, family_id),
        )
        append_audit(conn, family_id, "published", new_rev, {
            "version_id": draft_version_id,
            "version_no": next_no,
            "profile_sha256": sha,
            "profile_name": draft["profile_name"],
            "gate_run_id": report["run_id"],
            "n_baselines": report["n_baselines"],
            "live_profile_file": live_name,
            "label": (label or "").strip() or draft["label"],
        })

    # 让新发布配置立即出现在软打样配置下拉里（仅刷新 DB 索引，文件不可变）。
    try:
        for rec in pm.list_profiles():
            if rec.role == "press":
                db.upsert_profile(conn, rec)
    except Exception:
        pass

    out = _family_dict(conn, _get_family(conn, family_id))
    out["gate_run"] = report
    return out


def withdraw(conn: sqlite3.Connection, family_id: str, version_id: str,
             expected_revision: int | None, reason: str | None = None) -> dict:
    """撤回当前版本（撤回后族没有 current；历史快照与报告仍可复现）。"""
    with db.transaction(conn):
        fam = _get_family(conn, family_id)
        _check_expected(fam, expected_revision)
        v = _get_version(conn, version_id)
        if v["family_id"] != family_id:
            raise ReleaseError("版本不属于该配置族")
        if v["status"] != VERSION_CURRENT:
            raise ReleaseError(f"只能撤回当前版本，该版本状态为 {v['status']}")
        conn.execute(
            "UPDATE press_versions SET status=?, withdrawn_at=? WHERE version_id=?",
            (VERSION_WITHDRAWN, _now(), version_id),
        )
        new_rev = fam["revision"] + 1
        conn.execute(
            "UPDATE press_families SET revision=?, current_version_id=NULL "
            "WHERE family_id=?",
            (new_rev, family_id),
        )
        append_audit(conn, family_id, "withdrawn", new_rev, {
            "version_id": version_id,
            "version_no": v["version_no"],
            "profile_sha256": v["profile_sha256"],
            "reason": (reason or "").strip() or None,
        })
    return _family_dict(conn, _get_family(conn, family_id))


def restore(conn: sqlite3.Connection, family_id: str, version_id: str,
            expected_revision: int | None, reason: str | None = None) -> dict:
    """恢复（回滚）到一个已发布但不在当前位的历史版本。

    被取代/已撤回的版本都可恢复；恢复即把它重新置为 current，原 current
    （若有）回到 superseded。恢复不需要重跑门禁——该版本发布时的门禁报告
    按 gate_run_id 仍可完整复现。
    """
    with db.transaction(conn):
        fam = _get_family(conn, family_id)
        _check_expected(fam, expected_revision)
        v = _get_version(conn, version_id)
        if v["family_id"] != family_id:
            raise ReleaseError("版本不属于该配置族")
        if v["version_no"] is None:
            raise ReleaseError("草稿不能作为版本恢复")
        if v["status"] == VERSION_CURRENT:
            raise ReleaseError("该版本已经是当前版本，无需恢复")
        if v["status"] not in (VERSION_SUPERSEDED, VERSION_WITHDRAWN):
            raise ReleaseError(f"当前状态 {v['status']} 不允许恢复")

        previous_current = conn.execute(
            "SELECT version_id FROM press_versions WHERE family_id=? AND status=?",
            (family_id, VERSION_CURRENT),
        ).fetchone()
        conn.execute(
            "UPDATE press_versions SET status=? WHERE family_id=? AND status=?",
            (VERSION_SUPERSEDED, family_id, VERSION_CURRENT),
        )
        conn.execute(
            "UPDATE press_versions SET status=?, withdrawn_at=NULL WHERE version_id=?",
            (VERSION_CURRENT, version_id),
        )
        new_rev = fam["revision"] + 1
        conn.execute(
            "UPDATE press_families SET revision=?, current_version_id=? "
            "WHERE family_id=?",
            (new_rev, version_id, family_id),
        )
        append_audit(conn, family_id, "restored", new_rev, {
            "version_id": version_id,
            "version_no": v["version_no"],
            "profile_sha256": v["profile_sha256"],
            "previous_current_version_id":
                previous_current["version_id"] if previous_current else None,
            "gate_run_id": v["gate_run_id"],
            "reason": (reason or "").strip() or None,
        })
    return _family_dict(conn, _get_family(conn, family_id))
