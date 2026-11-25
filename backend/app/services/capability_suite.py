"""Agent 能力套件与能力矩阵（第 55 步需求回溯修复）。

原始需求 PDF（第 3 节 20 项能力矩阵）要求 20 项 Agent 能力均有真实实现与 L2 自动化测试。
本模块补齐此前缺失/部分实现的能力，并给出**可核对的能力矩阵**（状态、证据、限制）：

- RAG：本地知识库检索（`data/knowledge/knowledge_base.json`，合成数据，无外部向量库依赖）；
- Memory：运行内 session 记忆（remember/recall），不跨进程持久化；
- Context Engineering：上下文装配（预算截断、检索文档/记忆注入、来源标注）；
- Context Review：上下文质量审查（注入模式、重复、超长/空、敏感串）；
- Skill：复合技能（SQLite 查询 + 汇总统计 + 结果说明）；
- Reflection：可选反思步骤（Agent 工具失败后触发一次；默认关闭）；
- Explainability：基于 Trace 证据的决策解释（**不保存/不还原模型思维链**）；
- HITL：审批工作流（创建/查询/批准/拒绝，含审计字段；**不实现执行中途暂停恢复**）；
- Execution Environment：工具执行超时与结构化错误（由 Agent 运行时强制）；
- Deployment：Dockerfile / docker-compose（运行时验证状态单独标注）。

诚实性：能力矩阵中的 COMPLETE 表示「本仓库有实现 + 自动化测试」；PARTIAL 表示实现受限；
UNVERIFIED 表示有代码但缺少运行验证；绝不把「只有接口/文档」标为 COMPLETE。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "PROJECT_ROOT",
    "KNOWLEDGE_BASE_PATH",
    "CAPABILITY_MATRIX",
    "MemoryStore",
    "HitlStore",
    "get_hitl_store",
    "search_knowledge",
    "review_context",
    "build_context",
    "explain_trace",
    "capability_matrix_report",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_BASE_PATH = PROJECT_ROOT / "data" / "knowledge" / "knowledge_base.json"
HITL_PATH = PROJECT_ROOT / "data" / "hitl" / "approvals.json"

# ---------------------------------------------------------------------------
# 能力矩阵（状态与证据必须与仓库真实实现/测试一致）
# ---------------------------------------------------------------------------

CAPABILITY_MATRIX: list[dict[str, Any]] = [
    {
        "id": "planning",
        "name": "规划 (Planning)",
        "status": "COMPLETE",
        "level": "L3",
        "implementation": "backend/app/agents/basic_agent.py（understand→plan→agent 图）",
        "evidence": ["tests/test_basic_agent.py", "Trace 中 planning 步骤", "DAG Analysis 页面"],
        "limitations": ["受控模拟模型生成的计划不等同真实模型规划质量"],
    },
    {
        "id": "tool_calling",
        "name": "工具调用 (Tool Calling)",
        "status": "COMPLETE",
        "level": "L3",
        "implementation": "backend/app/tools/basic_tools.py（calculator / sqlite_query / text_analyzer）",
        "evidence": ["tests/test_basic_tools.py", "Agent Runs / DAG 页面"],
        "limitations": ["仅本地白名单工具，无并行工具调用"],
    },
    {
        "id": "skill",
        "name": "技能 (Skill)",
        "status": "COMPLETE",
        "level": "L2",
        "implementation": "backend/app/tools/capability_tools.py: composite_sales_report（SQLite 查询 + 汇总技能）",
        "evidence": ["tests/test_capability_suite.py::test_skill_composite_report", "Trace step_type=skill"],
        "limitations": ["技能为代码注册的固定组合，不支持模型动态编排新技能"],
    },
    {
        "id": "rag",
        "name": "RAG（检索增强）",
        "status": "COMPLETE",
        "level": "L2",
        "implementation": "capability_tools.knowledge_search + data/knowledge/knowledge_base.json（本地关键字检索）",
        "evidence": ["tests/test_capability_suite.py::test_rag_retrieval", "Trace step_type=retrieval"],
        "limitations": ["无向量数据库/embedding；检索为确定性关键词匹配，知识库为合成数据"],
    },
    {
        "id": "memory",
        "name": "记忆 (Memory)",
        "status": "COMPLETE",
        "level": "L2",
        "implementation": "capability_suite.MemoryStore + remember_fact / recall_facts 工具（运行内 session）",
        "evidence": ["tests/test_capability_suite.py::test_memory_remember_recall", "Trace step_type=memory_read/write"],
        "limitations": ["仅进程内单次运行会话；不跨进程/跨 Trace 持久化"],
    },
    {
        "id": "context_engineering",
        "name": "上下文工程 (Context Engineering)",
        "status": "COMPLETE",
        "level": "L2",
        "implementation": "capability_suite.build_context + run_agent(extra_context=...) 注入",
        "evidence": ["tests/test_capability_suite.py::test_context_builder", "tests/test_capability_suite.py::test_agent_extra_context_injected"],
        "limitations": ["上下文预算为字符数截断；未实现学习式 prompt 优化"],
    },
    {
        "id": "reflection",
        "name": "反思 (Reflection)",
        "status": "COMPLETE",
        "level": "L2",
        "implementation": "basic_agent.run_agent(enable_reflection=True)：工具失败后触发一次反思步骤",
        "evidence": ["tests/test_capability_suite.py::test_reflection_on_tool_failure", "Trace step_type=reflection"],
        "limitations": ["默认关闭；仅失败后一次反思，不保证重试成功"],
    },
    {
        "id": "explainability",
        "name": "可解释性 (Explainability)",
        "status": "PARTIAL",
        "level": "L2",
        "implementation": "capability_suite.explain_trace + GET /api/traces/{id}/explanation",
        "evidence": ["tests/test_capability_suite.py::test_explain_trace", "Trace 详情页「决策解释」卡片"],
        "limitations": ["使用步骤/工具/错误证据生成解释，不保存或还原模型内部思维链；不是置信度"],
    },
    {
        "id": "hitl",
        "name": "人机协同 (HITL)",
        "status": "PARTIAL",
        "level": "L2",
        "implementation": "HitlStore + request_human_approval 工具 + /api/hitl/approvals",
        "evidence": ["tests/test_capability_suite.py::test_hitl_lifecycle", "tests/test_capability_suite.py::test_capability_tools_smoke", "e2e/capabilities.spec.ts HITL API"],
        "limitations": ["审批是运行外工作流：Agent 不中途暂停等待；无真实人工审批人身份体系"],
    },
    {
        "id": "evaluation",
        "name": "执行评估 (Evaluation)",
        "status": "COMPLETE",
        "level": "L3",
        "implementation": "evaluation_engine + Evaluation Center + 评测流水线",
        "evidence": ["tests/test_evaluation_engine.py", "tests/test_evaluation_pipeline.py", "e2e/flows.spec.ts"],
        "limitations": ["确定性模拟模型；语义正确性依赖 Mock/可选真实 Judge"],
    },
    {
        "id": "safety",
        "name": "安全 (Safety)",
        "status": "COMPLETE",
        "level": "L3",
        "implementation": "evaluation_engine._dim_safety + Red Team / Adversarial Lab + 安全门禁",
        "evidence": ["tests/test_evaluation_engine.py", "tests/test_red_team*.py", "e2e/redteam.spec.ts"],
        "limitations": ["安全维度为模式扫描，不构成完整安全审计；真实模型安全未验证"],
    },
    {
        "id": "error_handling",
        "name": "故障检测/错误处理 (Error Handling)",
        "status": "COMPLETE",
        "level": "L3",
        "implementation": "basic_agent 工具异常结构化回传 + fault_injector + failure_propagation",
        "evidence": ["tests/test_fault_injector.py", "tests/test_failure_propagation.py"],
        "limitations": ["恢复依赖模型重试；无自动降级策略编排"],
    },
    {
        "id": "execution_environment",
        "name": "执行环境管理",
        "status": "PARTIAL",
        "level": "L2",
        "implementation": "run_agent(tool_timeout_seconds=...) 工具调用超时 + Agent 整体超时限制",
        "evidence": ["tests/test_capability_suite.py::test_tool_timeout_enforced"],
        "limitations": ["无子进程/容器隔离；仅超时与结构化错误；未实现 shell 环境管理"],
    },
    {
        "id": "cost_monitoring",
        "name": "记账计费 (Cost Monitoring)",
        "status": "COMPLETE",
        "level": "L3",
        "implementation": "token_analytics + Cost Analytics 页面 + /api/cost/*",
        "evidence": ["tests/test_token_analytics.py", "e2e/cost-analytics.spec.ts"],
        "limitations": ["无官方可信价格时金额为 PRICE_UNKNOWN；模拟数据成本为 0"],
    },
    {
        "id": "versioning",
        "name": "版本控制 (Versioning)",
        "status": "COMPLETE",
        "level": "L2",
        "implementation": "Git 历史 + PROJECT_STATE.md + DECISIONS.md + 版本对比/回归/门禁",
        "evidence": ["git log 30+ commits", "e2e/version-comparison.spec.ts"],
        "limitations": ["评测记录部分内存态，重启后需重新运行"],
    },
    {
        "id": "automation",
        "name": "自动化测试 (Automation)",
        "status": "COMPLETE",
        "level": "L2",
        "implementation": "pytest + Playwright + GitHub Actions workflow + 本地 CI 质量门禁",
        "evidence": ["tests/（661 项）", "frontend/e2e（102 项）", ".github/workflows/agent-evaluation.yml"],
        "limitations": ["云端 CI 尚未在 GitHub 实际运行（无远程仓库）"],
    },
    {
        "id": "deployment",
        "name": "部署 (Deployment)",
        "status": "PARTIAL",
        "level": "L1",
        "implementation": "Dockerfile + docker-compose.yml + docs/DEPLOYMENT.md",
        "evidence": ["docker-compose config 文件存在", "部署文档"],
        "limitations": ["本机未执行容器构建/启动，运行状态为 NOT_VERIFIED；无云部署"],
    },
    {
        "id": "monitoring",
        "name": "性能监控 (Monitoring)",
        "status": "COMPLETE",
        "level": "L3",
        "implementation": "Overview / Cost Analytics / Pareto / 延迟与成功率指标",
        "evidence": ["e2e/cost-analytics.spec.ts", "e2e/pareto.spec.ts", "e2e/pages.spec.ts"],
        "limitations": ["监控为本地运行指标，无生产 APM/告警集成"],
    },
    {
        "id": "cost_optimization",
        "name": "成本优化 (Cost Optimization)",
        "status": "PARTIAL",
        "level": "L2",
        "implementation": "Pareto Explorer 成本-延迟-质量选择 + 成本分析；无自动调度/缓存降本",
        "evidence": ["tests/test_pareto_analysis.py", "e2e/pareto.spec.ts"],
        "limitations": ["仅提供决策支持，不自动切换模型/缓存/并发策略；无真实价格"],
    },
    {
        "id": "context_review",
        "name": "上下文审查 (Context Review)",
        "status": "COMPLETE",
        "level": "L2",
        "implementation": "capability_suite.review_context + knowledge_search 结果自动审查 + review_context 工具",
        "evidence": ["tests/test_capability_suite.py::test_context_review_blocks_injection"],
        "limitations": ["规则模式检测（注入/重复/超长/敏感串），不覆盖全部语义脏数据"],
    },
]

INNOVATION_MATRIX: list[dict[str, Any]] = [
    {
        "id": "causal_trace_algebra",
        "name": "因果轨迹代数（Causal Trace Algebra）",
        "status": "PARTIAL",
        "implementation": "counterfactual_engine（干预类型：跳过/固定输出/替换工具结果）",
        "evidence": ["tests/test_counterfactual_engine.py", "Counterfactual Lab"],
        "limitations": ["以受控操作符表示干预，未形式化完整代数系统"],
    },
    {
        "id": "minimal_intervention",
        "name": "最小充分干预搜索",
        "status": "PARTIAL",
        "implementation": "minimal_repair 生成有限候选 + CounterfactualEngine 受控验证",
        "evidence": ["tests/test_minimal_repair.py", "Repair Studio"],
        "limitations": ["启发式候选、最多单步/少量组合；非全局最优搜索"],
    },
    {
        "id": "uncertainty_gatekeeping",
        "name": "不确定性感知门禁",
        "status": "PARTIAL",
        "implementation": "证据等级（CONFIRMED/INFERRED/UNKNOWN）+ 质量门禁四态（PASS/WARN/BLOCK/INCONCLUSIVE）",
        "evidence": ["tests/test_quality_gate.py", "docs/CI_QUALITY_GATE.md"],
        "limitations": ["刻意不输出无来源概率/CI；未实现统计重采样置信区间"],
    },
    {
        "id": "causal_effect_pruning",
        "name": "因果影响剪枝",
        "status": "PARTIAL",
        "implementation": "故障传播分析限定候选节点 + 归因候选合并/去重",
        "evidence": ["tests/test_failure_propagation.py", "bad_case_attribution 候选生成"],
        "limitations": ["无基于效应量的搜索空间剪枝与成本-准确率对比实验"],
    },
    {
        "id": "fault_injection",
        "name": "故障注入与自动测试生成",
        "status": "COMPLETE",
        "implementation": "fault_injector（6 类故障）+ fault 评测集 + Red Team Generator",
        "evidence": ["tests/test_fault_injector.py", "tests/test_red_team*.py", "e2e/redteam.spec.ts"],
        "limitations": ["注入为受控模拟；真实模型故障场景未验证"],
    },
    {
        "id": "rootcause_metrics",
        "name": "根因准确率评估（Top-1/Top-3）",
        "status": "COMPLETE",
        "implementation": "rootcause_metrics + /api/attribution/metrics + Fault Attribution 页面指标卡",
        "evidence": ["tests/test_rootcause_metrics.py", "e2e/capabilities.spec.ts 根因指标卡", "Evaluation Center"],
        "limitations": ["金标准为合成故障集；真实故障分布未验证；修复成功率需显式开启验证"],
    },
    {
        "id": "propagation_visualization",
        "name": "失败传播路径可视化",
        "status": "COMPLETE",
        "implementation": "DAG 可视化 + 传播分析 + 反事实/修复对比",
        "evidence": ["tests/test_failure_propagation.py", "e2e/time-travel.spec.ts", "DAG Analysis"],
        "limitations": ["DAG 依赖不等于因果证明（界面已标注）"],
    },
]


# ---------------------------------------------------------------------------
# Memory（运行内 session）
# ---------------------------------------------------------------------------

class MemoryStore:
    """单次 Agent 运行内的短期记忆（不持久化、不跨会话）。"""

    def __init__(self, *, max_entries: int = 50) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        self._max_entries = max(1, max_entries)

    def remember(self, key: str, value: str) -> dict[str, Any]:
        key = str(key).strip()
        value = str(value).strip()
        if not key or not value:
            raise ValueError("key 与 value 不能为空")
        if len(key) > 128 or len(value) > 2000:
            raise ValueError("key 最长 128 字符、value 最长 2000 字符")
        with self._lock:
            self._entries.append(
                {
                    "key": key,
                    "value": value,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if len(self._entries) > self._max_entries:
                self._entries = self._entries[-self._max_entries :]
            return {"stored": True, "key": key, "entries": len(self._entries)}

    def recall(self, query: str = "", limit: int = 5) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 20))
        query = str(query).strip().lower()
        with self._lock:
            entries = list(self._entries)
        if not query:
            return entries[-limit:][::-1]
        terms = _terms(query)
        scored: list[tuple[int, dict[str, Any]]] = []
        for entry in entries:
            text = (entry["key"] + " " + entry["value"]).lower()
            score = sum(1 for term in terms if term in text)
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1]["created_at"]))
        return [dict(entry, score=score) for score, entry in scored[:limit]]

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)


# ---------------------------------------------------------------------------
# HITL（审批工作流，非执行中途暂停）
# ---------------------------------------------------------------------------

class HitlStore:
    """审批记录持久化（JSON 文件；状态 PENDING/APPROVED/REJECTED）。"""

    def __init__(self, path: Path = HITL_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏文件不阻塞审批
            return []
        return data if isinstance(data, list) else []

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(rows[-200:], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def create(
        self,
        *,
        action: str,
        reason: str,
        risk_level: str = "MEDIUM",
        run_id: str | None = None,
        requested_by: str = "agent",
    ) -> dict[str, Any]:
        action = str(action).strip()
        reason = str(reason).strip()
        if not action or len(action) > 500:
            raise ValueError("action 不能为空且最长 500 字符")
        if risk_level not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("risk_level 必须是 LOW / MEDIUM / HIGH")
        approval = {
            "approval_id": f"hitl-{uuid.uuid4().hex}",
            "action": action,
            "reason": reason[:1000],
            "risk_level": risk_level,
            "run_id": run_id,
            "requested_by": requested_by,
            "status": "PENDING",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "decided_at": None,
            "decided_by": None,
            "comment": None,
        }
        with self._lock:
            rows = self._load()
            rows.append(approval)
            self._save(rows)
        return approval

    def list(self, *, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._load()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return list(reversed(rows))[: max(1, limit)]

    def get(self, approval_id: str) -> dict[str, Any]:
        with self._lock:
            for row in self._load():
                if row.get("approval_id") == approval_id:
                    return row
        raise KeyError(f"审批记录不存在: {approval_id}")

    def decide(
        self,
        approval_id: str,
        *,
        decision: str,
        decided_by: str = "human",
        comment: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError("decision 必须是 APPROVED / REJECTED")
        with self._lock:
            rows = self._load()
            for row in rows:
                if row.get("approval_id") != approval_id:
                    continue
                if row.get("status") != "PENDING":
                    raise ValueError(f"审批已完成（{row.get('status')}），不能重复决策")
                row.update(
                    {
                        "status": decision,
                        "decided_at": datetime.now(timezone.utc).isoformat(),
                        "decided_by": decided_by,
                        "comment": (comment or "")[:1000] or None,
                    }
                )
                self._save(rows)
                return row
        raise KeyError(f"审批记录不存在: {approval_id}")


_hitl_store: HitlStore | None = None
_hitl_lock = threading.Lock()


def get_hitl_store() -> HitlStore:
    global _hitl_store
    if _hitl_store is None:
        with _hitl_lock:
            if _hitl_store is None:
                _hitl_store = HitlStore()
    return _hitl_store


# ---------------------------------------------------------------------------
# 知识库（RAG，合成数据）
# ---------------------------------------------------------------------------

def _load_knowledge() -> list[dict[str, Any]]:
    if not KNOWLEDGE_BASE_PATH.exists():
        return []
    try:
        data = json.loads(KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    docs = data.get("documents") if isinstance(data, dict) else None
    return docs if isinstance(docs, list) else []


def _terms(text: str) -> list[str]:
    text = str(text).lower()
    terms: list[str] = re.findall(r"[a-z0-9_]+", text)
    chinese = re.findall(r"[\u4e00-\u9fff]", text)
    # 中文用单字 + 二元组；足够做确定性关键词匹配，不引入 embedding 依赖
    terms.extend(chinese)
    terms.extend("".join(chinese[i : i + 2]) for i in range(max(0, len(chinese) - 1)))
    return sorted({t for t in terms if t})


def search_knowledge(query: str, *, top_k: int = 3) -> dict[str, Any]:
    """本地知识库检索（确定性关键词打分；结果先经上下文审查）。"""
    query = str(query).strip()
    if not query:
        raise ValueError("query 不能为空")
    top_k = max(1, min(int(top_k), 10))
    docs = _load_knowledge()
    query_terms = _terms(query)
    scored: list[tuple[int, dict[str, Any], list[str]]] = []
    for doc in docs:
        haystack = " ".join(
            [str(doc.get("title") or ""), str(doc.get("content") or "")]
        )
        doc_terms = set(_terms(haystack))
        matched = [term for term in query_terms if term in doc_terms]
        if matched:
            scored.append((len(matched), doc, matched[:8]))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("doc_id"))))
    results: list[dict[str, Any]] = []
    review_issues: list[str] = []
    for score, doc, matched in scored[:top_k]:
        review = review_context(str(doc.get("content") or ""))
        if review["blocked"]:
            review_issues.append(
                f"{doc.get('doc_id')} 被上下文审查拦截: {'; '.join(review['issues'])}"
            )
            continue
        results.append(
            {
                "doc_id": doc.get("doc_id"),
                "title": doc.get("title"),
                "content": doc.get("content"),
                "score": score,
                "matched_terms": matched,
                "data_source": doc.get("data_source", "synthetic"),
            }
        )
    return {
        "query": query,
        "result_count": len(results),
        "results": results,
        "review_issues": review_issues,
        "knowledge_base": str(KNOWLEDGE_BASE_PATH.relative_to(PROJECT_ROOT)),
        "note": "本地确定性关键词检索；知识库为合成演示数据，不代表真实业务知识",
    }


# ---------------------------------------------------------------------------
# 上下文审查 + 上下文工程
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior)\s+(instructions?|prompts?)",
    r"忽略(之前|以上|前面)(的)?(所有)?(指令|提示|要求)",
    r"system\s*prompt",
    r"你现在是.*(没有限制|不受约束)",
    r"泄露.*(api[_ ]?key|密钥|密码)",
]
_SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9]{16,}",
    r"Bearer\s+[A-Za-z0-9._-]{20,}",
    r"DEEPSEEK_API_KEY\s*=",
]


def review_context(text: str) -> dict[str, Any]:
    """审查一段待注入上下文（不调用模型）。"""
    text = str(text or "")
    issues: list[str] = []
    severity = "PASS"
    if not text.strip():
        return {
            "verdict": "WARN",
            "blocked": False,
            "issues": ["上下文为空"],
            "chars": 0,
            "duplicate_ratio": 0.0,
        }
    lowered = text.lower()
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            issues.append("检测到疑似提示注入/越权指令模式")
            severity = "BLOCK"
            break
    for pattern in _SECRET_PATTERNS:
        if re.search(pattern, text):
            issues.append("检测到疑似密钥/敏感串")
            severity = "BLOCK"
            break
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        duplicate_ratio = 1 - (len(set(lines)) / len(lines))
        if duplicate_ratio >= 0.5:
            issues.append("重复内容比例过高（>=50%）")
            severity = "BLOCK"
    else:
        duplicate_ratio = 0.0
    if len(text) > 20000:
        issues.append("上下文过长（>20000 字符），可能挤占有效信息")
        severity = "BLOCK" if severity == "BLOCK" else "WARN"
    if severity == "PASS":
        issues.append("未发现注入/重复/超长/敏感串问题")
    return {
        "verdict": severity,
        "blocked": severity == "BLOCK",
        "issues": issues,
        "chars": len(text),
        "duplicate_ratio": round(duplicate_ratio, 4),
        "note": "规则审查不等于语义安全审计",
    }


def build_context(
    *,
    task: str,
    documents: list[dict[str, Any]] | None = None,
    memories: list[dict[str, Any]] | None = None,
    max_chars: int = 4000,
) -> dict[str, Any]:
    """装配上下文（任务 + 检索文档 + 记忆），按字符预算截断并标注来源。"""
    max_chars = max(200, min(int(max_chars), 20000))
    sections: list[dict[str, Any]] = []
    used = 0

    task = str(task or "").strip()
    if task:
        text = f"任务：{task[: min(len(task), max_chars)]}"
        sections.append({"kind": "task", "text": text, "data_source": "user"})
        used += len(text)

    for doc in documents or []:
        content = str(doc.get("content") or "").strip()
        if not content:
            continue
        review = review_context(content)
        if review["blocked"]:
            sections.append(
                {
                    "kind": "document_blocked",
                    "doc_id": doc.get("doc_id"),
                    "reason": "; ".join(review["issues"]),
                }
            )
            continue
        remaining = max_chars - used
        if remaining <= 40:
            break
        text = (
            f"[检索文档 {doc.get('doc_id')}｜{doc.get('data_source', 'unknown')}] "
            f"{content[:remaining]}"
        )
        sections.append(
            {
                "kind": "retrieved_document",
                "doc_id": doc.get("doc_id"),
                "data_source": doc.get("data_source", "unknown"),
                "text": text,
            }
        )
        used += len(text)

    for memory in memories or []:
        remaining = max_chars - used
        if remaining <= 40:
            break
        text = (
            f"[记忆 {memory.get('key')}] {str(memory.get('value') or '')[:remaining]}"
        )
        sections.append({"kind": "memory", "key": memory.get("key"), "text": text})
        used += len(text)

    prompt = "\n".join(section.get("text", "") for section in sections if section.get("text"))
    return {
        "sections": sections,
        "prompt": prompt,
        "used_chars": len(prompt),
        "max_chars": max_chars,
        "truncated": len(prompt) >= max_chars,
        "note": "上下文装配为确定性字符预算，不调用模型；来源逐项标注",
    }


# ---------------------------------------------------------------------------
# 可解释性（基于 Trace 证据，不是思维链）
# ---------------------------------------------------------------------------

def explain_trace(trace: Any) -> dict[str, Any]:
    """基于 Trace 的决策解释：每步做了什么、证据是什么、是否异常。"""
    steps = list(getattr(trace, "steps", []) or [])
    rationale: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for step in steps:
        step_type = step.step_type.value if hasattr(step.step_type, "value") else str(step.step_type)
        status = step.status.value if hasattr(step.status, "value") else str(step.status)
        evidence = "步骤记录存在"
        if step.tool_name:
            evidence = f"工具 {step.tool_name} 返回结果（tool_result 已记录）"
        if step.error is not None:
            evidence = f"步骤失败：{step.error.error_type}"
            errors.append(
                {
                    "step_id": step.step_id,
                    "error_type": step.error.error_type,
                    "message": step.error.error_message,
                }
            )
        rationale.append(
            {
                "step_id": step.step_id,
                "step_type": step_type,
                "status": status,
                "decision_summary": _step_summary(step),
                "evidence": evidence,
                "duration_ms": step.duration_ms,
            }
        )
    outcome = getattr(getattr(trace, "status", None), "value", str(getattr(trace, "status", "")))
    summary = (
        f"Trace 共 {len(steps)} 个步骤，最终状态 {outcome}；"
        f"失败步骤 {len(errors)} 个。"
    )
    if errors:
        summary += " 首次失败：" + errors[0]["error_type"] + "（见 evidence）。"
    return {
        "trace_id": getattr(trace, "trace_id", None),
        "summary": summary,
        "rationale": rationale,
        "errors": errors,
        "evidence_level": "OBSERVED_TRACE",
        "is_chain_of_thought": False,
        "limitations": [
            "解释基于已记录的步骤输入/输出/错误，不保存也不推断模型内部思维链",
            "不是因果关系证明，也不能给出概率/置信度",
        ],
        "note": "可解释性输出是证据化理由，不是模型的原始推理过程",
    }


def _step_summary(step: Any) -> str:
    step_type = step.step_type.value if hasattr(step.step_type, "value") else str(step.step_type)
    if step_type == "tool_call" and step.tool_name:
        return f"调用工具 {step.tool_name}"
    if step_type == "planning":
        output = step.output
        if isinstance(output, dict) and output.get("plan"):
            return "制定计划：" + "；".join(str(item) for item in output["plan"][:5])
        return "制定执行计划"
    if step_type in {"retrieval", "memory_read", "memory_write", "skill"}:
        return f"{step_type} 操作（详见步骤记录）"
    if step_type in {"model_call", "reflection"}:
        return f"{step_type} 输出已记录（文本内容见 output）"
    return f"{step_type} 步骤"


# ---------------------------------------------------------------------------
# 能力矩阵报告
# ---------------------------------------------------------------------------

def capability_matrix_report() -> dict[str, Any]:
    capabilities = [dict(item) for item in CAPABILITY_MATRIX]
    innovations = [dict(item) for item in INNOVATION_MATRIX]
    capability_counts: dict[str, int] = {}
    for item in capabilities:
        capability_counts[item["status"]] = capability_counts.get(item["status"], 0) + 1
    innovation_counts: dict[str, int] = {}
    for item in innovations:
        innovation_counts[item["status"]] = innovation_counts.get(item["status"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "原始需求 PDF 第三节（20 项能力矩阵）与第四节（7 个创新点）",
        "capability_count": len(capabilities),
        "capability_status_counts": capability_counts,
        "capabilities": capabilities,
        "innovation_count": len(innovations),
        "innovation_status_counts": innovation_counts,
        "innovations": innovations,
        "status_definitions": {
            "COMPLETE": "有真实代码实现 + 自动化测试（L2），核心项有 UI（L3）",
            "PARTIAL": "已实现受限版本，限制在 limitations 中明确列出",
            "UNVERIFIED": "已有实现但缺少运行验证",
            "MISSING": "未实现",
        },
        "honesty_notes": [
            "能力矩阵的状态来自本仓库代码与测试，不按宣传口径拔高",
            "模拟执行/合成数据不等于真实模型或真实生产环境验证",
            "缺失/受限能力已写入 limitations 与 docs/TECH_DEBT.md",
        ],
        "paid_api_called": False,
    }
