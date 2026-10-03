"""The soft-proofing color pipeline.

Stages (every pixel, all transforms executed by LittleCMS):

    source pixels --(source profile, source intent)--> Lab (double) [Lab_src]
    source pixels --(source profile, source intent)--> press CMYK    [device]
    press CMYK    --(press profile)--> Lab (double)                  [Lab_press]
    Lab_press     --(display profile, proof intent)--> display RGB    [proof]
    source pixels --(display profile, source intent)--> display RGB   [preview]

The two Lab stages ask lcms directly for ``TYPE_Lab_DBL`` (native C double)
results via :mod:`app.lcms_native`, bypassing Pillow's fixed 8-bit ``"LAB"``
mode. Pixel-level ΔE00, the heatmap/mask, statistics, region inspection and
the exported report are therefore all computed from one unquantized Lab
result. The browser is never asked to interpret untagged data: every RGB
image we emit is saved with the chosen display profile embedded... (for the
screen-facing previews we tag sRGB so the file is unambiguous even when a
display profile differs).
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass

import numpy as np
from PIL import Image

from . import colorscience, lcms_native
from .profiles import ProfileManager

INTENT_LABELS = {
    "perceptual": "感知",
    "relative_colorimetric": "相对色度",
    "saturation": "饱和度",
    "absolute_colorimetric": "绝对色度",
}

ALLOWED_SUFFIXES = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}


class PipelineError(ValueError):
    pass


def summary_from_bytes(data: bytes, profile_id: str = "<snapshot>") -> dict:
    """Build the same profile summary dict as ProfileManager.summary from raw bytes.

    Used by the regression gate, whose reruns bind to immutable snapshot bytes
    rather than to files served from the live profile directories.
    """
    header = lcms_native.read_header(data)
    try:
        from PIL import ImageCms
        import io as _io
        name = ImageCms.getProfileName(
            ImageCms.ImageCmsProfile(_io.BytesIO(data))
        ).strip() or profile_id
    except Exception:
        name = profile_id
    return {
        "profile_id": profile_id,
        "name": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        **header,
    }


@dataclass
class PipelineInputs:
    image_data: bytes
    source_profile_id: str
    press_profile_id: str
    display_profile_id: str
    source_intent: str
    proof_intent: str
    threshold: float
    use_embedded: bool = False
    max_dimension: int = 3000
    # 回归门禁重跑时按快照固定的 ICC 字节；提供后忽略按 ID 从可变目录取配置，
    # 保证进行中/历史的重跑绑定的是固定快照而非当前文件。
    source_profile_bytes: bytes | None = None
    press_profile_bytes: bytes | None = None
    display_profile_bytes: bytes | None = None


def load_pixels(data: bytes, max_dimension: int) -> tuple[Image.Image, str, bytes | None]:
    """Validate the upload and return a normalized RGB/CMYK PIL image.

    Returns (image, declared_mode, embedded_icc_bytes).
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise PipelineError(f"无法打开图像: {exc}")

    fmt = (img.format or "").upper()
    if fmt not in ("TIFF", "PNG", "JPEG"):
        raise PipelineError(f"不支持的图像格式: {fmt or '未知'}（仅支持 TIFF/PNG/JPEG）")

    embedded = None
    try:
        icc_block = img.info.get("icc_profile")
        if icc_block:
            embedded = icc_block
    except Exception:
        embedded = None

    mode = img.mode
    # Only the color spaces the spec calls for are accepted; indexed ("P"),
    # 16-bit, LA 等一律拒绝，而不是被隐式重新解释。
    if mode not in ("RGB", "RGBA", "CMYK", "L"):
        raise PipelineError(
            f"不支持的图像位深/模式: {mode}（仅支持 8 位 RGB/RGBA/CMYK/灰度；"
            "请先在印前软件中转换并嵌入 ICC 配置）"
        )

    if mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        img = Image.composite(img, bg.convert("RGBA"), img.split()[-1]).convert("RGB")
        mode = "RGB"

    if max(img.size) > max_dimension:
        raise PipelineError(
            f"图像尺寸 {img.size[0]}x{img.size[1]} 超过上限 {max_dimension}px"
        )
    return img, mode, embedded


