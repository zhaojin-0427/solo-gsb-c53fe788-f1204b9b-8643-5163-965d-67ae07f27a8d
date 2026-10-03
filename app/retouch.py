"""局部 CMYK 修色（local CMYK retouch）。

以一个**已保存软打样任务**的分色结果为起点，在不超过 64×64 的选区内
搜索一组对全选区统一生效的**整数 CMYK 偏移** ``(dc, dm, dy, dk)``，
使选区像素沿**原任务的印刷 ICC** 回算出的平均 Lab 尽量逼近用户给定的
有限目标 Lab。

硬约束（任一像素违反即整组偏移不可行）：

* 通道取值保持字节范围：``0 <= C+dc <= 255``（M/Y/K 同理）；
* 每通道偏移绝对值不超过用户给定上限 ``max_shift``（0..8）；
* 每像素总墨量 ``(C+dc)+(M+dm)+(Y+dy)+(K+dk)`` 不超过 ``tac_limit``（0..1020）。

优选顺序（按序取最小）：

1. 选区平均 Lab（LittleCMS TYPE_Lab_DBL 回算）对目标 Lab 的 **ΔE00**；
2. 偏移绝对值之和 ``|dc|+|dm|+|dy|+|dk|``；
3. ``(C, M, Y, K)`` 偏移四元组字典序。

只有最优解的 ΔE00 不超过给定非负容差时才视为**可达**，否则不产出成品 TIFF。
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from . import colorscience, lcms_native

MAX_RECT_SIDE = 64
MAX_SHIFT_LIMIT = 8
# CMYK 四通道字节求和的理论最大值 4*255 = 1020。
MAX_TAC = 4 * 255


class RetouchError(ValueError):
    """请求参数非法或原任务无法作为修色起点（HTTP 422）。"""


@dataclass(frozen=True)
class RetouchRequest:
    job_id: str
    x: int
    y: int
    width: int
    height: int
    target_lab: tuple[float, float, float]
    tolerance_de: float
    max_shift: int
    tac_limit: int


def validate_request(raw: dict) -> RetouchRequest:
    """严格校验修色请求；任何不合法输入抛 RetouchError（不猜测、不截断）。"""
    if not isinstance(raw, dict):
        raise RetouchError("请求体必须是 JSON 对象")
    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise RetouchError("缺少已保存任务 job_id")

    def as_int(key: str, lo: int, hi: int) -> int:
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, int):
            raise RetouchError(f"{key} 必须是 {lo}..{hi} 的整数")
        if not (lo <= v <= hi):
            raise RetouchError(f"{key}={v} 超出允许范围 {lo}..{hi}")
        return v

    x = as_int("x", 0, 10**9)
    y = as_int("y", 0, 10**9)
    width = as_int("width", 1, MAX_RECT_SIDE)
    height = as_int("height", 1, MAX_RECT_SIDE)

    target = raw.get("target_lab")
    if not isinstance(target, (list, tuple)) or len(target) != 3:
        raise RetouchError("target_lab 必须是 [L, a, b] 三个数值")
    try:
        L, a, b = (float(v) for v in target)
    except (TypeError, ValueError):
        raise RetouchError("target_lab 必须是 [L, a, b] 三个数值")
    if not np.isfinite([L, a, b]).all():
        raise RetouchError("target_lab 必须是有限数值")
    if not (0.0 <= L <= 100.0):
        raise RetouchError(f"目标 L*={L} 超出 0..100")
    if not (-128.0 <= a <= 127.0):
        raise RetouchError(f"目标 a*={a} 超出 -128..127")
    if not (-128.0 <= b <= 127.0):
        raise RetouchError(f"目标 b*={b} 超出 -128..127")

    tol = raw.get("tolerance_de")
    if isinstance(tol, bool) or not isinstance(tol, (int, float)):
        raise RetouchError("tolerance_de 必须是非负数值")
    tol = float(tol)
    if not np.isfinite(tol) or tol < 0.0:
        raise RetouchError("tolerance_de 必须是非负有限数值")

    max_shift = as_int("max_shift", 0, MAX_SHIFT_LIMIT)
    tac_limit = as_int("tac_limit", 0, MAX_TAC)

    return RetouchRequest(
        job_id=job_id, x=x, y=y, width=width, height=height,
        target_lab=(L, a, b), tolerance_de=tol,
        max_shift=max_shift, tac_limit=tac_limit,
    )


def load_job_context(job_dir: Path) -> dict:
    """读取原任务的分色起点与参数；缺文件即拒绝。"""
    try:
        meta = json.loads((job_dir / "meta.json").read_text("utf-8"))
    except FileNotFoundError:
        raise RetouchError("原任务不存在或已被删除")
    except (json.JSONDecodeError, OSError) as exc:
        raise RetouchError(f"原任务元数据不可读: {exc}")
    try:
        cmyk = np.load(job_dir / "cmyk.npy", mmap_mode="r")
    except FileNotFoundError:
        raise RetouchError("原任务分色结果 cmyk.npy 缺失，无法以其为修色起点")
    return {"meta": meta, "cmyk": np.asarray(cmyk, dtype=np.uint8)}


def verify_press_profile(press_bytes: bytes,
                         recorded_digest: str | None) -> None:
    """原任务印刷 ICC 必须可打开且 SHA-256 摘要与任务记录一致。

    ``recorded_digest`` 为任务创建时固化的全 64 位摘要（数据库 jobs 行 /
    meta.json）。摘要缺失或不符一律拒绝，绝不在被替换的配置上修色。
    """
    if not recorded_digest:
        raise RetouchError("原任务缺少印刷 ICC 摘要记录，拒绝修色")
    digest = hashlib.sha256(press_bytes).hexdigest()
    if digest != recorded_digest:
        raise RetouchError(
            "原任务印刷 ICC 摘要不符：任务记录为 "
            f"{recorded_digest}，当前文件为 {digest}；拒绝在被替换的配置上修色"
        )
    try:
        lcms_native.read_header(press_bytes)
    except Exception as exc:
        raise RetouchError(f"原任务印刷 ICC 已损坏，无法打开: {exc}")


def _feasible_offsets(roi: np.ndarray, max_shift: int, tac_limit: int) -> np.ndarray:
    """枚举出对选区**每个像素**都满足字节范围与总墨量约束的统一整数偏移。

    通道范围约束 ``min_ch+d >= 0`` 且 ``max_ch+d <= 255`` 只取决于该通道在
    选区内的最小/最大值；总墨量约束对任一像素为
    ``(C_p+M_p+Y_p+K_p) + (dc+dm+dy+dk) <= TAC``，其在选区内的最坏情况只
    取决于**原像素总墨量的最大值**——因此也是一个标量比较，无需逐像素展开。
    返回 (N, 4) int16 数组。
    """
    flat = roi.reshape(-1, 4).astype(np.int32)
    mins = flat.min(axis=0)
    maxs = flat.max(axis=0)
    max_pixel_ink = int(flat.sum(axis=1).max())

    ranges = []
    for ch in range(4):
        lo = max(-max_shift, -int(mins[ch]))
        hi = min(max_shift, 255 - int(maxs[ch]))
        if lo > hi:
            return np.empty((0, 4), dtype=np.int16)
        ranges.append(np.arange(lo, hi + 1, dtype=np.int16))

    shape = tuple(r.size for r in ranges)
    grid = np.empty(shape + (4,), dtype=np.int16)
    for ch, r in enumerate(ranges):
        grid[..., ch] = r.reshape(
            tuple(r.size if i == ch else 1 for i in range(4))
        )
    candidates = grid.reshape(-1, 4)
    keep = candidates.astype(np.int32).sum(axis=1) <= (tac_limit - max_pixel_ink)
    return candidates[keep]


def _mean_lab_for_offset(
    flat_cmyk: np.ndarray,
    offset: np.ndarray,
    lab_xf,
    lab_cache: dict[int, np.ndarray],
) -> np.ndarray:
    """偏移后选区 CMYK -> 像素加权平均 Lab（D50 double）。

    相同（偏移后）CMYK 四元组只让 LittleCMS 回算一次；相邻候选间大量重叠的
    油墨组合直接在 ``lab_cache`` 命中，使平坦/重复色区域的搜索非常便宜。
    查表/加权均为 NumPy 操作，无逐像素 Python 循环。
    """
    shifted = np.clip(
        flat_cmyk.astype(np.int32) + offset[None, :].astype(np.int32), 0, 255
    ).astype(np.uint8)

    keys = (
        shifted[:, 0].astype(np.uint64)
        | (shifted[:, 1].astype(np.uint64) << 8)
        | (shifted[:, 2].astype(np.uint64) << 16)
        | (shifted[:, 3].astype(np.uint64) << 24)
    )
    unique_keys, inverse, counts = np.unique(
        keys, return_inverse=True, return_counts=True
    )

    key_list = unique_keys.tolist()
    missing_idx = [i for i, k in enumerate(key_list) if k not in lab_cache]
    if missing_idx:
        miss = np.empty((len(missing_idx), 4), dtype=np.uint8)
        for row, i in enumerate(missing_idx):
            k = key_list[i]
            miss[row] = (k & 255, (k >> 8) & 255,
                         (k >> 16) & 255, (k >> 24) & 255)
        labs = lab_xf(miss)
        for i, lab in zip(missing_idx, labs):
            lab_cache[key_list[i]] = lab

    unique_lab = np.stack([lab_cache[k] for k in key_list])
    weights = (counts.astype(np.float64) / counts.sum())[:, None]
    return (unique_lab * weights).sum(axis=0)


def run_retouch(req: RetouchRequest, ctx: dict, press_bytes: bytes) -> dict:
    """执行搜索并返回可直接落盘/展示的修色结果字典。"""
    meta = ctx["meta"]
    cmyk_full = ctx["cmyk"]
    H, W = cmyk_full.shape[:2]
    if req.x >= W or req.y >= H:
        raise RetouchError("选区起点超出原任务图像范围")
    x2, y2 = min(W, req.x + req.width), min(H, req.y + req.height)
    rw, rh = x2 - req.x, y2 - req.y

    roi = cmyk_full[req.y:y2, req.x:x2]
    flat = np.ascontiguousarray(roi).reshape(-1, 4)

    intent = meta.get("source_intent", "relative_colorimetric")
    if intent not in lcms_native.INTENTS:
        raise RetouchError(f"原任务渲染意图无效: {intent}")

    candidates = _feasible_offsets(roi, req.max_shift, req.tac_limit)
    target = np.array(req.target_lab, dtype=np.float64)
    with lcms_native.lab_transform("CMYK", press_bytes, intent) as lab_xf:
        roi_lab_before = lab_xf(np.ascontiguousarray(flat))
        baseline_mean_lab = roi_lab_before.mean(axis=0)
        de_before_target = float(colorscience.ciede2000(
            target[None, None, :], baseline_mean_lab[None, None, :])[0, 0])

        lab_cache: dict[int, np.ndarray] = {}
        mean_labs = np.empty((candidates.shape[0], 3), dtype=np.float64)
        for gi, off in enumerate(candidates):
            mean_labs[gi] = _mean_lab_for_offset(flat, off, lab_xf, lab_cache)

        # 一次性向量化计算全部候选对目标的 ΔE00（colorscience 与主流水线相同）。
        n_cand = candidates.shape[0]
        targets = np.repeat(target[None, :], n_cand, axis=0)
        de_all = colorscience.ciede2000(
            targets.reshape(n_cand, 1, 3),
            mean_labs.reshape(n_cand, 1, 3),
        ).reshape(n_cand)

        abs_sums = np.abs(candidates.astype(np.int32)).sum(axis=1)
        # 1) ΔE00 升序 2) 偏移绝对值和升序 3) (C,M,Y,K) 字典序；
        # 用 Schwartzian 变换只构造一次排序键。
        decorated = [
            (float(de_all[i]), int(abs_sums[i]),
             tuple(int(v) for v in candidates[i]), i)
            for i in range(n_cand)
        ]
        decorated.sort(key=lambda t: (t[0], t[1], t[2]))

        feasible_count = n_cand
        if feasible_count == 0:
            best = None
        else:
            de_b, abs_b, off_b, bi = decorated[0]
            best = (de_b, abs_b, off_b, mean_labs[bi])

        if best is None:
            reachable = False
            best_de = None
            best_offset = None
            best_mean_lab = None
        else:
            best_de, _abs_sum, best_offset, best_mean_lab = best
            reachable = best_de <= req.tolerance_de + 1e-9

        offset = list(best_offset) if best_offset is not None else [0, 0, 0, 0]

        # 构造修色后的整幅 CMYK（选区外字节严格不变）。
        adjusted_full = cmyk_full.copy()
        roi_adj = np.clip(
            roi.astype(np.int32) + np.array(offset, dtype=np.int32), 0, 255
        ).astype(np.uint8)
        adjusted_full[req.y:y2, req.x:x2] = roi_adj

        if reachable:
            roi_lab_after = lab_xf(np.ascontiguousarray(roi_adj.reshape(-1, 4)))
            de_residual = colorscience.ciede2000(
                roi_lab_before.reshape(rh, rw, 3),
                roi_lab_after.reshape(rh, rw, 3),
            )
            residual_stats = {
                "mean_de00_before_after": round(float(de_residual.mean()), 4),
                "max_de00_before_after": round(float(de_residual.max()), 4),
                "mean_lab_before": [round(float(v), 4) for v in baseline_mean_lab],
                "mean_lab_after": [round(float(v), 4) for v in best_mean_lab],
                "target_lab": [round(float(v), 4) for v in target],
                "de00_to_target": round(float(best_de), 4),
                "de00_before_to_target": round(float(de_before_target), 4),
            }
        else:
            residual_stats = {
                "mean_de00_before_after": None,
                "max_de00_before_after": None,
                "mean_lab_before": [round(float(v), 4) for v in baseline_mean_lab],
                "mean_lab_after": None,
                "target_lab": [round(float(v), 4) for v in target],
                "de00_to_target": (round(float(best_de), 4)
                                   if best_de is not None else None),
                "de00_before_to_target": round(float(de_before_target), 4),
            }

    return {
        "rect": {"x": int(req.x), "y": int(req.y),
                 "width": int(rw), "height": int(rh)},
        "pixel_count": int(rw * rh),
        "constraints": {
            "target_lab": [float(v) for v in target],
            "tolerance_de00": req.tolerance_de,
            "max_shift_per_channel": req.max_shift,
            "total_ink_limit": req.tac_limit,
        },
        "feasible_offset_count": int(feasible_count),
        "reachable": bool(reachable),
        "best_offset_cmyk": [int(v) for v in offset],
        "best_abs_shift_sum": (int(sum(abs(v) for v in offset))
                               if best_offset is not None else None),
        "residual": residual_stats,
        "_cmyk_adjusted": adjusted_full,
        "_roi_before": roi,
        "_roi_after": roi_adj,
        "_intent": intent,
    }


# ---------------------------------------------------------------------------
# 软打样可视化（选区修色前/后）与成品 TIFF
# ---------------------------------------------------------------------------

def render_proof_crops(result: dict, cmyk_full: np.ndarray,
                       press_bytes: bytes, display_bytes: bytes,
                       proof_intent: str) -> dict[str, bytes]:
    """生成修色前/后选区软打样 PNG（嵌入显示 ICC）。"""
    rect = result["rect"]
    x, y, w, h = rect["x"], rect["y"], rect["width"], rect["height"]
    before = np.ascontiguousarray(cmyk_full[y:y + h, x:x + w].reshape(-1, 4))
    after = np.ascontiguousarray(result["_roi_after"].reshape(-1, 4))

    with lcms_native.lab_transform("CMYK", press_bytes,
                                   result["_intent"]) as lab_xf:
        lab_before = lab_xf(before)
        lab_after = lab_xf(after)

    def to_png(lab: np.ndarray) -> bytes:
        rgb = lcms_native.lab_to_pixels(lab, display_bytes, "RGB", proof_intent)
        img = Image.fromarray(rgb.reshape(h, w, 3), "RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", icc_profile=display_bytes)
        return buf.getvalue()

    return {"before.png": to_png(lab_before), "after.png": to_png(lab_after)}


def export_retouched_tiff(cmyk_adjusted: np.ndarray, press_bytes: bytes) -> bytes:
    """整幅修色后 CMYK 存为嵌入原印刷 ICC 的 TIFF（选区外字节不变）。"""
    img = Image.fromarray(np.ascontiguousarray(cmyk_adjusted), "CMYK")
    buf = io.BytesIO()
    # 无压缩：字节确定、逐通道可核验；PIL 对 CMYK 写 4 个 8 位样例/像素。
    img.save(buf, format="TIFF", compression="raw", icc_profile=press_bytes)
    return buf.getvalue()


def compute_retouch_id(meta: dict, req: RetouchRequest) -> str:
    h = hashlib.sha256()
    h.update(meta["job_id"].encode())
    h.update(meta.get("source_intent", "").encode())
    h.update(meta.get("proof_intent", "").encode())
    for key in ("x", "y", "width", "height"):
        h.update(str(getattr(req, key)).encode())
    h.update(",".join(f"{v:.6f}" for v in req.target_lab).encode())
    h.update(f"{req.tolerance_de:.6f}".encode())
    h.update(str(req.max_shift).encode())
    h.update(str(req.tac_limit).encode())
    return h.hexdigest()


def build_report(retouch_id: str, meta: dict, press_summary: dict,
                 display_summary: dict, req: RetouchRequest, result: dict) -> dict:
    """修色报告：记录原任务、ICC 摘要、全部约束与最终偏移。"""
    return {
        "retouch_id": retouch_id,
        "source_job": {
            "job_id": meta["job_id"],
            "image_sha256": meta.get("image_sha256"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "image_mode": meta.get("mode"),
            "source_intent": meta.get("source_intent"),
            "proof_intent": meta.get("proof_intent"),
            "threshold_delta_e": meta.get("threshold"),
        },
        "profiles": {
            "press": press_summary,
            "display": display_summary,
        },
        "region": result["rect"],
        "pixel_count": result["pixel_count"],
        "constraints": result["constraints"],
        "search": {
            "max_rect_side": MAX_RECT_SIDE,
            "feasible_offset_count": result["feasible_offset_count"],
            "selection_order": [
                "mean Lab ΔE00 (CIEDE2000, LittleCMS TYPE_Lab_DBL 回算)",
                "offset abs sum |dC|+|dM|+|dY|+|dK|",
                "(dC, dM, dY, dK) lexicographic",
            ],
        },
        "reachable": result["reachable"],
        "best_offset_cmyk": result["best_offset_cmyk"],
        "best_abs_shift_sum": result["best_abs_shift_sum"],
        "residual": result["residual"],
        "artifacts": (
            {"tiff": "retouched.tif", "before_png": "before.png",
             "after_png": "after.png"}
            if result["reachable"]
            else {"tiff": None, "before_png": "before.png", "after_png": "after.png"}
        ),
        "note": (
            "修色以原任务分色 CMYK 为起点，按原印刷 ICC 搜索统一整数偏移；"
            "仅当最优平均 Lab ΔE00 不超过容差时标记可达并导出嵌入印刷 ICC 的 "
            "CMYK TIFF（选区外字节不变）。不可达时不生成成品。"
        ),
    }
