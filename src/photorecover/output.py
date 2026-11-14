"""Where recovered files are saved, and under what names.

Files that came back whole go into Recovered, sorted by what they are (Photos, Videos, ...) so
that the photos are one click away. The folders the card had are not rebuilt; the report says
where each file used to be. Partial files and their preview-filled copies go into folders of
their own, so nobody mistakes them for whole ones.
"""
from __future__ import annotations

import io
import warnings
from pathlib import Path

import numpy as np
from PIL import Image

from . import jpeg
from .items import JPEG_EXT, PHOTO_EXT, VIDEO_EXT
from .util import file_ext

FOLDER_OK = "Recovered"
FOLDER_PARTIAL = "Recovered - Partial"
FOLDER_FILLED = "Recovered - Preview filled"
FOLDER_LIVE = "Still on the card"
FOLDER_SMALL = "Small pictures (thumbnails and app images)"
SMALL_PICTURE = 640          # longest side, in pixels, below which a picture is not counted as a photo
DOC_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "rtf", "odt", "ods", "odp", "csv", "epub", "pages", "numbers"}
MUSIC_EXT = {"mp3", "wav", "flac", "m4a", "ogg", "oga", "opus", "wma", "aac", "amr", "mid", "aiff"}
FULL_NOTE = "not saved: the drive the results are being saved to ran out of space"


def kind_folder(name: str, data: bytes = b"") -> str:
    """The folder a recovered file is sorted into, by what kind of file it is."""
    ext = file_ext(name)
    if ext in JPEG_EXT or ext in ("png", "gif", "bmp"):
        # thumbnails, icons and pictures apps keep for themselves would bury the real photos
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with Image.open(io.BytesIO(data)) as im:
                    if max(im.size) < SMALL_PICTURE:
                        return FOLDER_SMALL
        except Exception:  # noqa: BLE001
            pass
    return ("Photos" if ext in PHOTO_EXT else "Videos" if ext in VIDEO_EXT else "Documents" if ext in DOC_EXT
            else "Music and sound" if ext in MUSIC_EXT else "Other files")


def preview_fill(data: bytes, percent: float) -> bytes | None:
    """A copy of a partly recovered photo whose missing lower part is filled in from the
    photo's own small preview, enlarged. The filled part is blurry but it is the real
    picture, not an invention. None if the photo has no usable preview."""
    from PIL import ImageFile
    try:
        info = jpeg.parse_header(data)
        thumb = jpeg.decode_thumb(info)
        if thumb is None or percent <= 0:
            return None
        w, h = info.width, info.height
        fill = Image.fromarray(np.clip(thumb, 0, 255).astype(np.uint8), "YCbCr").convert("RGB")
        fill = fill.resize((w, h), Image.BICUBIC)
        old = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            with Image.open(io.BytesIO(bytes(data) + b"\xff\xd9")) as im:
                real = im.convert("RGB")
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = old
        if real.size != (w, h):
            return None
        good = max(0, (int(percent / 100.0 * h) // 16 - 1) * 16)   # whole block rows known to be real
        fade = min(24, good)
        mask = np.zeros((h, 1), np.float32)                         # 1 where the real picture is kept
        mask[:good - fade] = 1.0
        if fade:
            mask[good - fade:good, 0] = np.linspace(1.0, 0.0, fade)
        a = np.asarray(real, np.float32) * mask[:, :, None] + np.asarray(fill, np.float32) * (1 - mask[:, :, None])
        buf = io.BytesIO()
        Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB").save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - this copy is a convenience; the partial file is already saved
        return None


def write_whole(dest: Path, data: bytes) -> None:
    """Write a file whole or not at all: a cut-short copy must never be left among the results."""
    try:
        dest.write_bytes(data)
    except OSError:
        try:
            dest.unlink()
        except OSError:
            pass
        raise


def cluster_runs(chain: list[int]) -> list[list[int]]:
    """A chain of clusters as [first, count] runs: where on the card the file's pieces were."""
    out: list[list[int]] = []
    for c in chain:
        if out and c >= 0 and out[-1][0] + out[-1][1] == c:
            out[-1][1] += 1
        else:
            out.append([c, 1])
    return out