class PipelineResult:
    def __init__(self, inputs: PipelineInputs, image_sha: str,
                 img: Image.Image, mode: str,
                 lab_src: np.ndarray, lab_press: np.ndarray,
                 cmyk: np.ndarray, de: np.ndarray,
                 preview: Image.Image, proof: Image.Image,
                 heatmap: np.ndarray, mask: np.ndarray,
                 stats: dict, profile_summaries: dict,
                 embedded_sha: str | None):
        self.inputs = inputs
        self.image_sha = image_sha
        self.img = img
        self.mode = mode
        self.lab_src = lab_src
        self.lab_press = lab_press
        self.cmyk = cmyk
        self.de = de
        self.preview = preview
        self.proof = proof
        self.heatmap = heatmap
        self.mask = mask
        self.stats = stats
        self.profile_summaries = profile_summaries
        self.embedded_sha = embedded_sha


def run_pipeline(inputs: PipelineInputs, pm: ProfileManager) -> PipelineResult:
    if inputs.threshold < 0:
        raise PipelineError("色差阈值不能为负")
    if inputs.source_intent not in INTENT_LABELS or inputs.proof_intent not in INTENT_LABELS:
        raise PipelineError("渲染意图无效")

    image_sha = hashlib.sha256(inputs.image_data).hexdigest()
    img, mode, embedded_icc = load_pixels(inputs.image_data, inputs.max_dimension)

    # ---- resolve source profile (embedded upload wins when requested) ----
    embedded_sha = None
    if inputs.use_embedded:
        if not embedded_icc:
            raise PipelineError("图像中没有内嵌的 ICC 配置，无法按“使用内嵌源配置”处理")
        try:
            embedded_header = lcms_native.read_header(embedded_icc)
        except Exception as exc:
            raise PipelineError(f"内嵌 ICC 配置无效: {exc}") from exc
        src_bytes = embedded_icc
        src_cs = embedded_header["color_space"]
        embedded_sha = hashlib.sha256(embedded_icc).hexdigest()
        if src_cs not in ("RGB", "CMYK", "GRAY"):
            raise PipelineError(f"内嵌配置的色彩空间不支持: {src_cs}")
    elif inputs.source_profile_bytes is not None:
        src_bytes = inputs.source_profile_bytes
        try:
            src_header = lcms_native.read_header(src_bytes)
        except Exception as exc:
            raise PipelineError(f"源 ICC 配置无效: {exc}") from exc
        pm.validate_for_role(src_header, "source")
        src_cs = src_header["color_space"]
    else:
        pm.get(inputs.source_profile_id, "source")
        src_bytes, src_header = pm.get_bytes_header(inputs.source_profile_id)
        src_cs = src_header["color_space"]

    if inputs.press_profile_bytes is not None:
        press_bytes = inputs.press_profile_bytes
        try:
            press_header = lcms_native.read_header(press_bytes)
        except Exception as exc:
            raise PipelineError(f"印刷 ICC 配置无效: {exc}") from exc
        pm.validate_for_role(press_header, "press")
    else:
        pm.get(inputs.press_profile_id, "press")
        press_bytes = pm.load_bytes(inputs.press_profile_id)

    if inputs.display_profile_bytes is not None:
        display_bytes = inputs.display_profile_bytes
        try:
            display_header = lcms_native.read_header(display_bytes)
        except Exception as exc:
            raise PipelineError(f"显示 ICC 配置无效: {exc}") from exc
        pm.validate_for_role(display_header, "display")
    else:
        pm.get(inputs.display_profile_id, "display")
        display_bytes = pm.load_bytes(inputs.display_profile_id)

    # ---- strict channel/color-space matching ----------------------------
    pixel_mode = mode
    pixel_cs = "GRAY" if pixel_mode == "L" else pixel_mode
    if pixel_cs != src_cs:
        raise PipelineError(
            f"图像色彩空间/通道数与源配置不匹配: 图像为 {pixel_cs}"
            f"（{'1' if pixel_cs == 'GRAY' else ('4' if pixel_cs == 'CMYK' else '3')} 通道），"
            f"源配置为 {src_cs}，拒绝按浏览器默认色彩解释"
        )

    src_pixels = np.asarray(img)
    if pixel_cs == "GRAY":
        src_pixels = src_pixels.reshape(*src_pixels.shape, 1)
    h, w = src_pixels.shape[:2]
    flat_pixels = np.ascontiguousarray(src_pixels, dtype=np.uint8).reshape(h * w, -1)

    try:
        # ---- transforms ---------------------------------------------------
        # All transforms are executed by LittleCMS directly. The two Lab
        # stages request native TYPE_Lab_DBL output, so source and post-press
        # Lab values reach CIEDE2000 without an extra ICC v2 8-bit PCS
        # quantization that could flip borderline pixels near the threshold.

        # Source -> Lab (back-calculation of color values to Lab via source ICC)
        lab_src = lcms_native.pixels_to_lab(
            flat_pixels, pixel_cs, src_bytes, inputs.source_intent
        ).reshape(h, w, 3)

        # Source -> press device CMYK
        cmyk_flat = lcms_native.pixels_to_press_cmyk(
            flat_pixels, pixel_cs, src_bytes, press_bytes, inputs.source_intent
        )
        cmyk = cmyk_flat.reshape(h, w, 4)

        # Press CMYK -> Lab (沿源→印刷→Lab 回算色值)
        lab_press = lcms_native.pixels_to_lab(
            cmyk_flat, "CMYK", press_bytes, inputs.source_intent
        ).reshape(h, w, 3)

        # Lab_press -> display RGB = the soft proof
        # (仍按所选显示 ICC 与显示渲染意图呈现，不经过 8 位 Lab PCS)
        proof_rgb = lcms_native.lab_to_pixels(
            lab_press.reshape(h * w, 3), display_bytes, "RGB", inputs.proof_intent
        ).reshape(h, w, 3)
        # Source -> display direct preview
        preview_rgb = lcms_native.pixels_to_rgb(
            flat_pixels, pixel_cs, src_bytes, display_bytes, inputs.source_intent
        ).reshape(h, w, 3)
    except RuntimeError as exc:
        raise PipelineError(f"色彩转换执行失败: {exc}") from exc

    proof_img = Image.fromarray(np.ascontiguousarray(proof_rgb), "RGB")
    preview_img = Image.fromarray(np.ascontiguousarray(preview_rgb), "RGB")

    # ---- ΔE, heatmap, mask（热图/遮罩/统计共用同一高精度 Lab 结果） ------
    de = colorscience.ciede2000(lab_src, lab_press)
    threshold = float(inputs.threshold)
    heat = colorscience.heatmap_rgb(de, vmax=max(float(np.percentile(de, 99)), threshold * 2, 1.0))
    mask = colorscience.threshold_mask(de, threshold)

    over = float((de >= threshold).sum()) / de.size
    stats = {
        "mean": float(de.mean()),
        "p95": float(np.percentile(de, 95)),
        "max": float(de.max()),
        "over_ratio": over,
        "threshold": threshold,
        "over_pixels": int((de >= threshold).sum()),
        "total_pixels": int(de.size),
    }

    # ---- profile summaries for the deterministic report ------------------
    summaries = {}
    if inputs.use_embedded:
        summaries["source"] = {
            "profile_id": "<embedded>",
            "embedded": True,
            "sha256": embedded_sha,
            **embedded_header,
        }
    elif inputs.source_profile_bytes is not None:
        summaries["source"] = summary_from_bytes(src_bytes, inputs.source_profile_id)
    else:
        summaries["source"] = pm.summary(inputs.source_profile_id)
    summaries["press"] = (
        summary_from_bytes(press_bytes, inputs.press_profile_id)
        if inputs.press_profile_bytes is not None
        else pm.summary(inputs.press_profile_id)
    )
    summaries["display"] = (
        summary_from_bytes(display_bytes, inputs.display_profile_id)
        if inputs.display_profile_bytes is not None
        else pm.summary(inputs.display_profile_id)
    )

    return PipelineResult(
        inputs=inputs, image_sha=image_sha, img=img, mode=mode,
        lab_src=lab_src, lab_press=lab_press, cmyk=cmyk, de=de,
        preview=preview_img, proof=proof_img, heatmap=heat,
        mask=np.stack([mask] * 3, axis=-1),
        stats=stats, profile_summaries=summaries, embedded_sha=embedded_sha,
    )


