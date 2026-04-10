"""OCR service for structured land-document extraction via Cloud Vision."""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict

import cv2
from google.cloud import vision
from google.oauth2 import service_account
import numpy as np
from PIL import Image, ImageOps

from app.google_credentials import resolve_google_credentials_path as resolve_service_credentials_path

logger = logging.getLogger("terratrust.ocr")

_vision_client: vision.ImageAnnotatorClient | None = None
COORDINATE_TOKEN_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d{2,8}))(?!\d)")


def resolve_google_credentials_path():
    """Resolve the Cloud Vision credentials file relative to the backend root."""
    return resolve_service_credentials_path("cloud_vision")


def _load_vision_credentials() -> service_account.Credentials:
    """Load Cloud Vision service-account credentials from the configured file."""
    credentials_path = resolve_google_credentials_path()
    return service_account.Credentials.from_service_account_file(str(credentials_path))


def _get_vision_client() -> vision.ImageAnnotatorClient:
    """Lazy-initialise the Cloud Vision client."""
    global _vision_client
    if _vision_client is None:
        credentials_path = resolve_google_credentials_path()
        _vision_client = vision.ImageAnnotatorClient(
            credentials=_load_vision_credentials()
        )
        logger.info(
            "Google Cloud Vision client initialised using credentials from %s.",
            credentials_path,
        )
    return _vision_client

# ---------------------------------------------------------------------------
# Regex patterns for field extraction (English + Marathi / Devanagari)
# ---------------------------------------------------------------------------
SURVEY_RE = re.compile(
    r"(?:Survey|Gat|Gut|सर्व्हे|गट)\s*[Nn]o\.?\s*[:\-]?\s*(\d+(?:/\d+)?)",
    re.IGNORECASE,
)

OWNER_RE = re.compile(
    r"(?:Name of Owner|Owner|मालकाचे नाव|धारकाचे नाव)\s*[:\-]?\s*"
    r"([A-Z][a-zA-Z\s]+|[\u0900-\u097F\s]+)",
    re.IGNORECASE,
)

VILLAGE_RE = re.compile(
    r"(?:Village|गाव|ग्राम)\s*[:\-]?\s*([A-Za-z\s]+|[\u0900-\u097F\s]+)",
    re.IGNORECASE,
)

TALUKA_RE = re.compile(
    r"(?:Taluka|तालुका|Tehsil|तहसील)\s*[:\-]?\s*([A-Za-z\s]+|[\u0900-\u097F\s]+)",
    re.IGNORECASE,
)

DISTRICT_RE = re.compile(
    r"(?:District|जिल्हा|Dist)\s*[:\-]?\s*([A-Za-z\s]+|[\u0900-\u097F\s]+)",
    re.IGNORECASE,
)


def preprocess_document_image(image_bytes: bytes) -> bytes:
    """Pre-process a raw document image for better Cloud Vision accuracy."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            upright_image = ImageOps.exif_transpose(image).convert("RGB")
    except Exception as exc:
        raise ValueError("Could not decode the uploaded image.") from exc

    bgr_image = cv2.cvtColor(np.array(upright_image), cv2.COLOR_RGB2BGR)
    grey = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
    thresholded = cv2.adaptiveThreshold(
        grey,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        15,
    )
    denoised = cv2.fastNlMeansDenoising(thresholded, h=10)

    if min(denoised.shape[:2]) < 1200:
        denoised = cv2.resize(
            denoised,
            None,
            fx=1.5,
            fy=1.5,
            interpolation=cv2.INTER_CUBIC,
        )

    success, encoded = cv2.imencode(".png", denoised)
    if not success:
        raise ValueError("Could not prepare the uploaded image for OCR.")
    return encoded.tobytes()


def _run_document_ocr(image_bytes: bytes) -> str:
    """Run Cloud Vision document OCR and return the extracted full text."""
    response = _get_vision_client().document_text_detection(
        image=vision.Image(content=image_bytes)
    )
    if response.error.message:
        raise RuntimeError(f"Cloud Vision OCR failed: {response.error.message}")
    return (response.full_text_annotation.text or "").strip()


def extract_text_annotations(image_bytes: bytes) -> list[Dict[str, Any]]:
    """Return OCR text annotations with bounding-box centres for map georeferencing."""
    response = _get_vision_client().document_text_detection(
        image=vision.Image(content=image_bytes)
    )
    if response.error.message:
        raise RuntimeError(f"Cloud Vision OCR failed: {response.error.message}")

    annotations: list[Dict[str, Any]] = []
    for annotation in response.text_annotations[1:]:
        text = (annotation.description or "").strip()
        if not text:
            continue

        vertices = annotation.bounding_poly.vertices or []
        if not vertices:
            continue

        x_values = [float(vertex.x or 0) for vertex in vertices]
        y_values = [float(vertex.y or 0) for vertex in vertices]
        annotations.append(
            {
                "text": text,
                "center_x": sum(x_values) / len(x_values),
                "center_y": sum(y_values) / len(y_values),
            }
        )

    return annotations


def _extract_fields_from_text(full_text: str) -> Dict[str, Any]:
    """Extract structured 7/12 fields from OCR text."""
    fields: Dict[str, Any] = {
        "survey_number": None,
        "owner_name": None,
        "village": None,
        "taluka": None,
        "district": None,
        "state": "Maharashtra",
    }

    match = SURVEY_RE.search(full_text)
    if match:
        fields["survey_number"] = match.group(1).strip()

    match = OWNER_RE.search(full_text)
    if match:
        fields["owner_name"] = " ".join(match.group(1).split())

    match = VILLAGE_RE.search(full_text)
    if match:
        fields["village"] = " ".join(match.group(1).split())

    match = TALUKA_RE.search(full_text)
    if match:
        fields["taluka"] = " ".join(match.group(1).split())

    match = DISTRICT_RE.search(full_text)
    if match:
        fields["district"] = " ".join(match.group(1).split())

    filled_fields = sum(
        1 for key in ("survey_number", "owner_name", "village", "taluka", "district")
        if fields.get(key)
    )
    fields["extraction_confidence"] = round(filled_fields / 5, 2)
    fields["filled_fields"] = filled_fields
    return fields


def extract_fields_from_document(image_bytes: bytes) -> Dict[str, Any]:
    """Run Cloud Vision OCR on a land document and extract structured fields."""
    processed_bytes = preprocess_document_image(image_bytes)

    processed_text = _run_document_ocr(processed_bytes)
    processed_fields = _extract_fields_from_text(processed_text)

    if processed_fields["survey_number"] and processed_fields["owner_name"]:
        best_fields = processed_fields
    else:
        original_text = _run_document_ocr(image_bytes)
        original_fields = _extract_fields_from_text(original_text)
        best_fields = max(
            (processed_fields, original_fields),
            key=lambda item: (
                int(bool(item.get("survey_number"))) + int(bool(item.get("owner_name"))),
                item.get("filled_fields", 0),
            ),
        )

    if not best_fields["survey_number"] or not best_fields["owner_name"]:
        raise ValueError(
            "Could not extract required fields from the document. "
            f"survey_number={'found' if best_fields['survey_number'] else 'missing'}, "
            f"owner_name={'found' if best_fields['owner_name'] else 'missing'}."
        )

    best_fields.pop("filled_fields", None)
    return best_fields
