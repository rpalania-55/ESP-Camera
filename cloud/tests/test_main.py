from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import (
    EmptyOcrError,
    app,
    build_update_requests,
    clean_ocr_text,
    parse_document_state,
    utf16_length,
)


class FakeProcessor:
    def __init__(self, result=("updated", ["Milk", "Eggs"]), error=None):
        self.result = result
        self.error = error
        self.received = None

    def process(self, image_bytes: bytes):
        self.received = image_bytes
        if self.error:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def configured_app(monkeypatch):
    monkeypatch.setenv("DEVICE_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_DEVICE_ID", "camera-1")
    app.state.processor = FakeProcessor()
    yield
    if hasattr(app.state, "processor"):
        del app.state.processor


def headers(**updates):
    values = {
        "Content-Type": "image/jpeg",
        "X-Device-ID": "camera-1",
        "X-Device-Token": "test-token",
    }
    values.update(updates)
    return values


def test_clean_ocr_text_preserves_order_and_deduplicates():
    assert clean_ocr_text(" • Milk  \n☐ Eggs\nMILK\n\nBread  rolls ") == [
        "Milk",
        "Eggs",
        "Bread rolls",
    ]


def test_clean_ocr_text_rejects_empty():
    with pytest.raises(EmptyOcrError):
        clean_ocr_text(" \n • \n")


def test_utf16_length_handles_non_bmp_characters():
    assert utf16_length("A😀B") == 4


def test_update_requests_are_tab_aware_and_bullet_only_items():
    requests = build_update_requests(20, ["Milk", "Eggs"], "t.0")
    assert requests[0]["deleteContentRange"]["range"] == {
        "startIndex": 1,
        "endIndex": 19,
        "tabId": "t.0",
    }
    heading_end = 1 + utf16_length("Shopping List\n")
    assert requests[-1]["createParagraphBullets"]["range"]["startIndex"] == heading_end


def test_update_requests_do_not_delete_a_blank_document():
    requests = build_update_requests(1, ["Milk"], None)
    assert "deleteContentRange" not in requests[0]


def test_parse_document_rejects_multiple_tabs():
    with pytest.raises(Exception, match="exactly one tab"):
        parse_document_state({"tabs": [{}, {}]})


def test_capture_requires_credentials():
    response = TestClient(app).post(
        "/v1/captures", content=b"jpeg", headers={"Content-Type": "image/jpeg"}
    )
    assert response.status_code == 401


def test_capture_validates_content_type():
    response = TestClient(app).post(
        "/v1/captures", content=b"jpeg", headers=headers(**{"Content-Type": "image/png"})
    )
    assert response.status_code == 415


def test_capture_rejects_oversized_declared_body():
    response = TestClient(app).post(
        "/v1/captures",
        content=b"jpeg",
        headers=headers(**{"Content-Length": str(6 * 1024 * 1024)}),
    )
    assert response.status_code == 413


def test_capture_processes_jpeg_without_logging_content():
    processor = app.state.processor
    response = TestClient(app).post(
        "/v1/captures", content=b"jpeg-bytes", headers=headers()
    )
    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert response.json()["item_count"] == 2
    assert processor.received == b"jpeg-bytes"


def test_empty_ocr_does_not_report_success():
    app.state.processor = FakeProcessor(error=EmptyOcrError("No usable text was detected"))
    response = TestClient(app).post(
        "/v1/captures", content=b"jpeg", headers=headers()
    )
    assert response.status_code == 422