# ---------------------------------------------------------------------------
# Export artifacts
# ---------------------------------------------------------------------------

def _png(img: Image.Image, icc_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", icc_profile=icc_bytes)
    return buf.getvalue()


def export_pngs(result: PipelineResult, srgb_icc_bytes: bytes, display_icc_bytes: bytes) -> dict[str, bytes]:
    """Emit the five PNG artifacts.

    preview/softproof live in the chosen display profile color space and are
    tagged with that profile; the heatmap, mask and their overlay are synthetic
    visualizations without physical colorimetry and are tagged sRGB so browsers
    reproduce their colors deterministically.
    """
    overlay = colorscience.overlay_heatmap(
        np.asarray(result.proof), result.heatmap, alpha=0.55
    )
    return {
        "preview.png": _png(result.preview, display_icc_bytes),
        "softproof.png": _png(result.proof, display_icc_bytes),
        "heatmap.png": _png(Image.fromarray(result.heatmap, "RGB"), srgb_icc_bytes),
        "overlay.png": _png(Image.fromarray(overlay, "RGB"), srgb_icc_bytes),
        "mask.png": _png(Image.fromarray(result.mask, "RGB"), srgb_icc_bytes),
    }


def compute_job_id(result: PipelineResult) -> str:
    h = hashlib.sha256()
    h.update(result.image_sha.encode())
    parts = [
        result.profile_summaries["source"]["sha256"],
        result.profile_summaries["press"]["sha256"],
        result.profile_summaries["display"]["sha256"],
        result.inputs.source_intent,
        result.inputs.proof_intent,
        f"{result.inputs.threshold:.6f}",
    ]
    for p in parts:
        h.update(p.encode())
    return h.hexdigest()


def build_report(result: PipelineResult, job_id: str) -> dict:
    w, h = result.img.size
    return {
        "job_id": job_id,
        "image": {
            "sha256": result.image_sha,
            "width": w,
            "height": h,
            "mode": result.mode,
            "embedded_source_sha256": result.embedded_sha,
        },
        "profiles": result.profile_summaries,
        "parameters": {
            "source_intent": result.inputs.source_intent,
            "source_intent_label": INTENT_LABELS[result.inputs.source_intent],
            "proof_intent": result.inputs.proof_intent,
            "proof_intent_label": INTENT_LABELS[result.inputs.proof_intent],
            "threshold_delta_e": result.inputs.threshold,
            "use_embedded_source": result.inputs.use_embedded,
        },
        "stats": result.stats,
        "lab_note": (
            "Lab 值由 LittleCMS 直接以 TYPE_Lab_DBL（native C double, D50 CIELAB）输出，"
            "未经 ICC v2 8 位 PCS 量化；ΔE00、热图、超阈遮罩、统计、框选区域 Lab/色差"
            "与本报告均取自同一高精度 Lab 结果。软打样与源图预览仍按所选显示 ICC 渲染。"
        ),
    }


def export_zip(pngs: dict[str, bytes], report: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(pngs.items()):
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zi, data)
        report_bytes = json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
        zi = zipfile.ZipInfo("report.json", date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi, report_bytes)
    return buf.getvalue()
