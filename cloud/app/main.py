from __future__ import annotations

import hmac
import logging
import os
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import google.auth
from fastapi import FastAPI, Header, HTTPException, Request
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import vision
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from starlette.concurrency import run_in_threadpool

LOGGER = logging.getLogger("shopping_board")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "100"))
MAX_ITEM_CHARS = int(os.getenv("MAX_ITEM_CHARS", "200"))
DOCS_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/documents",
)

LEADING_MARKS = re.compile(r"^[\s\-–—•*·◦▪▫☐☑☒✓✔]+")
WHITESPACE = re.compile(r"\s+")


class EmptyOcrError(RuntimeError):
    pass


class InvalidDocumentError(RuntimeError):
    pass


class UpstreamServiceError(RuntimeError):
    pass


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def clean_ocr_text(raw_text: str) -> list[str]:
    """Convert OCR text to a conservative, ordered shopping list."""
    items: list[str] = []
    seen: set[str] = set()
    for raw_line in raw_text.splitlines():
        line = unicodedata.normalize("NFKC", raw_line)
        line = LEADING_MARKS.sub("", line)
        line = WHITESPACE.sub(" ", line).strip()
        if not line:
            continue
        if len(line) > MAX_ITEM_CHARS:
            raise EmptyOcrError("OCR produced an implausibly long list item")
        identity = line.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        items.append(line)
    if not items:
        raise EmptyOcrError("No usable text was detected")
    if len(items) > MAX_ITEMS:
        raise EmptyOcrError("OCR produced too many list items")
    return items


def _range(start: int, end: int, tab_id: str | None) -> dict[str, Any]:
    value: dict[str, Any] = {"startIndex": start, "endIndex": end}
    if tab_id:
        value["tabId"] = tab_id
    return value


def _location(index: int, tab_id: str | None) -> dict[str, Any]:
    value: dict[str, Any] = {"index": index}
    if tab_id:
        value["tabId"] = tab_id
    return value


def desired_document_text(items: list[str]) -> str:
    return "Shopping List\n" + "\n".join(items) + "\n"


