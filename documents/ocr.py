import subprocess
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

def _find_tesseract():
    """Locate the tesseract executable on Windows or assume it's on PATH."""
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path
    return "tesseract"

def ocr_image(image_path: str) -> str:
    """Run Tesseract OCR on a pre‑processed PNG file and return plain text."""
    try:
        img_path = Path(image_path)
        out_txt = img_path.with_suffix('.txt')
        tesseract_bin = _find_tesseract()
        subprocess.run(
            [tesseract_bin, str(img_path), str(out_txt.with_suffix('')), "-l", "eng"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        text = out_txt.read_text(encoding="utf-8")
        logger.info(f"OCR succeeded on {image_path}")
        return text
    except Exception as exc:
        logger.error(f"OCR failed for {image_path}: {exc}")
        return ""

def ocr_image_nep_eng(image_path: str) -> str:
    """Run PaddleOCR on an image file for Nepali/Devanagari and English and return plain text."""
    try:
        from paddleocr import PaddleOCR
        # Initialize PaddleOCR with Nepali support
        ocr = PaddleOCR(use_angle_cls=True, lang='ne')
        result = ocr.ocr(image_path)
        
        text = ""
        if result and result[0]:
            for line in result[0]:
                text += line[1][0] + "\n"
                
        logger.info(f"Nepali/English PaddleOCR succeeded on {image_path}")
        return text
    except Exception as exc:
        logger.error(f"Nepali/English PaddleOCR failed for {image_path}: {exc}")
        return ""

import fitz
import docx

def extract_text_multi_format(file_path: str) -> str:
    """Extract text from PDF, DOCX, or Image (fallback to Tesseract)."""
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext == '.pdf':
        try:
            doc = fitz.open(file_path)
            text = ""
            for page in doc:
                page_text = page.get_text().strip()
                if page_text:
                    text += page_text + "\n"
                else:
                    # If no embedded text, fallback to OCR on this page
                    pix = page.get_pixmap(dpi=150)
                    temp_img_path = f"{file_path}_page_{page.number}.png"
                    pix.save(temp_img_path)
                    ocr_text = ocr_image_nep_eng(temp_img_path)
                    text += ocr_text + "\n"
                    if os.path.exists(temp_img_path):
                        os.remove(temp_img_path)
            return text
        except Exception as e:
            logger.error(f"Failed to process PDF {file_path}: {e}")
            return ""
            
    elif ext in ['.doc', '.docx']:
        try:
            doc = docx.Document(file_path)
            return "\n".join([para.text for para in doc.paragraphs])
        except Exception as e:
            logger.error(f"Failed to process DOCX {file_path}: {e}")
            return ""
            
    else:
        # Fallback to image OCR
        return ocr_image_nep_eng(file_path)
