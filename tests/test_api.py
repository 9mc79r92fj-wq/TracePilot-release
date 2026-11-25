"""FastAPI 后端测试（pytest，第 24 步）。使用 TestClient 与已有 Trace 数据。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402

client = TestClient(app)


def _get_pair():
    data = client.get("/api/traces").json()
    normal = fault = None
    for t in data["traces"]:
        if t["fault_types"] and fault is None:
            fault = t
        elif not t["fault_types"] and normal is None:
            normal = t
    assert normal is not None and fault is not None, "data/traces 中缺少正常/故障 Trace"
    return normal, fault


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["trace_count"] >= 2


def test_trace_list():
    r = client.get("/api/traces")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 2
    assert set(data["traces"][0].keys()) >= {"trace_id", "status", "step_count", "fault_types"}


def test_trace_detail():
    normal, _ = _get_pair()
    r = client.get(f"/api/traces/{normal['trace_id']}")
    assert r.status_code == 200
    assert r.json()["trace"]["trace_id"] == normal["trace_id"]


def test_trace_dag():
    normal, _ = _get_pair()
    r = client.get(f"/api/traces/{normal['trace_id']}/dag")
    assert r.status_code == 200
    dag = r.json()["dag"]
    assert set(dag.keys()) >= {"nodes", "edges", "topological_order", "root_nodes", "leaf_nodes"}


def test_trace_propagation():
    normal, fault = _get_pair()
    r = client.get(
        f"/api/traces/{fault['trace_id']}/propagation",
        params={"baseline_trace_id": normal["trace_id"]},
    )
    assert r.status_code == 200
    assert "summary" in r.json()["propagation"]


def test_trace_compare():
    normal, fault = _get_pair()
    r = client.post(
        "/api/traces/compare",
        json={"reference_trace_id": normal["trace_id"], "target_trace_id": fault["trace_id"]},
    )
    assert r.status_code == 200
    assert "comparison" in r.json()
    assert r.json()["comparison"]["differences"]["fault_types"]["tgt"] == ["tool_timeout"]


def test_invalid_trace_id():
    # 含 ".." 的 trace_id 被白名单拒绝 -> 400
    r = client.get("/api/traces/bad..name")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_input"
    # 编码路径穿越被服务器归一化拦截（4xx，未访问任何文件）
    r2 = client.get("/api/traces/..%2Fetc")
    assert r2.status_code in (400, 404)


def test_nonexistent_trace():
    r = client.get("/api/traces/trace-00000000000000000000000000000000")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_compare_invalid_body():
    r = client.post("/api/traces/compare", json={"reference_trace_id": "x"})
    assert r.status_code == 422  # 缺少 target_trace_id，Pydantic 校验拒绝
