"""Time OCR outside the service, to tell a slow engine from a slow service.

    .venv\\Scripts\\python.exe bench.py samples\\sample.jpeg [more files...]

Loads the same pipeline as main.py (same models and settings), then OCRs each
file TWICE: the first run includes one-off warm-up for that image size, the
second is what a steady service would take.

Settings come from the same environment variables as main.py (OCR_MODELS,
OCR_BATCH, OCR_THREADS, OCR_MAX_SIDE), so a setting can be tried here before
the service uses it. BENCH_TEXT=1 also prints the text, to compare accuracy.
"""
import os
import sys
import time

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

started = time.perf_counter()
import main  # noqa: E402  (builds the pipeline)
from PIL import Image  # noqa: E402

print(f"pipeline loaded in {time.perf_counter() - started:.1f} s "
      f"(models={main.MODEL_SET}, onednn={main.ONEDNN}, threads={main.THREADS}, "
      f"batch={main.BATCH}, max_side={main.MAX_SIDE}, cpus={os.cpu_count()})")

for path in sys.argv[1:]:
    if path.lower().endswith(".pdf"):
        doc = main.pymupdf.open(path)
        pix = doc[0].get_pixmap(dpi=main.PDF_DPI)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    else:
        img = Image.open(path)
        img.load()
    for attempt in ("first", "second"):
        result = main._ocr_image(img)
        lines = result["text"].count("\n") + 1 if result["text"] else 0
        print(f"{os.path.basename(path)} [{attempt}]: {result['seconds']} s, "
              f"{result['size']}, {lines} lines, confidence {result['confidence']}")
    if os.environ.get("BENCH_TEXT") == "1":
        print("-" * 60)
        print(result["text"])
        print("-" * 60)
