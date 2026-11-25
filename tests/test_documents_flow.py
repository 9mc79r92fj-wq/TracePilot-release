"""文件分析链路测试：上传→解析→内容注入 Agent→Trace 引用证据（不调用付费 API）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.services import document_store  # noqa: E402
from backend.app.services.document_store import build_document_context  # noqa: E402
from backend.app.services.trace_store import DEFAULT_TRACES_DIR, load_trace  # noqa: E402

client = TestClient(app)


@pytest.fixture()
def doc_root(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    root.mkdir()
    monkeypatch.setattr(document_store, "ROOT", root)
    return root


def _upload(filename: str, data: bytes, content_type: str = "text/plain"):
    return client.post(
        "/api/documents",
        files={"file": (filename, data, content_type)},
    )


def test_txt_upload_parse_preview_and_delete(doc_root):
    text = "TracePilot 文件引用测试：Agent 必须读取真实正文，而不是只拿到文件名。"
    r = _upload("reference.txt", text.encode("utf-8"))
    assert r.status_code == 200, r.text
    doc = r.json()["document"]
    assert len(doc["id"]) == 32
    assert doc["filename"] == "reference.txt"
    assert doc["format"] == ".txt"

    detail = client.get(f"/api/documents/{doc['id']}")
    assert detail.status_code == 200
    assert text in detail.json()["document"]["content"]

    listing = client.get("/api/documents").json()["documents"]
    assert doc["id"] in {item["id"] for item in listing}

    assert client.delete(f"/api/documents/{doc['id']}").status_code == 200
    assert client.get(f"/api/documents/{doc['id']}").status_code == 404


def test_json_csv_docx_xlsx_are_parsed_to_text(doc_root):
    import io

    # JSON
    r = _upload("data.json", '{"marker": "JSON-MARK", "count": 3}'.encode("utf-8"), "application/json")
    assert r.status_code == 200, r.text
    assert "JSON-MARK" in client.get(f"/api/documents/{r.json()['document']['id']}").json()["document"]["content"]

    # CSV
    r = _upload("data.csv", "id,status\n1,ok\n2,fail\n".encode("utf-8"), "text/csv")
    assert r.status_code == 200, r.text
    csv_content = client.get(f"/api/documents/{r.json()['document']['id']}").json()["document"]["content"]
    assert "id | status" in csv_content and "2 | fail" in csv_content

    # DOCX
    from docx import Document

    doc = Document()
    doc.add_paragraph("DOCX-MARKER: 文档解析后应进入 Agent 上下文")
    buf = io.BytesIO()
    doc.save(buf)
    r = _upload("report.docx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert r.status_code == 200, r.text
    assert "DOCX-MARKER" in client.get(f"/api/documents/{r.json()['document']['id']}").json()["document"]["content"]

    # XLSX
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["request_id", "status"])
    ws.append(["XLSX-REQ-1", "fail"])
    buf = io.BytesIO()
    wb.save(buf)
    r = _upload("requests.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert r.status_code == 200, r.text
    xlsx_content = client.get(f"/api/documents/{r.json()['document']['id']}").json()["document"]["content"]
    assert "XLSX-REQ-1" in xlsx_content and "request_id" in xlsx_content


def test_pdf_without_extractable_text_fails_explicitly(doc_root):
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    r = _upload("blank.pdf", buf.getvalue(), "application/pdf")
    assert r.status_code == 422
    assert "PDF" in r.json()["detail"]


def test_parse_failure_is_explicit(doc_root):
    bad_json = _upload("broken.json", b"{not-json")
    assert bad_json.status_code == 422
    assert "文件解析失败" in bad_json.json()["detail"]

    unsupported = _upload("virus.exe", b"MZ")
    assert unsupported.status_code == 400
    assert "不支持的文件格式" in unsupported.json()["detail"]


def test_document_context_injects_content_and_truncation_evidence():
    content = "甲" * 5000
    docs = [
        {
            "id": "a" * 32,
            "filename": "big.txt",
            "format": ".txt",
            "content": content,
        }
    ]
    context_task, provenance = build_document_context(
        "请统计文档词数", docs, max_chars=100, per_file_chars=60
    )
    assert "请统计文档词数" in context_task
    assert content[:60] in context_task
    assert "[TRUNCATED" in context_task
    assert len(provenance) == 1
    item = provenance[0]
    assert item["id"] == "a" * 32
    assert item["filename"] == "big.txt"
    assert item["source"] == "local_upload"
    assert item["content_chars"] == 5000
    assert item["injected_chars"] == 60
    assert item["truncated"] is True
    assert item["content_preview"].startswith("甲")
    assert len(item["content_sha256_16"]) == 16


def test_agent_run_receives_document_content_and_trace_records_provenance(doc_root):
    marker = "DOC-CONTENT-MARKER-7f3c"
    content = f"参考文档正文包含唯一标记 {marker}，Agent 必须读取该正文。"
    uploaded = _upload("analysis.txt", content.encode("utf-8"))
    assert uploaded.status_code == 200, uploaded.text
    doc_id = uploaded.json()["document"]["id"]

    r = client.post(
        "/api/agent/runs",
        json={
            "task": "请读取附件文档并说明其中一个唯一标记。",
            "document_ids": [doc_id],
            "mode": "mock",
        },
    )
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    # 任务文本确实包含文件正文（不是只有文件名）
    assert marker in run["task"]
    assert "reference_document" in run["task"].lower() or "REFERENCE_DOCUMENT" in run["task"]

    trace = load_trace(run["trace_id"])
    docs_meta = (trace.steps[0].metadata or {}).get("referenced_documents")
    assert docs_meta and docs_meta[0]["id"] == doc_id
    assert docs_meta[0]["filename"] == "analysis.txt"
    assert docs_meta[0]["source"] == "local_upload"
    assert docs_meta[0]["content_chars"] == len(content)
    assert marker in docs_meta[0]["content_preview"]

    # 清理测试写入的主目录 Trace，避免污染真实数据
    if run.get("trace_path"):
        Path(run["trace_path"]).unlink(missing_ok=True)
    else:
        (DEFAULT_TRACES_DIR / f"{run['trace_id']}.json").unlink(missing_ok=True)


def test_document_reference_count_limited_to_config(monkeypatch):
    # 超出配置的引用数量必须在请求校验阶段拒绝（默认本地 5 个）
    ids = ["0" * 32] * 6
    r = client.post(
        "/api/agent/runs",
        json={"task": "测试引用上限", "document_ids": ids, "mode": "mock"},
    )
    assert r.status_code == 422
    detail = json.dumps(r.json(), ensure_ascii=False)
    assert "document_ids" in detail
