"""Color science helpers: Lab encoding, CIEDE2000 and heatmap rendering."""
from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# LittleCMS 原始 LAB 模式字节解码 (ICC v2 8-bit encoding)
#
# 注意：这里读取的是 ImageCms.applyTransform 直接返回的图像字节（lcms 原生
# V2 编码），不要用 PIL 的 Image.new("LAB", ...) 手工构造——PIL 会按它自己
# 的极性约定重新解释字节。
#
# 重要：流水线的 ΔE00 不再经过这层 8 位 PCS 编码。源色与印后色由
# ``lcms_native.pixels_to_lab`` 以 TYPE_Lab_DBL（C double）直接取得，仅在
# 需要与历史 8 位路径对比/测试时才使用下面的编解码函数。
#
# L 通道: 0..255  -> 0..100
# a,b 通道: 字节 0..127 表示 +0..+127，128..255 表示 -128..-1
# （字节 0 和 255 都表示中性 0；这是 ICC v2 mft2 的遗留编码）。
# ---------------------------------------------------------------------------


def decode_lab(lab: np.ndarray) -> np.ndarray:
    """Convert HxWx3 uint8 LAB image to float HxWx3 (L in 0..100, a/b signed)."""
    lab = lab.astype(np.float64)
    L = lab[..., 0] * (100.0 / 255.0)
    ab = np.where(lab[..., 1:3] <= 127, lab[..., 1:3], lab[..., 1:3] - 256.0)
    return np.concatenate([L[..., None], ab], axis=-1)


def ciede2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """Vectorized CIEDE2000 between two HxWx3 Lab arrays; returns HxW ΔE00."""
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]

    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cbar = (C1 + C2) / 2.0
    Cbar7 = Cbar**7
    G = 0.5 * (1.0 - np.sqrt(Cbar7 / (Cbar7 + 25.0**7)))
    a1p = (1.0 + G) * a1
    a2p = (1.0 + G) * a2
    C1p = np.hypot(a1p, b1)
    C2p = np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360.0
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360.0

    dLp = L2 - L1
    dCp = C2p - C1p
    # Hue difference in the [-180, 180] range; zero when either chroma is 0.
    raw_dh = h2p - h1p
    dhp = np.where(
        (C1p * C2p) == 0.0,
        0.0,
        np.where(
            np.abs(raw_dh) <= 180.0,
            raw_dh,
            np.where(raw_dh > 180.0, raw_dh - 360.0, raw_dh + 360.0),
        ),
    )
    dHp = 2.0 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp / 2.0))

    Lbarp = (L1 + L2) / 2.0
    Cbarp = (C1p + C2p) / 2.0
    h_sum = h1p + h2p
    hbarp = np.where(
        (C1p * C2p) == 0.0,
        h_sum,
        np.where(
            np.abs(h1p - h2p) <= 180.0,
            h_sum / 2.0,
            np.where(h_sum < 360.0, (h_sum + 360.0) / 2.0, (h_sum - 360.0) / 2.0),
        ),
    )

    T = (
        1.0
        - 0.17 * np.cos(np.radians(hbarp - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * hbarp))
        + 0.32 * np.cos(np.radians(3.0 * hbarp + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * hbarp - 63.0))
    )
    dtheta = 30.0 * np.exp(-(((hbarp - 275.0) / 25.0) ** 2))
    Rc = 2.0 * np.sqrt(Cbarp**7 / (Cbarp**7 + 25.0**7))
    Sl = 1.0 + (0.015 * (Lbarp - 50.0) ** 2) / np.sqrt(20.0 + (Lbarp - 50.0) ** 2)
    Sc = 1.0 + 0.045 * Cbarp
    Sh = 1.0 + 0.015 * Cbarp * T
    Rt = -np.sin(np.radians(2.0 * dtheta)) * Rc

    kL = kC = kH = 1.0
    term = (dLp / (kL * Sl)) ** 2
    term += (dCp / (kC * Sc)) ** 2
    term += (dHp / (kH * Sh)) ** 2
    term += Rt * (dCp / (kC * Sc)) * (dHp / (kH * Sh))
    return np.sqrt(np.maximum(term, 0.0))


# ---------------------------------------------------------------------------
# Heatmap: black -> blue -> cyan -> green -> yellow -> red
# ---------------------------------------------------------------------------

_HEAT_STOPS = np.array(
    [
        [0, 0, 0],
        [0, 0, 180],
        [0, 200, 255],
        [0, 200, 60],
        [255, 255, 0],
        [255, 0, 0],
    ],
    dtype=np.float64,
)


def heatmap_rgb(de: np.ndarray, vmax: float | None = None) -> np.ndarray:
    """Map ΔE values to an HxWx3 uint8 heatmap."""
    if vmax is None:
        vmax = float(de.max()) if de.size else 1.0
    vmax = max(vmax, 1e-6)
    t = np.clip(de / vmax, 0.0, 1.0)
    pos = t * (len(_HEAT_STOPS) - 1)
    lo = np.floor(pos).astype(int)
    hi = np.minimum(lo + 1, len(_HEAT_STOPS) - 1)
    frac = (pos - lo)[..., None]
    rgb = _HEAT_STOPS[lo] * (1.0 - frac) + _HEAT_STOPS[hi] * frac
    return np.rint(rgb).astype(np.uint8)


def overlay_heatmap(base_rgb: np.ndarray, heat_rgb: np.ndarray, alpha: float = 0.55) -> np.ndarray:
    """Alpha-blend a heatmap over an RGB image."""
    return np.clip(
        base_rgb.astype(np.float64) * (1.0 - alpha) + heat_rgb.astype(np.float64) * alpha,
        0,
        255,
    ).astype(np.uint8)


def threshold_mask(de: np.ndarray, threshold: float) -> np.ndarray:
    """Binary HxW mask: white (255) where ΔE >= threshold, else black."""
    return (de >= threshold).astype(np.uint8) * 255
