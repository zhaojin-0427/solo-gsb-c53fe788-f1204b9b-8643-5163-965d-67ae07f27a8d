"""ICC profile discovery, storage and strict validation."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageCms

from . import lcms_native

# Device-color-space -> required input channels for the pixel data.
COLORSPACE_CHANNELS = {
    "RGB": 3,
    "CMYK": 4,
    "GRAY": 1,
}

# Abstract Lab profiles (class "abst") are used internally only.
ABSTRACT_CLASS = "abst"


class ProfileError(ValueError):
    """Raised when an ICC profile is missing, invalid or role-incompatible."""


@dataclass(frozen=True)
class ProfileRecord:
    profile_id: str
    name: str
    role: str  # source | press | display
    builtin: bool
    color_space: str
    device_class: str
    pcs: str
    size: int
    sha256: str
    filename: str

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "role": self.role,
            "builtin": self.builtin,
            "color_space": self.color_space,
            "device_class": self.device_class,
            "pcs": self.pcs,
            "size": self.size,
            "sha256": self.sha256,
        }


class ProfileManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir_source = self.root / "source"
        self.dir_press = self.root / "press"
        self.dir_display = self.root / "display"
        self.dir_builtin = self.root / "builtin"
        for d in (self.dir_source, self.dir_press, self.dir_display, self.dir_builtin):
            d.mkdir(parents=True, exist_ok=True)
        self.ensure_builtin_profiles()

    # -- built-ins ---------------------------------------------------------

    def ensure_builtin_profiles(self) -> None:
        self._write_builtin("builtin-srgb", "sRGB IEC61966-2.1 (built-in)",
                            lcms_native.build_srgb_profile_bytes())
        self._write_builtin("builtin-lab", "CIELAB identity (built-in)",
                            lcms_native.build_lab_profile_bytes())
        gray_path = self.dir_builtin / "builtin-demo-gray.icc"
        if not gray_path.exists():
            gray_path.write_bytes(lcms_native.build_demo_gray_profile_bytes())
        cmyk_path = self.dir_builtin / "builtin-demo-cmyk.icc"
        if not cmyk_path.exists():
            cmyk_path.write_bytes(lcms_native.build_demo_cmyk_profile_bytes())

    def _write_builtin(self, profile_id: str, name: str, data: bytes) -> None:
        # Built-in sRGB/Lab profiles are exposed as selectable display/source
        # profiles; we just keep their bytes on disk for uniform loading.
        path = self.dir_builtin / f"{profile_id}.icc"
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)

    # -- scanning ----------------------------------------------------------

    def _iter_files(self):
        for role, directory in (
            ("source", self.dir_source),
            ("press", self.dir_press),
            ("display", self.dir_display),
            ("builtin", self.dir_builtin),
        ):
            for p in sorted(directory.iterdir()):
                if p.is_file() and p.suffix.lower() in (".icc", ".icm"):
                    yield role, p

    def list_profiles(self) -> list[ProfileRecord]:
        out: list[ProfileRecord] = []
        for storage_role, path in self._iter_files():
            try:
                data = path.read_bytes()
                header = lcms_native.read_header(data)
                # Ask lcms to actually open it to catch malformed LUTs/tags.
                self._open(data)
            except Exception:
                # Skip corrupt profiles in listings rather than failing the UI.
                continue
            roles = self._roles_for(storage_role, header["device_class"], header["color_space"])
            digest = hashlib.sha256(data).hexdigest()
            profile_id = path.stem
            name = self._profile_name(data, path)
            for role in roles:
                out.append(
                    ProfileRecord(
                        profile_id=profile_id,
                        name=name,
                        role=role,
                        builtin=storage_role == "builtin",
                        color_space=header["color_space"],
                        device_class=header["device_class"],
                        pcs=header["pcs"],
                        size=len(data),
                        sha256=digest[:16],
                        filename=path.name,
                    )
                )
        return out

    def _roles_for(self, storage_role: str, device_class: str, color_space: str) -> list[str]:
        """Which pipeline roles a profile may serve."""
        if storage_role == "builtin":
            if color_space == "RGB" and device_class == "mntr":
                return ["source", "display"]
            if color_space == "GRAY" and device_class == "mntr":
                return ["source"]
            if color_space == "Lab":
                return ["lab"]
            if color_space == "CMYK" and device_class in ("prtr", "spac"):
                # A CMYK device profile can also be used to interpret CMYK
                # source pixels (its A2B0 tag does device->PCS).
                return ["press", "source"]
            return []
        if storage_role == "source":
            # Uploaded source profiles may be RGB/CMYK scanner/camera/monitor.
            if color_space in ("RGB", "CMYK", "GRAY"):
                return ["source"]
            return []
        if storage_role == "press":
            if color_space == "CMYK" and device_class in ("prtr", "spac"):
                return ["press"]
            return []
        if storage_role == "display":
            if color_space == "RGB" and device_class == "mntr":
                return ["display"]
            return []
        return []

    def _profile_name(self, data: bytes, path: Path) -> str:
        try:
            prof = self._open(data)
            name = ImageCms.getProfileName(prof).strip()
            if name and name not in ("", "\x00"):
                return name
        except Exception:
            pass
        return path.stem

    # -- loading / validation ---------------------------------------------

    def _open(self, data: bytes) -> ImageCms.ImageCmsProfile:
        return ImageCms.ImageCmsProfile(io.BytesIO(data))

    def load_bytes(self, profile_id: str) -> bytes:
        for _, path in self._iter_files():
            if path.stem == profile_id:
                return path.read_bytes()
        raise ProfileError(f"配置不存在: {profile_id}")

    def get(self, profile_id: str, role: str) -> ImageCms.ImageCmsProfile:
        """Load a profile and verify it can serve `role`."""
        data = self.load_bytes(profile_id)
        try:
            header = lcms_native.read_header(data)
            prof = self._open(data)
        except ProfileError:
            raise
        except Exception as exc:
            raise ProfileError(f"ICC 配置无效 ({profile_id}): {exc}") from exc
        self.validate_for_role(header, role)
        return prof

    def get_bytes_header(self, profile_id: str) -> tuple[bytes, dict]:
        data = self.load_bytes(profile_id)
        header = lcms_native.read_header(data)
        return data, header

    def validate_for_role(self, header: dict, role: str) -> None:
        cs = header["color_space"]
        dc = header["device_class"]
        if role == "press":
            if cs != "CMYK":
                raise ProfileError(
                    f"印刷配置色彩空间必须是 CMYK，实际为 {cs}"
                )
            if dc not in ("prtr", "spac"):
                raise ProfileError(
                    f"印刷配置类别必须是 prtr/spac，实际为 {dc}"
                )
        elif role == "display":
            if cs != "RGB" or dc != "mntr":
                raise ProfileError(
                    f"显示配置必须是 RGB 显示器配置 (mntr/RGB)，实际为 {dc}/{cs}"
                )
        elif role == "source":
            if cs not in ("RGB", "CMYK", "GRAY"):
                raise ProfileError(
                    f"源配置的色彩空间不支持: {cs}（仅支持 RGB/CMYK/GRAY）"
                )
            if dc == ABSTRACT_CLASS:
                raise ProfileError("抽象 Lab 配置不能用作源配置")

    def open_pil(self, profile_id: str) -> ImageCms.ImageCmsProfile:
        return self._open(self.load_bytes(profile_id))

    def profile_digest(self, profile_id: str) -> str:
        return hashlib.sha256(self.load_bytes(profile_id)).hexdigest()

    def summary(self, profile_id: str) -> dict:
        data = self.load_bytes(profile_id)
        header = lcms_native.read_header(data)
        return {
            "profile_id": profile_id,
            "name": self._profile_name(data, Path(profile_id)),
            "sha256": hashlib.sha256(data).hexdigest(),
            **header,
        }

    # -- upload ------------------------------------------------------------

    def save_upload(self, role: str, filename: str, data: bytes) -> ProfileRecord:
        if role not in ("source", "press", "display"):
            raise ProfileError(f"未知配置角色: {role}")
        # Full validation before touching disk.
        try:
            header = lcms_native.read_header(data)
            self._open(data)
        except ProfileError:
            raise
        except Exception as exc:
            raise ProfileError(f"ICC 配置无效: {exc}") from exc
        self.validate_for_role(header, role)

        digest = hashlib.sha256(data).hexdigest()[:16]
        safe_stem = Path(filename).stem
        profile_id = f"{safe_stem}-{digest[:8]}"
        target_dir = {
            "source": self.dir_source,
            "press": self.dir_press,
            "display": self.dir_display,
        }[role]
        path = target_dir / f"{profile_id}.icc"
        path.write_bytes(data)
        return ProfileRecord(
            profile_id=profile_id,
            name=self._profile_name(data, path),
            role=role,
            builtin=False,
            color_space=header["color_space"],
            device_class=header["device_class"],
            pcs=header["pcs"],
            size=len(data),
            sha256=digest,
            filename=path.name,
        )
