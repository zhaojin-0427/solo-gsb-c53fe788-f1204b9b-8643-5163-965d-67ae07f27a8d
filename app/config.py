"""Application configuration and shared singletons."""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageCms

from . import lcms_native
from .profiles import ProfileManager

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SPD_DATA_DIR", BASE_DIR.parent / "data"))
PROFILE_DIR = DATA_DIR / "profiles"
JOB_DIR = DATA_DIR / "jobs"
# 局部 CMYK 修色产物（按输入内容寻址、确定性复用）。
RETOUCH_DIR = DATA_DIR / "retouch"
# 印刷配置版本发布与回归门禁模块的不可变存储（草稿快照、基线夹具、门禁产物）。
RELEASE_DIR = DATA_DIR / "release"
DB_PATH = DATA_DIR / "softproof.db"

MAX_DIMENSION = int(os.environ.get("SPD_MAX_DIMENSION", "3000"))
MAX_UPLOAD_MB = int(os.environ.get("SPD_MAX_UPLOAD_MB", "50"))


@dataclass
class AppState:
    pm: ProfileManager
    srgb_profile: ImageCms.ImageCmsProfile
    srgb_bytes: bytes


_state: AppState | None = None


def init_state() -> AppState:
    global _state
    if _state is None:
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        RETOUCH_DIR.mkdir(parents=True, exist_ok=True)
        pm = ProfileManager(PROFILE_DIR)
        srgb_bytes = lcms_native.build_srgb_profile_bytes()
        srgb_profile = ImageCms.ImageCmsProfile(io.BytesIO(srgb_bytes))
        _state = AppState(
            pm=pm,
            srgb_profile=srgb_profile,
            srgb_bytes=srgb_bytes,
        )
    return _state
