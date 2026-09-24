"""OCR service: POST /ocr with an image or PDF, get back its text per page.

PDF pages that already carry text are read directly (exact, instant); pages
without text, pages that are mostly an image (a printed e-mail with a pasted
invoice), and images go through PaddleOCR.

Runs on the OMS server, bound to 127.0.0.1, so only Django on the same
machine can call it. Set up by `setup.ps1` in this folder.

Settings (environment variables, all optional):
  OCR_MODELS     mobile (default) | server: on the OMS server server was ~5.5x
                 slower (48 s vs 8.5 s per invoice photo) for the same key fields
  OCR_MODEL_DIR  where the model folders are; default: .\\models next to this file
  OCR_ONEDNN     1 (default) | 0 to switch off Intel oneDNN acceleration: only
                 if PaddlePaddle fails with a oneDNN error; several times slower
  OCR_MAX_SIDE   longest side, in pixels, an image is shrunk to before OCR (2400)
  OCR_THREADS    CPU threads the OCR engine uses (16)
  OCR_BATCH      text lines recognised per batch (16); 1 is PaddleOCR's default
                 and recognises a 100-line invoice one line at a time
"""
import io
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
import paddle
import pymupdf
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from paddleocr import PaddleOCR
from PIL import Image

log = logging.getLogger("ocr")

MAX_PAGES = 30
#: A PDF page with at least this much text is read as text, not OCR'd.
MIN_PDF_TEXT = 30
#: ...unless images cover this much of it: then its text layer is probably
#: only the page's frame (a printed e-mail around a pasted invoice) and the
#: invoice itself is in the image, so the whole rendered page is OCR'd.
IMAGE_COVERAGE_FOR_OCR = 0.25
PDF_DPI = 200

MODEL_SET = os.environ.get("OCR_MODELS", "mobile").strip().lower()
if MODEL_SET not in ("server", "mobile"):
    raise SystemExit(f"OCR_MODELS must be 'server' or 'mobile', not {MODEL_SET!r}")
MODEL_DIR = Path(os.environ.get("OCR_MODEL_DIR", Path(__file__).parent / "models"))
ONEDNN = os.environ.get("OCR_ONEDNN", "1").strip().lower() not in ("0", "false", "no")
#: A phone photo is ~4000x3000; text detection at full size is what made a
#: WhatsApp bill take a minute. Invoice text stays readable at this size.
MAX_SIDE = int(os.environ.get("OCR_MAX_SIDE", "2400"))
THREADS = int(os.environ.get("OCR_THREADS", "16"))
BATCH = int(os.environ.get("OCR_BATCH", "16"))


def _model_kwargs():
    """Model names, plus the local folder for each one that has been copied in.

    With local folders nothing is downloaded at startup, so the server needs no
    internet once set up. Without them, PaddleOCR downloads into the running
    user's profile (which is what setup.ps1 does once, then copies them here).
    """
    det, rec = f"PP-OCRv5_{MODEL_SET}_det", f"PP-OCRv5_{MODEL_SET}_rec"
    kwargs = {
        "text_detection_model_name": det,
        "text_recognition_model_name": rec,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        # Rotated / sideways text lines, as in phone photos.
        "use_textline_orientation": True,
        "enable_mkldnn": ONEDNN,
        "cpu_threads": THREADS,
        "text_recognition_batch_size": BATCH,
        "textline_orientation_batch_size": BATCH,
    }
    if (MODEL_DIR / det).is_dir():
        kwargs["text_detection_model_dir"] = str(MODEL_DIR / det)
    if (MODEL_DIR / rec).is_dir():
        kwargs["text_recognition_model_dir"] = str(MODEL_DIR / rec)
    orientation = sorted(MODEL_DIR.glob("*textline_ori*")) if MODEL_DIR.is_dir() else []
    if orientation:
        kwargs["textline_orientation_model_name"] = orientation[0].name
        kwargs["textline_orientation_model_dir"] = str(orientation[0])
    return kwargs


app = FastAPI(title="OCR")

# Loaded ONCE, at startup: loading takes seconds, a request should not.
ocr = PaddleOCR(**_model_kwargs())
# The OCR pipeline is not safe to call from two threads at once.
lock = threading.Lock()


def _to_bgr(img: Image.Image) -> np.ndarray:
    # `[..., ::-1]` alone is a reversed-stride VIEW, which OpenCV (inside
    # PaddleOCR) refuses; make it a real array.
    return np.ascontiguousarray(np.array(img.convert("RGB"))[:, :, ::-1])


