# SPDX-License-Identifier: MIT
"""Design a new look for the Xiaole device from a conversation.

The voice model turns the user's description into a spec (``SPEC_SCHEMA`` in
the workspace's ``assets/xiaole/generate.py``); :class:`AvatarDesigner`
renders it into the 21 expression frames, shows a contact sheet for a look
before the user says yes, and then builds the assets image the device
downloads: the existing speech models and wake word plus the avatar pack,
packed with the firmware's own ``build_default_assets.configure_assets_models``
so the result is what ``package_firmware.py`` would have produced.

Paths come from the esp32-ai workspace (``STACKCHAN_XIAOLE_WORKSPACE``; by
default the directory that holds this gateway checkout):

- ``assets/xiaole/generate.py``: the renderer (imported by path, no Pillow);
- ``firmware/xiaozhi-esp32/scripts/build_default_assets.py``: the packer;
- ``firmware/xiaozhi-esp32/managed_components/espressif__esp-sr/model/multinet_model``:
  the MultiNet model directories (``mn6_cn`` + ``fst``);
- ``artifacts/xiaole-motion/xiaole-assets.bin`` (``STACKCHAN_ASSETS_BASE_IMAGE``):
  the packaged assets image whose font, models and wake word are kept.

Built images live under ``STACKCHAN_AVATAR_DIR`` (default ``avatars/`` next to
the gateway package) and are served by ``GET /xiaozhi/assets/<sha>.bin``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ASSETS_PARTITION_SIZE = 9 * 1024 * 1024
DEVICE_UPDATE_TOOL = "self.avatar.update"
DEVICE_GET_TOOL = "self.avatar.get"
DEVICE_PREVIEW_TOOL = "self.avatar.preview"
DEVICE_PREVIEW_END_TOOL = "self.avatar.preview_end"
IMAGE_DIR = "images"
PACK_FILE = "avatar.bin"
SHEET_FILE = "contact-sheet.png"


def workspace_root() -> Path:
    raw = os.getenv("STACKCHAN_XIAOLE_WORKSPACE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[4]


def generator_path() -> Path:
    return workspace_root() / "assets" / "xiaole" / "generate.py"


def firmware_scripts_dir() -> Path:
    return workspace_root() / "firmware" / "xiaozhi-esp32" / "scripts"


def multinet_model_dir() -> Path:
    return workspace_root() / "firmware" / "xiaozhi-esp32" / "managed_components" / "espressif__esp-sr" / "model" / "multinet_model"


def base_image_path() -> Path:
    raw = os.getenv("STACKCHAN_ASSETS_BASE_IMAGE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return workspace_root() / "artifacts" / "xiaole-motion" / "xiaole-assets.bin"


def avatar_dir() -> Path:
    raw = os.getenv("STACKCHAN_AVATAR_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[1] / "avatars"


_generator_cache: dict[str, Any] = {}


def load_generator(path: Path | None = None):
    """Import ``assets/xiaole/generate.py`` as a module (cached per path)."""
    path = path or generator_path()
    key = str(path)
    module = _generator_cache.get(key)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location("xiaole_generate", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"avatar generator not found: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _generator_cache[key] = module
    return module


def load_assets_builder(scripts_dir: Path | None = None):
    """The firmware's build_default_assets module (read/pack entries, configure models)."""
    scripts_dir = scripts_dir or firmware_scripts_dir()
    path = scripts_dir / "build_default_assets.py"
    if not path.is_file():
        raise FileNotFoundError(f"firmware assets packer not found: {path}")
    spec = importlib.util.spec_from_file_location("xiaole_build_default_assets", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def available() -> bool:
    """Whether the generator and a base image exist, i.e. the tools can be offered."""
    return generator_path().is_file() and base_image_path().is_file()


def spec_schema() -> dict[str, Any] | None:
    try:
        return dict(load_generator().SPEC_SCHEMA)
    except (FileNotFoundError, AttributeError):
        return None


def appearance_text(spec: dict[str, Any], look: str) -> str:
    """The ``{{appearance}}`` sentence personality.md gets, e.g.
    '一只住在主人桌上的像素小猫：橘色的身体、尖尖的耳朵、红色的围巾'."""
    head, _, rest = look.partition("，")
    species = head[2:] if head.startswith("一只") else head  # e.g. 黑色的小猫
    return f"一只住在主人桌上的像素{species}：{rest.replace('，', '、')}" if rest else f"一只住在主人桌上的像素{species}"


@dataclass
class PendingDesign:
    id: str
    spec: dict[str, Any]
    description: str
    look: str
    pack: bytes
    sheet: Path
    created_at: float


@dataclass
class AvatarBuild:
    id: str
    name: str
    look: str
    spec: dict[str, Any]
    pack_sha256: str
    image: Path
    image_sha256: str
    image_size: int

    @property
    def file_name(self) -> str:
        return self.image.name

    def as_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "pack_sha256": self.pack_sha256,
            "image": self.file_name,
            "image_sha256": self.image_sha256,
            "image_size": self.image_size,
        }


