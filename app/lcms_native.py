"""Minimal ctypes bindings for the subset of LittleCMS 2 we need.

Pillow's ImageCms is used for the pixel transforms themselves, but we go to
liblcms2 directly for a few things Pillow cannot do:

* create a real Lab identity profile (``cmsCreateLab2Profile``) so the
  pipeline can land in CIELAB PCS with V2 8-bit encoding;
* synthesize a self-contained CMYK demo profile at startup, so the app can
  demonstrate the full source -> press -> display chain without requiring
  the user to ship a press profile;
* read ICC header fields (device class / color space / PCS) for strict
  validation;
* run pixel transforms with full control over PCS precision: the color
  pipeline asks for LittleCMS native ``TYPE_Lab_DBL`` output so source and
  post-press Lab values feed CIEDE2000 directly as C doubles, without the
  extra ICC v2 8-bit PCS quantization that Pillow's ``"LAB"`` mode implies.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import struct
from functools import lru_cache

import numpy as np

# ICC 4-byte signatures (big-endian uint32 on the wire/API).
def sig(value: bytes) -> int:
    return struct.unpack(">I", value)[0]


# ---------------------------------------------------------------------------
# LittleCMS pixel-type constants (lcms2.h: COLORSPACE_SH/CHANNELS_SH/BYTES_SH/
# FLOAT_SH) and render intents. We construct transform input/output formats
# ourselves instead of relying on Pillow's fixed 8-bit modes, so the Lab PCS
# can be retrieved as native C doubles (TYPE_Lab_DBL, L* in 0..100, a*/b*
# signed).
# ---------------------------------------------------------------------------
_PT_GRAY = 3
_PT_RGB = 4
_PT_CMYK = 6
_PT_LAB = 10
_TYPE_GRAY_8 = (_PT_GRAY << 16) | (1 << 3) | 1
_TYPE_RGB_8 = (_PT_RGB << 16) | (3 << 3) | 1
_TYPE_CMYK_8 = (_PT_CMYK << 16) | (4 << 3) | 1
_TYPE_LAB_DBL = (1 << 22) | (_PT_LAB << 16) | (3 << 3) | 0

INTENT_PERCEPTUAL = 0
INTENT_RELATIVE_COLORIMETRIC = 1
INTENT_SATURATION = 2
INTENT_ABSOLUTE_COLORIMETRIC = 3
INTENTS = {
    "perceptual": INTENT_PERCEPTUAL,
    "relative_colorimetric": INTENT_RELATIVE_COLORIMETRIC,
    "saturation": INTENT_SATURATION,
    "absolute_colorimetric": INTENT_ABSOLUTE_COLORIMETRIC,
}

_UINT8_INPUT_TYPES = {
    "RGB": _TYPE_RGB_8,
    "CMYK": _TYPE_CMYK_8,
    "GRAY": _TYPE_GRAY_8,
}


class _CIEXYZ(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_double),
        ("Y", ctypes.c_double),
        ("Z", ctypes.c_double),
    ]


@lru_cache(maxsize=1)
def lcms() -> ctypes.CDLL:
    lib = ctypes.util.find_library("lcms2")
    if not lib:
        raise RuntimeError("找不到 LittleCMS 2 共享库 (liblcms2)")
    L = ctypes.CDLL(lib)

    L.cmsCreate_sRGBProfile.restype = ctypes.c_void_p
    L.cmsCreate_sRGBProfile.argtypes = []

    L.cmsCreateLab2Profile.restype = ctypes.c_void_p
    L.cmsCreateLab2Profile.argtypes = [ctypes.c_void_p]

    L.cmsCreateProfilePlaceholder.restype = ctypes.c_void_p
    L.cmsCreateProfilePlaceholder.argtypes = [ctypes.c_void_p]

    for name in ("cmsSetPCS", "cmsSetColorSpace", "cmsSetDeviceClass"):
        getattr(L, name).restype = None
        getattr(L, name).argtypes = [ctypes.c_void_p, ctypes.c_uint32]

    L.cmsSetProfileVersion.restype = None
    L.cmsSetProfileVersion.argtypes = [ctypes.c_void_p, ctypes.c_double]

    L.cmsMLUalloc.restype = ctypes.c_void_p
    L.cmsMLUalloc.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

    L.cmsMLUsetASCII.restype = ctypes.c_int
    L.cmsMLUsetASCII.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
    ]

    L.cmsWriteTag.restype = ctypes.c_int
    L.cmsWriteTag.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]

    L.cmsCloseProfile.restype = ctypes.c_int
    L.cmsCloseProfile.argtypes = [ctypes.c_void_p]

    L.cmsSaveProfileToMem.restype = ctypes.c_int
    L.cmsSaveProfileToMem.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]

    L.cmsSaveProfileToFile.restype = ctypes.c_int
    L.cmsSaveProfileToFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

    L.cmsOpenProfileFromMem.restype = ctypes.c_void_p
    L.cmsOpenProfileFromMem.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

    L.cmsGetColorSpace.restype = ctypes.c_uint32
    L.cmsGetColorSpace.argtypes = [ctypes.c_void_p]

    L.cmsGetPCS.restype = ctypes.c_uint32
    L.cmsGetPCS.argtypes = [ctypes.c_void_p]

    L.cmsGetDeviceClass.restype = ctypes.c_uint32
    L.cmsGetDeviceClass.argtypes = [ctypes.c_void_p]

    L.cmsPipelineAlloc.restype = ctypes.c_void_p
    L.cmsPipelineAlloc.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    L.cmsPipelineFree.restype = None
    L.cmsPipelineFree.argtypes = [ctypes.c_void_p]

    L.cmsStageAllocCLut16bit.restype = ctypes.c_void_p
    L.cmsStageAllocCLut16bit.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint16),
    ]

    L.cmsPipelineInsertStage.restype = ctypes.c_int
    L.cmsPipelineInsertStage.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]

    L.cmsD50_XYZ.restype = ctypes.POINTER(_CIEXYZ)
    L.cmsD50_XYZ.argtypes = []

    # Direct transform API for high-precision (TYPE_Lab_DBL) PCS access.
    L.cmsCreateTransform.restype = ctypes.c_void_p
    L.cmsCreateTransform.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32,
    ]
    L.cmsDeleteTransform.restype = None
    L.cmsDeleteTransform.argtypes = [ctypes.c_void_p]
    L.cmsDoTransform.restype = None
    L.cmsDoTransform.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]

    return L


def _profile_to_bytes(handle: ctypes.c_void_p) -> bytes:
    L = lcms()
    size = ctypes.c_uint32(0)
    L.cmsSaveProfileToMem(handle, None, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    if not L.cmsSaveProfileToMem(handle, buf, ctypes.byref(size)):
        raise RuntimeError("保存 ICC profile 到内存失败")
    return buf.raw[: size.value]


def build_lab_profile_bytes() -> bytes:
    """CIELAB identity profile (ICC v2, PCS Lab) used to materialize Lab."""
    L = lcms()
    handle = L.cmsCreateLab2Profile(None)
    if not handle:
        raise RuntimeError("cmsCreateLab2Profile 失败")
    try:
        return _profile_to_bytes(handle)
    finally:
        L.cmsCloseProfile(handle)


def build_srgb_profile_bytes() -> bytes:
    L = lcms()
    handle = L.cmsCreate_sRGBProfile()
    try:
        return _profile_to_bytes(handle)
    finally:
        L.cmsCloseProfile(handle)


def read_header(data: bytes) -> dict[str, str | int]:
    """Parse the ICC profile header without trusting lcms to load it."""
    if len(data) < 128 or data[36:40] != b"acsp":
        raise ValueError("不是有效的 ICC profile（缺少 acsp 签名）")
    declared = struct.unpack(">I", data[0:4])[0]
    if declared != len(data):
        # lcms tolerates trailing bytes when loading, but a malformed size
        # field is a strong signal of a corrupt file; reject it.
        if declared > len(data):
            raise ValueError("ICC profile 长度字段与文件不一致")
    return {
        "size": declared,
        "version": data[8:12].hex(),
        "device_class": data[12:16].decode("latin1").strip(),
        "color_space": data[16:20].decode("ascii").strip(),
        "pcs": data[20:24].decode("ascii").strip(),
    }


# ---------------------------------------------------------------------------
# High-precision pixel transforms (Lab PCS as C doubles)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _open_profiles(*profile_blobs: bytes):
    """Open each ICC blob in memory and close it on exit (buffer kept alive)."""
    L = lcms()
    handles = []
    keepalive = []
    try:
        for index, blob in enumerate(profile_blobs):
            buf = ctypes.create_string_buffer(blob, len(blob) + 1)
            keepalive.append(buf)
            h = L.cmsOpenProfileFromMem(buf, ctypes.c_uint32(len(blob)))
            if not h:
                raise RuntimeError(f"lcms 无法打开第 {index + 1} 个 ICC 配置")
            handles.append(h)
        yield handles
    finally:
        for h in handles:
            L.cmsCloseProfile(h)


def _pixel_type(color_space: str) -> int:
    try:
        return _UINT8_INPUT_TYPES[color_space]
    except KeyError:
        raise RuntimeError(f"不支持的像素色彩空间: {color_space}") from None


def _transform_pixels(
    src_pixels: np.ndarray,
    src_cs: str,
    src_profile: bytes,
    dst_profile: bytes,
    dst_type: int,
    intent: str,
) -> np.ndarray:
    """Run an lcms transform over a flat (N, C_in) uint8 array.

    Returns a flat (N, C_out) array: uint8 for 8-bit destination types,
    float64 for TYPE_Lab_DBL. The caller keeps control of PCS precision;
    nothing passes through Pillow's fixed 8-bit LAB mode.
    """
    if intent not in INTENTS:
        raise RuntimeError(f"未知渲染意图: {intent}")
    pixels = np.ascontiguousarray(src_pixels, dtype=np.uint8).reshape(-1)
    n_pixels = pixels.size // _channel_count(src_cs)
    L = lcms()
    with _open_profiles(src_profile, dst_profile) as (h_src, h_dst):
        xform = L.cmsCreateTransform(
            h_src, _pixel_type(src_cs),
            h_dst, ctypes.c_uint32(dst_type),
            ctypes.c_uint32(INTENTS[intent]), ctypes.c_uint32(0),
        )
        if not xform:
            raise RuntimeError("lcms 无法建立色彩转换")
        try:
            channels_out = _type_channels(dst_type)
            if dst_type == _TYPE_LAB_DBL:
                out = np.zeros(n_pixels * channels_out, dtype=np.float64)
            else:
                out = np.zeros(n_pixels * channels_out, dtype=np.uint8)
            in_ptr = pixels.ctypes.data_as(ctypes.c_void_p)
            out_ptr = out.ctypes.data_as(ctypes.c_void_p)
            L.cmsDoTransform(xform, in_ptr, out_ptr, ctypes.c_uint32(n_pixels))
            return out.reshape(n_pixels, channels_out)
        finally:
            L.cmsDeleteTransform(xform)


def _channel_count(color_space: str) -> int:
    return {"GRAY": 1, "RGB": 3, "CMYK": 4}[color_space]


def _type_channels(pixel_type: int) -> int:
    # CHANNELS_SH occupies bits 3..10 of the type signature.
    return (int(pixel_type) >> 3) & 0x7F


def pixels_to_lab(pixels: np.ndarray, src_cs: str, src_profile: bytes,
                  intent: str) -> np.ndarray:
    """Device pixels (flat N x C uint8) -> N x 3 float64 CIELAB (D50)."""
    return _transform_pixels(
        pixels, src_cs, src_profile, _lab_profile_bytes(),
        _TYPE_LAB_DBL, intent,
    )


@contextlib.contextmanager
def lab_transform(src_cs: str, src_profile: bytes, intent: str):
    """Open a reusable device->Lab_DBL transform.

    局部 CMYK 修色要对上万组候选偏移各回算一次选区 Lab；反复 create/delete
    transform 的固定开销在小批量调用时占主导，因此调用方可在整个搜索期间
    持有同一 transform（lcms DoTransform 对同一对象可重入、无内部游标）。

    Usage::

        with lab_transform("CMYK", press_bytes, intent) as xf:
            labs = xf(cmyk_uint8_flat)
    """
    if intent not in INTENTS:
        raise RuntimeError(f"未知渲染意图: {intent}")
    L = lcms()
    with _open_profiles(src_profile, _lab_profile_bytes()) as (h_src, h_lab):
        xform = L.cmsCreateTransform(
            h_src, _pixel_type(src_cs),
            h_lab, ctypes.c_uint32(_TYPE_LAB_DBL),
            ctypes.c_uint32(INTENTS[intent]), ctypes.c_uint32(0),
        )
        if not xform:
            raise RuntimeError("lcms 无法建立色彩转换")

        def run(pixels: np.ndarray) -> np.ndarray:
            flat = np.ascontiguousarray(pixels, dtype=np.uint8).reshape(-1)
            n_pixels = flat.size // _channel_count(src_cs)
            out = np.zeros(n_pixels * 3, dtype=np.float64)
            L.cmsDoTransform(
                xform,
                flat.ctypes.data_as(ctypes.c_void_p),
                out.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_uint32(n_pixels),
            )
            return out.reshape(n_pixels, 3)

        try:
            yield run
        finally:
            L.cmsDeleteTransform(xform)


def pixels_to_rgb(pixels: np.ndarray, src_cs: str, src_profile: bytes,
                  display_profile: bytes, intent: str) -> np.ndarray:
    """Device pixels -> N x 3 uint8 RGB under `display_profile`."""
    return _transform_pixels(
        pixels, src_cs, src_profile, display_profile, _TYPE_RGB_8, intent,
    )


def pixels_to_press_cmyk(pixels: np.ndarray, src_cs: str, src_profile: bytes,
                         press_profile: bytes, intent: str) -> np.ndarray:
    """Device pixels -> N x 4 uint8 press CMYK (0 = no ink, 255 = full)."""
    return _transform_pixels(
        pixels, src_cs, src_profile, press_profile, _TYPE_CMYK_8, intent,
    )


def lab_to_pixels(lab: np.ndarray, dst_profile: bytes, dst_cs: str,
                  intent: str) -> np.ndarray:
    """N x 3 float64 CIELAB -> N x C uint8 device pixels in `dst_profile`."""
    lab_flat = np.ascontiguousarray(lab, dtype=np.float64).reshape(-1)
    n_pixels = lab_flat.size // 3
    L = lcms()
    with _open_profiles(_lab_profile_bytes(), dst_profile) as (h_lab, h_dst):
        xform = L.cmsCreateTransform(
            h_lab, ctypes.c_uint32(_TYPE_LAB_DBL),
            h_dst, ctypes.c_uint32(_pixel_type(dst_cs)),
            ctypes.c_uint32(INTENTS[intent]), ctypes.c_uint32(0),
        )
        if not xform:
            raise RuntimeError("lcms 无法建立 Lab 色彩转换")
        try:
            channels_out = _channel_count(dst_cs)
            out = np.zeros(n_pixels * channels_out, dtype=np.uint8)
            L.cmsDoTransform(
                xform,
                lab_flat.ctypes.data_as(ctypes.c_void_p),
                out.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_uint32(n_pixels),
            )
            return out.reshape(n_pixels, channels_out)
        finally:
            L.cmsDeleteTransform(xform)


@lru_cache(maxsize=1)
def _lab_profile_bytes() -> bytes:
    return build_lab_profile_bytes()


# ---------------------------------------------------------------------------
# Synthetic CMYK demo profile
# ---------------------------------------------------------------------------

# sRGB linear -> CIE XYZ D50 (Bradford-adapted, Bruce Lindbloom, full precision).
_SRGB_TO_XYZ_D50 = np.array(
    [
        [0.4360747, 0.3850649, 0.1430804],
        [0.2225045, 0.7168786, 0.0606169],
        [0.0139322, 0.0971045, 0.7141733],
    ]
)
_XYZ_TO_SRGB_D50 = np.linalg.inv(_SRGB_TO_XYZ_D50)
_D50 = (0.9642, 1.0, 0.8249)
_GRID_POINTS = 17


def _srgb_eotf(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _srgb_oetf(c: np.ndarray) -> np.ndarray:
    return np.where(
        c <= 0.0031308,
        12.92 * c,
        1.055 * np.power(np.clip(c, 0, None), 1.0 / 2.4) - 0.055,
    )


def _xyz_to_lab(xyz: np.ndarray) -> np.ndarray:
    Xn, Yn, Zn = _D50
    scaled = xyz / np.array([Xn, Yn, Zn])
    f = np.where(
        scaled > 216.0 / 24389.0,
        np.cbrt(scaled),
        7.787 * scaled + 16.0 / 116.0,
    )
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    return np.stack(
        [116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)], axis=-1
    )


def _lab_to_xyz(lab: np.ndarray) -> np.ndarray:
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16.0) / 116.0
    fx = a / 500.0 + fy
    fz = fy - b / 200.0

    def finv(f: np.ndarray) -> np.ndarray:
        return np.where(f**3 > 216.0 / 24389.0, f**3, (f - 16.0 / 116.0) / 7.787)

    Xn, Yn, Zn = _D50
    return np.stack([finv(fx) * Xn, finv(fy) * Yn, finv(fz) * Zn], axis=-1)


def _a2b_table(ink_scale: float = 1.0) -> np.ndarray:
    """CMYK device -> Lab PCS, shape (n^4, 3), uint16 (ICC 16-bit Lab).

    ``ink_scale`` compresses the device gamut (<1 = weaker inks) so tests can
    build a second, byte-distinct CMYK profile that actually moves output
    colors. The default 1.0 reproduces the original demo profile exactly.
    """
    n = _GRID_POINTS
    v = np.linspace(0.0, 1.0, n)
    c, m, y, k = np.meshgrid(v, v, v, v, indexing="ij")
    # Simple "physical" model: ink removes its primary, black removes all.
    s = float(ink_scale)
    r = (1.0 - c * s) * (1.0 - k * s)
    g = (1.0 - m * s) * (1.0 - k * s)
    b = (1.0 - y * s) * (1.0 - k * s)
    rgb_lin = _srgb_eotf(np.stack([r, g, b], axis=-1))
    xyz = rgb_lin @ _SRGB_TO_XYZ_D50.T
    lab = _xyz_to_lab(xyz)
    # ICC 16-bit Lab encoding: L*65535/100, (a,b)*256 + 32768.
    enc = np.stack(
        [lab[..., 0] * 655.35, lab[..., 1] * 256.0 + 32768.0, lab[..., 2] * 256.0 + 32768.0],
        axis=-1,
    )
    return np.clip(np.rint(enc), 0, 65535).astype(np.uint16).reshape(-1, 3)


def _b2a_table() -> np.ndarray:
    """Lab PCS -> CMYK device, shape (n^3, 4), uint16."""
    n = _GRID_POINTS
    Lv = np.linspace(0.0, 100.0, n)
    av = np.linspace(-128.0, 127.0, n)
    bv = np.linspace(-128.0, 127.0, n)
    lab = np.stack(np.meshgrid(Lv, av, bv, indexing="ij"), axis=-1).reshape(-1, 3)
    rgb_lin = np.clip(_lab_to_xyz(lab) @ _XYZ_TO_SRGB_D50.T, 0.0, 1.0)
    rgb = _srgb_oetf(rgb_lin)
    c = 1.0 - rgb[..., 0]
    m = 1.0 - rgb[..., 1]
    y = 1.0 - rgb[..., 2]
    k = np.minimum(np.minimum(c, m), y)
    # Full undercolor removal: CMY normalized after black.
    denom = np.clip(1.0 - k, 1e-12, None)
    cmyk = np.stack(
        [(c - k) / denom, (m - k) / denom, (y - k) / denom, k], axis=-1
    )
    cmyk = np.where(k[..., None] >= 1.0, 0.0, cmyk)
    return np.clip(np.rint(cmyk * 65535.0), 0, 65535).astype(np.uint16)


def _write_profile(prof: ctypes.c_void_p) -> bytes:
    return _profile_to_bytes(prof)


def _clut_tag(table: np.ndarray, n_in: int, n_out: int, grid: int = _GRID_POINTS):
    """Attach a CLUT16 pipeline (tag bytes are owned by the profile)."""
    L = lcms()
    pipe = L.cmsPipelineAlloc(None, n_in, n_out)
    if not pipe:
        raise RuntimeError("cmsPipelineAlloc 失败")
    flat = np.ascontiguousarray(table, dtype=np.uint16)
    ptr = flat.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16))
    stage = L.cmsStageAllocCLut16bit(None, grid, n_in, n_out, ptr)
    if not stage or not L.cmsPipelineInsertStage(pipe, 0, stage):
        L.cmsPipelineFree(pipe)
        raise RuntimeError("CLUT stage 构建失败")
    return pipe


def build_demo_gray_profile_bytes() -> bytes:
    """Synthetic sRGB-tone gray monitor profile (GRAY -> Lab PCS)."""
    L = lcms()
    n = _GRID_POINTS
    v = np.linspace(0.0, 1.0, n)
    lin = _srgb_eotf(v)
    # Neutral sRGB device value (R=G=B=g): XYZ = [g,g,g] @ M.T = g * row sums.
    sums = _SRGB_TO_XYZ_D50.sum(axis=1)
    xyz = lin[:, None] * sums[None, :]
    lab = _xyz_to_lab(xyz)
    a2b = np.stack(
        [lab[:, 0] * 655.35, lab[:, 1] * 256.0 + 32768.0, lab[:, 2] * 256.0 + 32768.0],
        axis=-1,
    )
    a2b = np.clip(np.rint(a2b), 0, 65535).astype(np.uint16).reshape(-1, 3)

    Lv = np.linspace(0.0, 100.0, n)
    av = np.linspace(-128.0, 127.0, n)
    bv = np.linspace(-128.0, 127.0, n)
    lab_grid = np.stack(np.meshgrid(Lv, av, bv, indexing="ij"), axis=-1).reshape(-1, 3)
    rgb_lin = np.clip(_lab_to_xyz(lab_grid) @ _XYZ_TO_SRGB_D50.T, 0.0, 1.0)
    rgb = _srgb_oetf(rgb_lin)
    # Gray device value: luminance-equivalent mean of the reconstructed RGB.
    gray = rgb.mean(axis=1)
    b2a = np.clip(np.rint(gray * 65535.0), 0, 65535).astype(np.uint16).reshape(-1)

    prof = L.cmsCreateProfilePlaceholder(None)
    try:
        L.cmsSetProfileVersion(prof, 2.4)
        L.cmsSetDeviceClass(prof, sig(b"mntr"))
        L.cmsSetColorSpace(prof, sig(b"GRAY"))
        L.cmsSetPCS(prof, sig(b"Lab "))
        mlu = L.cmsMLUalloc(None, 1)
        L.cmsMLUsetASCII(mlu, b"en", b"US", b"Soft Proofing Demo sRGB Gray (synthetic)")
        L.cmsWriteTag(prof, sig(b"desc"), mlu)
        cprt = L.cmsMLUalloc(None, 1)
        L.cmsMLUsetASCII(cprt, b"en", b"US", b"Public domain synthetic demo profile")
        L.cmsWriteTag(prof, sig(b"cprt"), cprt)
        L.cmsWriteTag(prof, sig(b"wtpt"), L.cmsD50_XYZ())
        p1 = _clut_tag(a2b, 1, 3)
        ok = L.cmsWriteTag(prof, sig(b"A2B0"), p1)
        L.cmsPipelineFree(p1)
        if not ok:
            raise RuntimeError("gray A2B0 写入失败")
        p2 = _clut_tag(b2a, 3, 1)
        ok = L.cmsWriteTag(prof, sig(b"B2A0"), p2)
        L.cmsPipelineFree(p2)
        if not ok:
            raise RuntimeError("gray B2A0 写入失败")
        return _profile_to_bytes(prof)
    finally:
        L.cmsCloseProfile(prof)


def build_demo_cmyk_profile_bytes(
    description: str = "Soft Proofing Demo SWOP-like CMYK (synthetic, do not use for production)",
    ink_scale: float = 1.0,
) -> bytes:
    """Build a self-contained, valid CMYK output profile (ICC v2.4).

    The gamut model is intentionally simple (complementary inks + sRGB
    chromaticities) -- it exists so the full pipeline runs out of the box;
    users should upload real press profiles for production work. ``ink_scale``
    allows generating a byte-distinct variant (used by regression tests).
    """
    L = lcms()
    prof = L.cmsCreateProfilePlaceholder(None)
    if not prof:
        raise RuntimeError("cmsCreateProfilePlaceholder 失败")
    try:
        L.cmsSetProfileVersion(prof, 2.4)
        L.cmsSetDeviceClass(prof, sig(b"prtr"))
        L.cmsSetColorSpace(prof, sig(b"CMYK"))
        L.cmsSetPCS(prof, sig(b"Lab "))

        mlu = L.cmsMLUalloc(None, 1)
        L.cmsMLUsetASCII(mlu, b"en", b"US", description.encode("utf-8"))
        if not L.cmsWriteTag(prof, sig(b"desc"), mlu):
            raise RuntimeError("写入 desc 标签失败")

        cprt = L.cmsMLUalloc(None, 1)
        L.cmsMLUsetASCII(cprt, b"en", b"US", b"Public domain synthetic demo profile")
        if not L.cmsWriteTag(prof, sig(b"cprt"), cprt):
            raise RuntimeError("写入 cprt 标签失败")

        if not L.cmsWriteTag(prof, sig(b"wtpt"), L.cmsD50_XYZ()):
            raise RuntimeError("写入 wtpt 标签失败")

        for tag, table, n_in, n_out in (
            (b"A2B0", _a2b_table(ink_scale), 4, 3),
            (b"B2A0", _b2a_table(), 3, 4),
        ):
            pipe = L.cmsPipelineAlloc(None, n_in, n_out)
            if not pipe:
                raise RuntimeError("cmsPipelineAlloc 失败")
            flat = np.ascontiguousarray(table, dtype=np.uint16)
            ptr = flat.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16))
            stage = L.cmsStageAllocCLut16bit(
                None, _GRID_POINTS, n_in, n_out, ptr
            )
            if not stage or not L.cmsPipelineInsertStage(pipe, 0, stage):
                L.cmsPipelineFree(pipe)
                raise RuntimeError(f"构建 {tag.decode()} CLUT 失败")
            ok = L.cmsWriteTag(prof, sig(tag), pipe)
            L.cmsPipelineFree(pipe)  # profile owns its dup of the pipeline
            if not ok:
                raise RuntimeError(f"写入 {tag.decode()} 标签失败")

        return _profile_to_bytes(prof)
    finally:
        L.cmsCloseProfile(prof)