def _rows(items) -> list:
    """`items` are (x0, y0, x1, y1, text); one string per visual row.

    Text whose vertical centres are within ~half a line is one row, read left
    to right, so a statement photo keeps its date, narration and amount
    together. A wide gap (a column boundary) is kept as three spaces. The same
    rule as OMS's `advance_payment/services/proof_reader.group_rows`, which
    does this for PDFs that carry text.
    """
    items = [i for i in items if str(i[4]).strip()]
    if not items:
        return []
    heights = sorted(max(i[3] - i[1], 1.0) for i in items)
    line = heights[len(heights) // 2]
    items.sort(key=lambda i: ((i[1] + i[3]) / 2, i[0]))
    rows, current, centre = [], [], 0.0
    for item in items:
        mid = (item[1] + item[3]) / 2
        if current and mid - centre > line * 0.6:
            rows.append(current)
            current = []
        current.append(item)
        centre = sum((i[1] + i[3]) / 2 for i in current) / len(current)
    rows.append(current)
    out = []
    for row in rows:
        row.sort(key=lambda i: i[0])
        text, last_x1 = "", None
        for x0, _y0, x1, _y1, word in row:
            if last_x1 is not None:
                text += "   " if x0 - last_x1 > line * 1.5 else " "
            text += str(word).strip()
            last_x1 = x1
        out.append(text)
    return out


def _boxes(res) -> list:
    """Each recognised text's (x0, y0, x1, y1), from whichever form the result has."""
    get = getattr(res, "get", None)
    boxes = get("rec_boxes") if get else None
    if boxes is not None and len(boxes):
        return [tuple(float(v) for v in b[:4]) for b in boxes]
    polys = get("rec_polys") if get else None
    if polys is not None and len(polys):
        out = []
        for q in polys:
            p = np.asarray(q)
            out.append((float(p[:, 0].min()), float(p[:, 1].min()),
                        float(p[:, 0].max()), float(p[:, 1].max())))
        return out
    return []


def _ocr_image(img: Image.Image) -> dict:
    started = time.perf_counter()
    img = img.convert("RGB")
    original = img.size
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    with lock:
        result = ocr.predict(_to_bgr(img))
    texts, scores, positioned = [], [], []
    for res in result:
        texts.extend(res["rec_texts"])
        scores.extend(float(s) for s in res["rec_scores"])
        boxes = _boxes(res)
        if len(boxes) == len(res["rec_texts"]):
            positioned.extend((*box, text) for box, text in zip(boxes, res["rec_texts"]))
    return {
        "text": "\n".join(texts),
        # Grouped by position: what OMS reads statements from. Falls back to
        # the plain lines if this PaddleOCR gave no boxes.
        "rows": _rows(positioned) if positioned else list(texts),
        # Mean recognition confidence, 0..1: a low one says "check this by eye".
        "confidence": round(sum(scores) / len(scores), 3) if scores else None,
        "size": f"{original[0]}x{original[1]}" + (
            f" -> {img.size[0]}x{img.size[1]}" if img.size != original else ""),
        "seconds": round(time.perf_counter() - started, 1),
    }


def _image_coverage(page) -> float:
    """Share of the page's area covered by images (overlaps counted twice)."""
    area = page.rect.width * page.rect.height
    if not area:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        box = pymupdf.Rect(info["bbox"]) & page.rect
        if not box.is_empty:
            covered += box.width * box.height
    return min(covered / area, 1.0)


@app.exception_handler(Exception)
async def _unexpected(request, exc):
    # Logged with the traceback (logs\ocr.log), and the reason sent back
    # rather than a bare "Internal Server Error".
    log.exception("OCR failed")
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


@app.get("/health")
def health():
    return {"status": "ok", "models": MODEL_SET, "onednn": ONEDNN, "paddle": paddle.__version__,
            "max_side": MAX_SIDE, "threads": THREADS, "batch": BATCH}


# `def`, not `async def`: OCR is slow CPU work, so FastAPI runs it in a worker
# thread instead of blocking every other request while it runs.
@app.post("/ocr")
def run_ocr(file: UploadFile = File(...)):
    data = file.file.read()
    if not data:
        raise HTTPException(400, "The file is empty.")
    name = (file.filename or "").lower()

    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        pages = []
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception:
            raise HTTPException(415, "The PDF could not be opened.")
        with doc:
            if doc.page_count > MAX_PAGES:
                raise HTTPException(
                    413, f"The PDF has {doc.page_count} pages; the limit is {MAX_PAGES}.")
            for page in doc:
                text = page.get_text().strip()
                if len(text) >= MIN_PDF_TEXT and _image_coverage(page) < IMAGE_COVERAGE_FOR_OCR:
                    pages.append({"page": page.number + 1, "source": "pdf-text",
                                  "text": text, "confidence": None})
                    continue
                pix = page.get_pixmap(dpi=PDF_DPI)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                pages.append({"page": page.number + 1, "source": "ocr", **_ocr_image(img)})
        return {"pages": pages}

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise HTTPException(415, "Not an image or a PDF.")
    return {"pages": [{"page": 1, "source": "ocr", **_ocr_image(img)}]}
