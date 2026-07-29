#!/usr/bin/env python3
"""Extract email metrics from a Mimecast assessment PDF.

This report is rendered as images in the PDF, so direct text extraction fails.
The script renders pages to images and uses OCR to read the reporting values.

Required packages:
    pip install PyMuPDF pillow pytesseract

Also install the Tesseract binary and ensure it is on PATH.
  Windows: https://github.com/tesseract-ocr/tesseract
  Linux: sudo apt install tesseract-ocr
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

try:
    import fitz
except ImportError as exc:
    raise SystemExit("Missing dependency: pip install PyMuPDF") from exc

try:
    from PIL import Image
except ImportError as exc:
    raise SystemExit("Missing dependency: pip install pillow") from exc

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    import easyocr
except ImportError:
    easyocr = None

DATE_PATTERN = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\b",
    flags=re.IGNORECASE,
)

NUMBER_PATTERN = re.compile(r"[\d,]+")

COMMON_WINDOWS_TESSERACT_PATHS = [
    r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
    r"C:\\Program Files (x86)\\Tesseract-OCR\\tesseract.exe",
    r"C:\\Tesseract-OCR\\tesseract.exe",
]


def find_tesseract_cmd() -> Optional[str]:
    if pytesseract is None:
        return None
    configured = getattr(pytesseract.pytesseract, "tesseract_cmd", None)
    if configured and Path(configured).exists():
        return configured
    for candidate in COMMON_WINDOWS_TESSERACT_PATHS:
        if Path(candidate).exists():
            return candidate
    return shutil.which("tesseract")


def render_page(pdf_path: Path, page_number: int, dpi: int = 250) -> Image.Image:
    doc = fitz.open(pdf_path)
    if page_number < 0 or page_number >= len(doc):
        raise IndexError(f"Page number {page_number + 1} out of range")
    page = doc[page_number]
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def ocr_image(
    image: Image.Image,
    tesseract_cmd: Optional[str] = None,
    allow_easyocr: bool = False,
) -> str:
    grayscale = image.convert("L")
    if pytesseract is not None:
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        return pytesseract.image_to_string(grayscale, lang="eng", config="--psm 6")

    if allow_easyocr:
        if easyocr is None:
            raise RuntimeError(
                "EasyOCR was requested but is not installed. Install easyocr manually."
            )
        reader = easyocr.Reader(["en"], gpu=False)
        result = reader.readtext(grayscale, detail=0, paragraph=True)
        return "\n".join(result)

    raise RuntimeError(
        "No OCR backend available. Install pytesseract and the Tesseract binary, "
        "or rerun with --use-easyocr after installing easyocr and its models."
    )


def normalize_text(raw: str) -> str:
    text = raw.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text


def parse_int(value: str) -> int:
    cleaned = re.sub(r"[^0-9]", "", value)
    return int(cleaned) if cleaned else 0


def select_best_number(matches: List[str]) -> Optional[int]:
    numbers = [parse_int(value) for value in matches if parse_int(value) != 0]
    if not numbers:
        return None
    large_numbers = [num for num in numbers if num > 100]
    if large_numbers:
        return max(large_numbers)
    return numbers[-1]


def find_number_before(lines: List[str], index: int) -> Optional[int]:
    for j in range(index - 1, -1, -1):
        matches = NUMBER_PATTERN.findall(lines[j])
        if matches:
            return select_best_number(matches)
    return None


def find_number_after(lines: List[str], index: int) -> Optional[int]:
    for j in range(index + 1, len(lines)):
        matches = NUMBER_PATTERN.findall(lines[j])
        if matches:
            return select_best_number(matches)
    return None


def parse_metrics_from_text(text: str) -> Dict[str, Optional[int]]:
    metrics: Dict[str, Optional[int]] = {
        "total_inbound": None,
        "total_outbound": None,
        "internal": None,
        "total_rejected": None,
        "outbound_rejected": None,
        "inbound_rejected": None,
    }
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        lower = line.lower()
        if "total inbound messages" in lower:
            metrics["total_inbound"] = find_number_before(lines, index) or parse_int(line)
        elif "total outbound messages" in lower:
            metrics["total_outbound"] = find_number_before(lines, index) or parse_int(line)
        elif "inbound rejected" in lower:
            metrics["inbound_rejected"] = find_number_before(lines, index) or parse_int(line)
        elif "outbound rejected" in lower:
            metrics["outbound_rejected"] = find_number_before(lines, index) or parse_int(line)
        elif lower == "internal" or lower.startswith("internal "):
            if metrics["internal"] is None:
                metrics["internal"] = find_number_before(lines, index) or parse_int(line)
        elif "rejected" in lower and metrics["total_rejected"] is None:
            # Capture any standalone rejected count if not already found.
            if "inbound rejected" not in lower and "outbound rejected" not in lower:
                metrics["total_rejected"] = find_number_before(lines, index) or parse_int(line)
        elif "inbound" in lower and metrics["total_inbound"] is None:
            # Capture summary inbound count in case the label appears on a different line.
            if any(keyword in lower for keyword in ["inbound messages", "messages processed"]):
                metrics["total_inbound"] = find_number_before(lines, index) or parse_int(line)
        elif "outbound" in lower and metrics["total_outbound"] is None:
            if any(keyword in lower for keyword in ["outbound messages", "messages delivered"]):
                metrics["total_outbound"] = find_number_before(lines, index) or parse_int(line)

    # fallback patterns for text that may appear on the same line
    if metrics["total_inbound"] is None:
        for match in re.finditer(r"total\s*inbound\s*messages[:\s]*([\d,]+)", text, flags=re.I):
            metrics["total_inbound"] = parse_int(match.group(1))
            break
    if metrics["total_outbound"] is None:
        for match in re.finditer(r"total\s*outbound\s*messages[:\s]*([\d,]+)", text, flags=re.I):
            metrics["total_outbound"] = parse_int(match.group(1))
            break
    if metrics["inbound_rejected"] is None:
        for match in re.finditer(r"inbound\s*rejected[:\s]*([\d,]+)", text, flags=re.I):
            metrics["inbound_rejected"] = parse_int(match.group(1))
            break
    if metrics["outbound_rejected"] is None:
        for match in re.finditer(r"outbound\s*rejected[:\s]*([\d,]+)", text, flags=re.I):
            metrics["outbound_rejected"] = parse_int(match.group(1))
            break

    if metrics["total_rejected"] is None:
        if metrics["inbound_rejected"] is not None:
            metrics["total_rejected"] = metrics["inbound_rejected"]
        elif metrics["outbound_rejected"] is not None:
            metrics["total_rejected"] = metrics["outbound_rejected"]

    return metrics


def parse_daily_counts(text: str) -> Dict[str, Dict[str, Optional[int]]]:
    inbound: Dict[str, Optional[int]] = {}
    outbound: Dict[str, Optional[int]] = {}
    total: Dict[str, Optional[int]] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        date_match = DATE_PATTERN.search(line)
        if not date_match:
            continue
        date_str = date_match.group(0)
        numbers = NUMBER_PATTERN.findall(line)
        if len(numbers) >= 2:
            # If the line contains more than one number, we may have separate counts.
            inbound[date_str] = parse_int(numbers[0])
            outbound[date_str] = parse_int(numbers[-1])
            continue
        count = None
        if numbers:
            count = parse_int(numbers[-1])
        if count is None:
            count = find_number_after(lines, index)
        total[date_str] = count
    return {"inbound": inbound, "outbound": outbound, "total": total}


def extract_report(
    pdf_path: Path,
    pages: Optional[Sequence[int]] = None,
    allow_easyocr: bool = False,
) -> Dict[str, object]:
    if pages is None:
        pages = list(range(0, 8))
    tesseract_cmd = find_tesseract_cmd()
    if pytesseract is None and easyocr is None:
        raise RuntimeError(
            "Install OCR dependencies: pip install pytesseract pillow PyMuPDF "
            "or easyocr."
        )
    if pytesseract is not None and tesseract_cmd is None:
        print(
            "Warning: pytesseract is installed but no Tesseract binary was found. "
            "Install Tesseract and add it to PATH, or use --use-easyocr if you have easyocr models.",
            file=sys.stderr,
        )

    raw_texts: List[str] = []
    for page_num in pages:
        print(f"Rendering page {page_num + 1}...", file=sys.stderr)
        image = render_page(pdf_path, page_num)
        try:
            page_text = ocr_image(
                image,
                tesseract_cmd=tesseract_cmd,
                allow_easyocr=allow_easyocr,
            )
        except RuntimeError as exc:
            raise RuntimeError(
                "OCR failed. Ensure Tesseract is installed or rerun with --use-easyocr "
                "after installing easyocr and its models."
            ) from exc
        normalized = normalize_text(page_text)
        raw_texts.append(normalized)

    full_text = "\n\n".join(raw_texts)
    metrics = parse_metrics_from_text(full_text)
    daily = parse_daily_counts(full_text)
    return {
        "pdf_path": str(pdf_path),
        "metrics": metrics,
        "daily_counts": daily,
        "ocr_text": full_text,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Mimecast assessment email metrics from an image-based PDF."
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        default="assessment.pdf",
        help="Assessment PDF file path (default: assessment.pdf)",
    )
    parser.add_argument(
        "--pages",
        nargs="*",
        type=int,
        help="Page numbers to OCR (1-based). If omitted, parses the first 8 pages.",
    )
    parser.add_argument(
        "--use-easyocr",
        action="store_true",
        help="Use EasyOCR instead of Tesseract if Tesseract is not available.",
    )
    parser.add_argument(
        "--json",
        help="Write extracted metrics to JSON file.",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"PDF file not found: {pdf_path}")

    pages = [n - 1 for n in args.pages] if args.pages else None
    report = extract_report(
        pdf_path,
        pages=pages,
        allow_easyocr=args.use_easyocr,
    )
    metrics = report["metrics"]
    daily = report["daily_counts"]

    print("Extracted metrics:")
    for key, value in metrics.items():
        print(f"  {key}: {value}")
    print("\nDaily counts (inbound/outbound) found:")
    for direction in ("inbound", "outbound", "total"):
        entries = daily.get(direction, {})
        if not entries:
            continue
        print(f"  {direction}:")
        for date, count in sorted(entries.items()):
            print(f"    {date}: {count}")

    if args.json:
        out_path = Path(args.json)
        out_path.write_text(json.dumps(report, indent=2))
        print(f"\nWritten JSON report to {out_path}")


if __name__ == "__main__":
    main()
