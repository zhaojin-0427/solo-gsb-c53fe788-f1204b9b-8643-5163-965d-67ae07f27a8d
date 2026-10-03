"""Region-of-interest inspection: per-stage Lab/CMYK statistics."""
from __future__ import annotations

import numpy as np

from .pipeline import PipelineResult


def _sample_grid(arr: np.ndarray, points: int = 5) -> list:
    """Return a `points` x `points` sampling of a region (channels last)."""
    h, w = arr.shape[:2]
    ys = np.linspace(0, h - 1, points).round().astype(int)
    xs = np.linspace(0, w - 1, points).round().astype(int)
    out = []
    for y in ys:
        row = []
        for x in xs:
            v = arr[y, x]
            row.append([round(float(c), 3) for c in np.atleast_1d(v)])
        out.append(row)
    return out


def inspect_region(result: PipelineResult, x: int, y: int, w: int, h: int) -> dict:
    H, W = result.img.size[1], result.img.size[0]
    x = max(0, int(x)); y = max(0, int(y))
    w = max(1, int(w)); h = max(1, int(h))
    if x >= W or y >= H or w < 1 or h < 1:
        raise ValueError("区域坐标超出图像范围")
    x2 = min(W, x + w); y2 = min(H, y + h)

    lab_src = result.lab_src[y:y2, x:x2]
    lab_press = result.lab_press[y:y2, x:x2]
    cmyk = result.cmyk[y:y2, x:x2].astype(np.float64)
    de = result.de[y:y2, x:x2]

    def stats_lab(a):
        return {
            "mean": [round(float(v), 3) for v in a.reshape(-1, 3).mean(axis=0)],
            "min": [round(float(v), 3) for v in a.reshape(-1, 3).min(axis=0)],
            "max": [round(float(v), 3) for v in a.reshape(-1, 3).max(axis=0)],
        }

    over_ratio = float((de >= result.inputs.threshold).sum()) / de.size
    return {
        "rect": {"x": x, "y": y, "width": x2 - x, "height": y2 - y},
        "pixel_count": int(de.size),
        "lab_source": stats_lab(lab_src),
        "lab_after_press": stats_lab(lab_press),
        "cmyk": {
            # LittleCMS CMYK 8-bit: 0 = 无墨, 255 = 满版
            "mean_percent": [round(float(v) / 255 * 100, 2) for v in cmyk.reshape(-1, 4).mean(axis=0)],
            "max_percent": [round(float(v) / 255 * 100, 2) for v in cmyk.reshape(-1, 4).max(axis=0)],
            "channel_order": "C,M,Y,K; 0% = 无墨, 100% = 满版",
        },
        "delta_e00": {
            "mean": round(float(de.mean()), 4),
            "p95": round(float(np.percentile(de, 95)), 4),
            "max": round(float(de.max()), 4),
            "over_threshold_ratio": round(over_ratio, 6),
        },
        "samples_5x5": {
            "lab_source": _sample_grid(lab_src),
            "lab_after_press": _sample_grid(lab_press),
            "cmyk_percent": _sample_grid(cmyk / 255.0 * 100.0),
            "delta_e00": _sample_grid(de[..., None]),
        },
    }
