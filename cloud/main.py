from __future__ import annotations

import hmac
import logging
import os
import time
import uuid

from flask import Request, jsonify
from google.api_core.exceptions import GoogleAPICallError
from googleapiclient.errors import HttpError

from app.main import (
    EmptyOcrError,
    InvalidDocumentError,
    MAX_IMAGE_BYTES,
    UpstreamServiceError,
    _processor,
)


LOGGER = logging.getLogger("shopping_board_function")


def _error(message: str, status: int):
    return jsonify({"detail": message}), status


def capture_function(request: Request):
    """Cloud Functions fallback for the currently affected Cloud Run URL routing."""
    if request.method == "GET" and request.path.rstrip("/").endswith("healthz"):
        return jsonify({"status": "ok"})
    if request.method != "POST":
        return _error("Not found", 404)

    request_id = str(uuid.uuid4())
    started = time.monotonic()
    configured_token = os.getenv("DEVICE_TOKEN", "")
    allowed_device = os.getenv("ALLOWED_DEVICE_ID", "shopping-board-camera")
    device_token = request.headers.get("X-Device-Token")
    device_id = request.headers.get("X-Device-Id")

    if (
        not configured_token
        or not device_token
        or not hmac.compare_digest(device_token, configured_token)
        or device_id != allowed_device
    ):
        return _error("Invalid device credentials", 401)

    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip()
    if content_type != "image/jpeg":
        return _error("Expected image/jpeg", 415)

    content_length = request.content_length
    if content_length is not None and (content_length < 0 or content_length > MAX_IMAGE_BYTES):
        return _error("Image is too large", 413)
    image_bytes = request.get_data(cache=False)
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        return _error("Image is empty or too large", 413)

    try:
        status, items = _processor().process(image_bytes)
    except EmptyOcrError as error:
        return _error(str(error), 422)
    except InvalidDocumentError as error:
        return _error(str(error), 500)
    except (GoogleAPICallError, HttpError, UpstreamServiceError):
        LOGGER.exception("Google API operation failed request_id=%s", request_id)
        return _error("Google API operation failed", 502)

    LOGGER.info(
        "capture request_id=%s device_id=%s bytes=%d items=%d status=%s latency_ms=%d",
        request_id,
        device_id,
        len(image_bytes),
        len(items),
        status,
        round((time.monotonic() - started) * 1000),
    )
    return jsonify({"status": status, "item_count": len(items), "request_id": request_id})