def build_update_requests(
    end_index: int, items: list[str], tab_id: str | None
) -> list[dict[str, Any]]:
    text = desired_document_text(items)
    heading_end = 1 + utf16_length("Shopping List\n")
    document_end = 1 + utf16_length(text)
    requests: list[dict[str, Any]] = []
    # Preserve the document's mandatory final newline.
    if end_index - 1 > 1:
        requests.append(
            {"deleteContentRange": {"range": _range(1, end_index - 1, tab_id)}}
        )
    requests.extend(
        [
            {
                "insertText": {
                    "location": _location(1, tab_id),
                    "text": text,
                }
            },
            {
                "updateParagraphStyle": {
                    "range": _range(1, heading_end, tab_id),
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "fields": "namedStyleType",
                }
            },
            {
                "createParagraphBullets": {
                    "range": _range(heading_end, document_end, tab_id),
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            },
        ]
    )
    return requests


@dataclass(frozen=True)
class DocumentState:
    revision_id: str
    tab_id: str | None
    end_index: int
    plain_text: str


def parse_document_state(document: dict[str, Any]) -> DocumentState:
    tabs = document.get("tabs")
    tab_id: str | None = None
    if tabs:
        if len(tabs) != 1 or tabs[0].get("childTabs"):
            raise InvalidDocumentError(
                "The target must be a dedicated Google Doc with exactly one tab"
            )
        tab = tabs[0]
        tab_id = tab.get("tabProperties", {}).get("tabId")
        body = tab.get("documentTab", {}).get("body", {})
    else:
        body = document.get("body", {})

    content = body.get("content", [])
    end_index = max((part.get("endIndex", 1) for part in content), default=1)
    text_parts: list[str] = []
    for structural_element in content:
        paragraph = structural_element.get("paragraph", {})
        for element in paragraph.get("elements", []):
            text_parts.append(element.get("textRun", {}).get("content", ""))
    return DocumentState(
        revision_id=document.get("revisionId", ""),
        tab_id=tab_id,
        end_index=end_index,
        plain_text="".join(text_parts),
    )


class GooglePipeline:
    def __init__(self, document_id: str) -> None:
        credentials, _ = google.auth.default(scopes=DOCS_SCOPES)
        self._vision = vision.ImageAnnotatorClient(credentials=credentials)
        self._docs = build(
            "docs", "v1", credentials=credentials, cache_discovery=False
        )
        self._document_id = document_id

    def recognize(self, image_bytes: bytes) -> list[str]:
        image = vision.Image(content=image_bytes)
        response = self._vision.document_text_detection(image=image)
        if response.error.message:
            raise UpstreamServiceError(response.error.message)
        return clean_ocr_text(response.full_text_annotation.text)

    def _read_document(self) -> DocumentState:
        document = (
            self._docs.documents()
            .get(documentId=self._document_id, includeTabsContent=True)
            .execute()
        )
        return parse_document_state(document)

    def _write_once(self, state: DocumentState, items: list[str]) -> None:
        body: dict[str, Any] = {
            "requests": build_update_requests(state.end_index, items, state.tab_id)
        }
        if state.revision_id:
            body["writeControl"] = {"requiredRevisionId": state.revision_id}
        (
            self._docs.documents()
            .batchUpdate(documentId=self._document_id, body=body)
            .execute()
        )

    @staticmethod
    def _is_revision_conflict(error: HttpError) -> bool:
        status = getattr(error.resp, "status", None)
        message = str(error).casefold()
        return status in (400, 409) and "revision" in message

    def replace_document(self, items: list[str]) -> str:
        expected = desired_document_text(items).rstrip("\n")
        state = self._read_document()
        if state.plain_text.rstrip("\n") == expected:
            return "unchanged"

        try:
            self._write_once(state, items)
        except HttpError as error:
            if not self._is_revision_conflict(error):
                raise
            state = self._read_document()
            self._write_once(state, items)

        verified = self._read_document()
        if verified.plain_text.rstrip("\n") != expected:
            raise UpstreamServiceError("Google Doc verification failed after update")
        return "updated"

    def process(self, image_bytes: bytes) -> tuple[str, list[str]]:
        items = self.recognize(image_bytes)
        return self.replace_document(items), items


def _processor() -> GooglePipeline:
    processor = getattr(app.state, "processor", None)
    if processor is not None:
        return processor
    document_id = os.getenv("GOOGLE_DOC_ID", "").strip()
    if not document_id:
        raise RuntimeError("GOOGLE_DOC_ID is not configured")
    processor = GooglePipeline(document_id)
    app.state.processor = processor
    return processor


app = FastAPI(title="Shopping Board Capture", version="1.0.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/captures")
async def capture(
    request: Request,
    x_device_id: str | None = Header(default=None),
    x_device_token: str | None = Header(default=None),
    x_captured_at: str | None = Header(default=None),
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    started = time.monotonic()
    configured_token = os.getenv("DEVICE_TOKEN", "")
    allowed_device = os.getenv("ALLOWED_DEVICE_ID", "shopping-board-camera")

    if (
        not configured_token
        or not x_device_token
        or not hmac.compare_digest(x_device_token, configured_token)
        or x_device_id != allowed_device
    ):
        raise HTTPException(status_code=401, detail="Invalid device credentials")

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "image/jpeg":
        raise HTTPException(status_code=415, detail="Expected image/jpeg")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from error
        if declared_length < 0 or declared_length > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Image is too large")
    image_bytes = await request.body()
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image is empty or too large")

    try:
        status, items = await run_in_threadpool(_processor().process, image_bytes)
    except EmptyOcrError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except InvalidDocumentError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except (GoogleAPICallError, HttpError, UpstreamServiceError) as error:
        LOGGER.exception("Google API operation failed request_id=%s", request_id)
        raise HTTPException(status_code=502, detail="Google API operation failed") from error

    elapsed_ms = round((time.monotonic() - started) * 1000)
    LOGGER.info(
        "capture request_id=%s device_id=%s bytes=%d items=%d status=%s latency_ms=%d captured_at_present=%s",
        request_id,
        x_device_id,
        len(image_bytes),
        len(items),
        status,
        elapsed_ms,
        bool(x_captured_at),
    )
    return {"status": status, "item_count": len(items), "request_id": request_id}