class AvatarDesigner:
    """Renders specs and builds the assets image the device downloads."""

    def __init__(self, *, root: Path | None = None, base_image: Path | None = None,
                 models_dir: Path | None = None, generator=None, builder=None):
        self.root = root or avatar_dir()
        self.base_image = base_image or base_image_path()
        self.models_dir = models_dir or multinet_model_dir()
        self._generator = generator
        self._builder = builder

    @property
    def generator(self):
        if self._generator is None:
            self._generator = load_generator()
        return self._generator

    @property
    def builder(self):
        if self._builder is None:
            self._builder = load_assets_builder()
        return self._builder

    # -- design ---------------------------------------------------------------

    def design(self, spec: Any, description: str = "") -> PendingDesign:
        """Normalise and render ``spec``; writes the pack and a contact sheet under
        ``root/<id>/``. Raises the generator's SpecError when it cannot be drawn."""
        gen = self.generator
        rendered = gen.render_spec(spec)  # SpecError propagates with a spoken reply
        pack = gen.pack_bytes(rendered)
        folder = self.root / rendered.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "spec.json").write_text(json.dumps(rendered.spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (folder / "avatar.bin").write_bytes(pack)
        if description:
            (folder / "description.txt").write_text(description.strip() + "\n", encoding="utf-8")
        sheet = folder / "contact-sheet.png"
        gen.contact_sheet_png(rendered, sheet)
        look = gen.describe_spec(rendered.spec)
        logger.info("avatar design %s (%s): %s -> %s", rendered.id, rendered.name, description or "-", sheet)
        return PendingDesign(rendered.id, rendered.spec, description, look, pack, sheet, time.time())

    # -- build ----------------------------------------------------------------

    def build(self, pending: PendingDesign) -> AvatarBuild:
        """Pack the pending design with the speech models into an assets image."""
        images = self.root / IMAGE_DIR
        images.mkdir(parents=True, exist_ok=True)
        tmp = images / f"build-{pending.id}.tmp"
        try:
            data = self.build_assets_image(pending.pack, tmp)
        finally:
            tmp_base = tmp.with_suffix(".base")
            if tmp_base.exists():
                tmp_base.unlink()
        sha = hashlib.sha256(data).hexdigest()
        image = images / f"{sha[:32]}.bin"
        tmp.replace(image)
        build = AvatarBuild(
            id=pending.id,
            name=str(pending.spec.get("name", "")),
            look=pending.look,
            spec=pending.spec,
            pack_sha256=hashlib.sha256(pending.pack).hexdigest(),
            image=image,
            image_sha256=sha,
            image_size=len(data),
        )
        (self.root / pending.id / "image.json").write_text(json.dumps(build.as_record(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("avatar image %s: %d bytes for pack %s", image.name, len(data), pending.id)
        return build

    def build_assets_image(self, pack: bytes, out: Path) -> bytes:
        """``base image`` + the pack -> ``out``; returns the image bytes.

        The base image's index.json keeps its font, models and ``multinet_model``
        (the current wake word); ``avatar_pack`` is added. When the MultiNet
        model directories are available the models are re-packed with the
        firmware's ``configure_assets_models`` exactly like package_firmware.py;
        otherwise the base image's srmodels.bin is kept as is.
        """
        b = self.builder
        files = b.read_assets_entries(str(self.base_image), 32)
        if "index.json" not in files:
            raise ValueError(f"{self.base_image} has no index.json")
        index = json.loads(files["index.json"][0])
        wake = index.get("multinet_model")
        files["avatar.bin"] = (pack, 0, 0)
        index["avatar_pack"] = "avatar.bin"
        files["index.json"] = (json.dumps(index, ensure_ascii=False).encode("utf-8"), 0, 0)
        out.parent.mkdir(parents=True, exist_ok=True)
        models = [self.models_dir / "mn6_cn", self.models_dir / "fst"]
        if all(m.is_dir() for m in models):
            base = out.with_suffix(".base")
            base.write_bytes(b.pack_assets_entries(files, 32))
            b.configure_assets_models(str(base), str(out), [], [str(m) for m in models], idf_target="esp32s3",
                                      multinet_model_info=wake, max_size=ASSETS_PARTITION_SIZE)
            base.unlink()
        else:
            logger.warning("MultiNet models not found under %s; keeping the base image's srmodels.bin", self.models_dir)
            packed = b.pack_assets_entries(files, 32)
            if len(packed) > ASSETS_PARTITION_SIZE:
                raise ValueError(f"assets image {len(packed)} bytes exceeds the {ASSETS_PARTITION_SIZE} byte partition")
            out.write_bytes(packed)
        data = out.read_bytes()
        check = b.read_assets_entries(str(out), 32)
        result = json.loads(check["index.json"][0])
        if result.get("avatar_pack") != "avatar.bin" or "avatar.bin" not in check or result.get("srmodels") not in check:
            raise ValueError("assets image verification failed")
        if wake is not None and result.get("multinet_model") != wake:
            raise ValueError("wake word lost while packing the assets image")
        return data

    def prune(self, keep: set[str]) -> None:
        """Drop built images other than ``keep`` (file names); designs are tiny and stay."""
        images = self.root / IMAGE_DIR
        if not images.is_dir():
            return
        for path in images.glob("*.bin"):
            if path.name not in keep:
                try:
                    path.unlink()
                except OSError:
                    logger.debug("could not remove %s", path, exc_info=True)


def known_design(root: Path, pack_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(spec, image record) of a design this gateway built, or None."""
    folder = root / pack_id
    try:
        spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
        record = json.loads((folder / "image.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (spec, record) if isinstance(spec, dict) and isinstance(record, dict) else None


def adopt_device_avatar(store, device_info: dict[str, Any] | None, root: Path | None = None) -> str:
    """Make persona.json agree with the pack the device reports it is drawing.

    apply_avatar saves persona.json only after the device's reply; the device
    reboots right after answering, so a lost reply leaves the file behind. When
    the device comes back showing a pack this gateway built, the design's spec
    and image record are adopted (and announced once). Returns a word for the
    log: adopted / in_sync / builtin / unknown / skipped.
    """
    if not isinstance(device_info, dict):
        return "skipped"
    pack_id = str(device_info.get("id") or "")
    current = store.load()
    current_id = (current.avatar_pack or {}).get("id")
    if device_info.get("source") != "pack" or not pack_id:
        return "builtin"
    if pack_id == current_id:
        return "in_sync"
    design = known_design(root or avatar_dir(), pack_id)
    if design is None:
        return "unknown"
    spec, record = design
    try:
        look = load_generator().describe_spec(spec)
    except (FileNotFoundError, AttributeError, Exception):  # noqa: BLE001 - keep the sync best-effort
        look = str(device_info.get("name") or pack_id)
    store.set_avatar(appearance_text(spec, look), spec, {**record, "id": pack_id})
    logger.info("avatar: adopted pack %s from the device (persona.json had %s)", pack_id, current_id or "builtin")
    return "adopted"


def image_path(root: Path, name: str) -> Path:
    return root / IMAGE_DIR / name


def _base_url(origin: str | None) -> str:
    """``STACKCHAN_ASSETS_BASE_URL`` (e.g. ``http://192.168.10.58:8766``) wins;
    otherwise the origin the device used for its last OTA check."""
    return (os.getenv("STACKCHAN_ASSETS_BASE_URL", "").strip() or (origin or "")).rstrip("/")


def assets_url(file_name: str, origin: str | None) -> str | None:
    """Where the device downloads an assets image (or a design's pack, by ``<id>.bin``)."""
    base = _base_url(origin)
    return f"{base}/xiaozhi/assets/{file_name}" if base else None


def pack_url(pack_id: str, origin: str | None) -> str | None:
    """The design's pack for a live preview on the device (served from ``avatars/<id>/avatar.bin``)."""
    return assets_url(f"{pack_id}.bin", origin)


def sheet_url(pack_id: str, origin: str | None) -> str | None:
    """The design's contact sheet for a phone or browser."""
    base = _base_url(origin)
    return f"{base}/xiaozhi/avatars/{pack_id}.png" if base else None


def merge_spec(base: dict[str, Any], changes: dict[str, Any] | None) -> dict[str, Any]:
    """``changes`` over ``base``: scalars replace, ``accessories`` replaces the whole list,
    empty strings / None in ``changes`` are ignored (the model often sends them)."""
    merged = dict(base)
    for key, value in (changes or {}).items():
        if value is None or value == "":
            continue
        merged[key] = list(value) if key == "accessories" and isinstance(value, list) else value
    return merged


def prune_old_designs(root: Path, keep_ids: set[str], max_age_s: float = 7 * 86400) -> None:
    """Remove design folders older than a week that are not referenced."""
    if not root.is_dir():
        return
    now = time.time()
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name == IMAGE_DIR or folder.name in keep_ids:
            continue
        try:
            if now - folder.stat().st_mtime > max_age_s:
                shutil.rmtree(folder)
        except OSError:
            logger.debug("could not remove %s", folder, exc_info=True)


__all__ = [
    "ASSETS_PARTITION_SIZE", "AvatarBuild", "AvatarDesigner", "DEVICE_GET_TOOL", "DEVICE_UPDATE_TOOL", "PendingDesign",
    "DEVICE_PREVIEW_END_TOOL", "DEVICE_PREVIEW_TOOL", "PACK_FILE", "SHEET_FILE",
    "adopt_device_avatar", "appearance_text", "assets_url", "available", "avatar_dir", "base_image_path", "image_path",
    "known_design", "load_generator", "merge_spec", "pack_url", "sheet_url",
    "spec_schema", "workspace_root",
]

if sys.version_info < (3, 10):  # pragma: no cover
    raise RuntimeError("stackchan_mcp.avatar needs Python 3.10+")
