"""kind/K8s 交付清单的只读静态校验（不依赖集群、不访问网络、不修改文件）。

覆盖的硬性设计约束：
1. 所有 YAML 可被 ``yaml.safe_load_all`` 解析；资源齐全、统一命名空间 ``tracepilot``；
2. Deployment 的副本/升级策略/端口/镜像/探针/资源/安全上下文（非 root uid 10001、
   allowPrivilegeEscalation=false、capabilities.drop=ALL）；
3. 环境变量来自 ConfigMap，且至少包含生产档位、公开演示、静态目录、DB 路径与持久化开关；
4. 敏感信息只允许 optional Secret ``tracepilot-secrets`` 的 ``secretKeyRef``，清单无明文 Key/Token；
5. Service 为 NodePort（80 -> 7860，nodePort 30080），selector 与 Pod labels 完全一致；
6. kind 配置为单 control-plane，且 hostPort 8080 <-> containerPort 30080；
7. Dockerfile 为多阶段（node 构建 -> python:3.11-slim 运行），EXPOSE 7860、非 root、
   HEALTHCHECK、读取 frontend/dist 的构建产物、无 8000 端口与明文密钥；
8. 可选 PVC 不进入默认 kustomization，保证 ``kubectl apply -k deploy/k8s`` 可直接成功。

定位方式与现有测试一致：``Path(__file__).resolve().parents[1]`` 即仓库根。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = PROJECT_ROOT / "deploy" / "k8s"

NAMESPACE = "tracepilot"
IMAGE = "tracepilot:local"
CONTAINER_PORT = 7860
NODE_PORT = 30080
HOST_PORT = 8080
SECRET_NAME = "tracepilot-secrets"

MANIFEST_NAMES = (
    "namespace.yaml",
    "configmap.yaml",
    "deployment.yaml",
    "service.yaml",
    "pvc.yaml",
)

# 设计约束 3：ConfigMap 必须提供的环境变量
EXPECTED_ENV = {
    "TRACEPILOT_ENV": "production",
    "TRACEPILOT_PUBLIC_DEMO": "1",
    "TRACEPILOT_STATIC_DIR": "/app/frontend/dist",
    "TRACEPILOT_DB_PATH": "/app/data/tracepilot.db",
    "TRACEPILOT_PERSIST_AGENT_RUNS": "1",
    "TRACEPILOT_PERSIST_EVAL_RUNS": "1",
}

# 设计约束 4：敏感变量名（只允许 secretKeyRef，不允许任何明文值）
SENSITIVE_ENV_NAMES = ("DEEPSEEK_API_KEY", "TRACEPILOT_ADMIN_TOKEN")

# 明文密钥的启发式扫描规则（命中即失败）
PLAINTEXT_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?im)^\s*(?:DEEPSEEK_API_KEY|TRACEPILOT_ADMIN_TOKEN)\s*:\s*\S+"),
    re.compile(
        r"(?im)^\s*(?:api[_-]?key|admin[_-]?token|access[_-]?token|password|secret)"
        r"\s*:\s*['\"]?[A-Za-z0-9+/=_\-]{8,}"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


# ---------------------------------------------------------------------------
# 只读解析辅助
# ---------------------------------------------------------------------------
def _manifest_path(name: str) -> Path:
    path = K8S_DIR / name
    assert path.is_file(), f"缺少清单文件: {path}"
    return path


def _parse(name: str) -> list[Any]:
    """用 yaml.safe_load_all 解析单个清单文件（只读）。"""
    text = _manifest_path(name).read_text(encoding="utf-8")
    docs = [doc for doc in yaml.safe_load_all(text) if doc is not None]
    assert docs, f"{name} 未解析出任何 YAML 文档"
    return docs


def _manifest(name: str) -> dict[str, Any]:
    docs = _parse(name)
    assert len(docs) == 1, f"{name} 应只包含 1 个 YAML 文档，实际 {len(docs)}"
    assert isinstance(docs[0], dict), f"{name} 顶层应为 mapping"
    return docs[0]


def _deployment() -> dict[str, Any]:
    return _manifest("deployment.yaml")


def _pod_spec(dep: dict[str, Any]) -> dict[str, Any]:
    return dep["spec"]["template"]["spec"]


def _container(dep: dict[str, Any]) -> dict[str, Any]:
    containers = _pod_spec(dep)["containers"]
    assert len(containers) == 1, "Deployment 应只有一个应用容器"
    return containers[0]


def _named_ports(ctr: dict[str, Any]) -> dict[str, int]:
    ports: dict[str, int] = {}
    for item in ctr.get("ports", []):
        assert "containerPort" in item, f"容器端口缺少 containerPort: {item}"
        if item.get("name"):
            ports[item["name"]] = item["containerPort"]
    return ports


def _resolve_port(port: Any, named_ports: dict[str, int]) -> int | None:
    """把探测/Service 端口解析成容器端口数字（命名端口按容器 ports 解析）。"""
    if isinstance(port, str):
        return named_ports.get(port)
    if isinstance(port, int):
        return port
    return None


def _security_value(pod: dict[str, Any], ctr: dict[str, Any], key: str) -> Any:
    """取安全上下文字段：容器级优先，其次 Pod 级（约束允许两者其一）。"""
    for scope in (ctr.get("securityContext") or {}, pod.get("securityContext") or {}):
        if key in scope:
            return scope[key]
    return None


def _repo_relative(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


# ---------------------------------------------------------------------------
# 1. YAML 可解析 + 资源齐全 + 命名空间
# ---------------------------------------------------------------------------
def test_all_manifests_are_parseable() -> None:
    for name in MANIFEST_NAMES:
        docs = _parse(name)
        assert len(docs) == 1, f"{name} 应只包含 1 个文档"
        assert isinstance(docs[0], dict), f"{name} 顶层应为 mapping"
        assert docs[0].get("apiVersion"), f"{name} 缺少 apiVersion"
        assert docs[0].get("kind"), f"{name} 缺少 kind"

    # 非 API 资源：kind 集群配置 + kustomization，也必须可解析
    for name, kind in (("kind-config.yaml", "Cluster"), ("kustomization.yaml", "Kustomization")):
        doc = _manifest(name)
        assert doc["kind"] == kind


def test_k8s_resources_are_namespaced_tracepilot() -> None:
    expected_kinds = {
        "configmap.yaml": "ConfigMap",
        "deployment.yaml": "Deployment",
        "service.yaml": "Service",
        "pvc.yaml": "PersistentVolumeClaim",
    }
    for name, kind in expected_kinds.items():
        doc = _manifest(name)
        assert doc["kind"] == kind, f"{name} kind 应为 {kind}"
        metadata = doc["metadata"]
        assert metadata.get("name"), f"{name} 缺少 metadata.name"
        assert metadata.get("namespace") == NAMESPACE, f"{name} 必须位于 {NAMESPACE} 命名空间"

    ns = _manifest("namespace.yaml")
    assert ns["kind"] == "Namespace"
    assert ns["metadata"]["name"] == NAMESPACE
    # Namespace 是集群级资源，不应带 metadata.namespace
    assert "namespace" not in ns["metadata"]


# ---------------------------------------------------------------------------
# 2. kustomization：默认资源可无依赖 apply
# ---------------------------------------------------------------------------
def test_kustomization_default_resources_exclude_optional_pvc() -> None:
    kustomization = _manifest("kustomization.yaml")
    assert kustomization["kind"] == "Kustomization"
    assert kustomization.get("namespace") == NAMESPACE

    resources = {
        item.lstrip("./") if isinstance(item, str) else str(item)
        for item in kustomization.get("resources", [])
    }
    assert resources == {
        "namespace.yaml",
        "configmap.yaml",
        "deployment.yaml",
        "service.yaml",
    }, f"默认 resources 不符: {resources}"
    # kind 集群配置不是 K8s 资源；可选 PVC 依赖 local-path-provisioner（kind 默认无 StorageClass）
    assert "kind-config.yaml" not in resources
    assert "pvc.yaml" not in resources, "pvc.yaml 必须保持可选，不能进入默认 kustomization"


def test_pvc_example_is_valid_and_optional_by_design() -> None:
    pvc = _manifest("pvc.yaml")
    spec = pvc["spec"]
    assert spec["accessModes"] == ["ReadWriteOnce"]
    request = spec["resources"]["requests"]["storage"]
    assert request, "PVC 必须声明 storage 容量"
    assert spec.get("storageClassName") == "local-path", "PVC 依赖 local-path-provisioner 的 StorageClass"


# ---------------------------------------------------------------------------
# 3. Deployment：副本/升级策略/镜像/端口
# ---------------------------------------------------------------------------
def test_deployment_replicas_strategy_image_and_port() -> None:
    dep = _deployment()
    spec = dep["spec"]

    assert spec["replicas"] == 1
    assert spec["strategy"]["type"] == "Recreate", "SQLite 单写者必须用 Recreate"
    assert spec["selector"]["matchLabels"], "缺少 selector.matchLabels"

    ctr = _container(dep)
    assert ctr["name"]
    assert ctr["image"] == IMAGE
    assert ctr["imagePullPolicy"] == "IfNotPresent"

    ports = ctr["ports"]
    assert len(ports) == 1, "应用应只暴露一个同源端口（/api + SPA）"
    port = ports[0]
    assert port.get("name") == "http"
    assert port["containerPort"] == CONTAINER_PORT
    assert port.get("protocol", "TCP") == "TCP"


# ---------------------------------------------------------------------------
# 4. Deployment：探针指向 /api/health，且通过命名端口引用 7860
# ---------------------------------------------------------------------------
def test_deployment_probes_use_health_path_and_named_port() -> None:
    dep = _deployment()
    ctr = _container(dep)
    named_ports = _named_ports(ctr)

    for probe_name in ("livenessProbe", "readinessProbe"):
        assert probe_name in ctr, f"缺少 {probe_name}"
        probe = ctr[probe_name]
        http_get = probe.get("httpGet")
        assert http_get, f"{probe_name} 应使用 httpGet"
        assert http_get.get("path") == "/api/health"
        port_ref = http_get.get("port")
        assert isinstance(port_ref, str), f"{probe_name} 的 port 应为容器命名端口引用"
        assert _resolve_port(port_ref, named_ports) == CONTAINER_PORT


# ---------------------------------------------------------------------------
# 5. Deployment：资源 requests/limits + 安全上下文
# ---------------------------------------------------------------------------
def test_deployment_resources_are_set() -> None:
    ctr = _container(_deployment())
    resources = ctr.get("resources")
    assert resources, "缺少 resources"
    for section in ("requests", "limits"):
        values = resources.get(section)
        assert values, f"缺少 resources.{section}"
        for key in ("cpu", "memory"):
            assert key in values, f"resources.{section} 缺少 {key}"


def test_deployment_security_context_is_non_root_and_least_privilege() -> None:
    dep = _deployment()
    pod = _pod_spec(dep)
    ctr = _container(dep)

    # 非 root：容器级或 Pod 级其一即可（本清单放在 Pod 级，供 fsGroup/emptyDir 共用）
    assert _security_value(pod, ctr, "runAsNonRoot") is True
    assert _security_value(pod, ctr, "runAsUser") == 10001
    assert _security_value(pod, ctr, "runAsGroup") in (None, 10001)

    # 最小权限
    assert _security_value(pod, ctr, "allowPrivilegeEscalation") is False
    capabilities = (ctr.get("securityContext") or {}).get("capabilities") or {}
    drop = capabilities.get("drop") or []
    assert "ALL" in drop, "capabilities.drop 必须包含 ALL"
    # 不允许提权/逃逸类配置
    assert (ctr.get("securityContext") or {}).get("privileged") is not True
    assert pod.get("hostNetwork") is not True
    assert pod.get("hostPID") is not True


def test_deployment_mounts_writable_data_dirs_without_pvc_dependency() -> None:
    """默认清单不引用 PVC：kind 无默认 StorageClass 时 apply 后 Pod 必须能正常调度。"""
    pod = _pod_spec(_deployment())

    volumes = {volume["name"]: volume for volume in pod.get("volumes", [])}
    for volume in volumes.values():
        assert "persistentVolumeClaim" not in volume, (
            "默认 Deployment 不得引用 PVC（否则无 StorageClass 的 kind 集群会 Pending）；"
            "持久化示例应保持在 deployment.yaml 注释中"
        )
    assert volumes.get("app-data", {}).get("emptyDir") is not None
    assert volumes.get("backend-data", {}).get("emptyDir") is not None

    ctr = _container(_deployment())
    mount_paths = {mount["mountPath"] for mount in ctr.get("volumeMounts", [])}
    assert "/app/data" in mount_paths
    assert "/app/backend/data" in mount_paths


# ---------------------------------------------------------------------------
# 6. 配置来源：ConfigMap + optional Secret
# ---------------------------------------------------------------------------
def test_configmap_contains_required_non_secret_env() -> None:
    configmap = _manifest("configmap.yaml")
    assert configmap["kind"] == "ConfigMap"
    data = configmap.get("data") or {}

    for key, expected in EXPECTED_ENV.items():
        assert key in data, f"ConfigMap 缺少 {key}"
        assert str(data[key]) == expected, f"{key} 期望 {expected!r}，实际 {data[key]!r}"

    # 敏感变量绝不能出现在 ConfigMap 中
    for key in SENSITIVE_ENV_NAMES:
        assert key not in data, f"{key} 属于敏感信息，不能进 ConfigMap"


def test_deployment_env_is_injected_from_configmap() -> None:
    configmap = _manifest("configmap.yaml")
    data = configmap.get("data") or {}
    ctr = _container(_deployment())

    env_from = ctr.get("envFrom") or []
    configmap_refs = [
        item["configMapRef"]["name"] for item in env_from if "configMapRef" in item
    ]
    assert data, "ConfigMap 未提供 data"
    assert configmap_refs == [configmap["metadata"]["name"]], (
        "Deployment 必须从同名 ConfigMap 注入环境变量"
    )

    # 解析最终生效的环境变量：ConfigMap -> dispatch envFrom / configMapKeyRef
    resolved: dict[str, str] = dict(data)
    literal_or_secret_names: set[str] = set()
    for item in ctr.get("env") or []:
        name = item["name"]
        literal_or_secret_names.add(name)
        if "value" in item:
            resolved[name] = str(item["value"])
        value_from = item.get("valueFrom") or {}
        config_key = value_from.get("configMapKeyRef")
        if config_key:
            assert config_key.get("name") == configmap["metadata"]["name"]
            resolved[name] = str(data.get(config_key.get("key")))

    for key, expected in EXPECTED_ENV.items():
        assert resolved.get(key) == expected, f"{key} 未从 ConfigMap 正确注入"
        # 必须来自 ConfigMap，而不是硬编码在 Deployment 的 env 中
        assert key not in literal_or_secret_names


def test_sensitive_env_only_uses_optional_secret_refs() -> None:
    ctr = _container(_deployment())
    env_entries = {item["name"]: item for item in ctr.get("env") or []}

    secret_refs = []
    for name in SENSITIVE_ENV_NAMES:
        assert name in env_entries, f"{name} 必须通过 secretKeyRef 注入（允许 Secret 不存在）"
        entry = env_entries[name]
        assert "value" not in entry, f"{name} 不允许硬编码明文值"
        value_from = entry.get("valueFrom") or {}
        assert set(value_from) == {"secretKeyRef"}, f"{name} 只能使用 secretKeyRef"
        secret_ref = value_from["secretKeyRef"]
        assert secret_ref.get("name") == SECRET_NAME
        assert secret_ref.get("key") == name
        assert secret_ref.get("optional") is True, "Secret 缺失时 Pod 仍需能启动"
        secret_refs.append(secret_ref)

    assert secret_refs, "至少应演示一例 secretKeyRef 注入"


# ---------------------------------------------------------------------------
# 7. 明文敏感信息扫描（整份 YAML 文本）
# ---------------------------------------------------------------------------
def test_manifests_have_no_plaintext_secrets() -> None:
    for name in MANIFEST_NAMES:
        text = _manifest_path(name).read_text(encoding="utf-8")
        for pattern in PLAINTEXT_SECRET_PATTERNS:
            match = pattern.search(text)
            assert match is None, (
                f"{_repo_relative(K8S_DIR / name)} 疑似包含明文敏感信息: {match.group(0)!r}"
            )


# ---------------------------------------------------------------------------
# 8. Service：NodePort 映射 + selector 完全匹配
# ---------------------------------------------------------------------------
def test_service_nodeport_maps_to_container_port() -> None:
    service = _manifest("service.yaml")
    assert service["kind"] == "Service"
    spec = service["spec"]
    assert spec["type"] == "NodePort"

    ports = spec["ports"]
    assert len(ports) == 1
    port = ports[0]
    assert port["port"] == 80
    assert port["nodePort"] == NODE_PORT
    assert port.get("protocol", "TCP") == "TCP"

    named_ports = _named_ports(_container(_deployment()))
    assert _resolve_port(port.get("targetPort"), named_ports) == CONTAINER_PORT


def test_service_selector_exactly_matches_deployment_pod_labels() -> None:
    service = _manifest("service.yaml")
    dep = _deployment()

    pod_labels = dep["spec"]["template"]["metadata"]["labels"]
    selector = service["spec"]["selector"]
    assert selector == pod_labels, "Service selector 必须与 Pod 模板 labels 完全一致"

    # Deployment selector 必须是 Pod labels 的子集（否则 apply 会校验失败）
    match_labels = dep["spec"]["selector"]["matchLabels"]
    assert match_labels, "Deployment selector.matchLabels 不能为空"
    for key, value in match_labels.items():
        assert pod_labels.get(key) == value


# ---------------------------------------------------------------------------
# 9. kind 配置：单节点 + 端口映射
# ---------------------------------------------------------------------------
def test_kind_config_single_node_and_port_mapping() -> None:
    config = _manifest("kind-config.yaml")
    assert config["kind"] == "Cluster"
    assert str(config["apiVersion"]).startswith("kind.x-k8s.io/")

    nodes = config["nodes"]
    assert len(nodes) == 1, "要求 kind 单节点集群"
    node = nodes[0]
    assert node.get("role") == "control-plane"

    mappings = node.get("extraPortMappings") or []
    assert mappings, "缺少 extraPortMappings"
    assert any(
        mapping.get("hostPort") == HOST_PORT
        and mapping.get("containerPort") == NODE_PORT
        for mapping in mappings
    ), f"必须映射 hostPort {HOST_PORT} -> containerPort {NODE_PORT}"


# ---------------------------------------------------------------------------
# 10. Dockerfile：多阶段/单端口/非 root/健康检查
# ---------------------------------------------------------------------------
def _dockerfile_text() -> str:
    path = K8S_DIR / "Dockerfile"
    assert path.is_file(), f"缺少 {_repo_relative(path)}"
    return path.read_text(encoding="utf-8")


def test_dockerfile_is_multistage_and_builds_frontend() -> None:
    text = _dockerfile_text()
    froms = re.findall(r"(?m)^FROM\s+(\S+)", text)

    assert len(froms) >= 2, "Dockerfile 必须为多阶段构建"
    assert "node" in froms[0], "第一阶段必须用 node 构建前端"
    assert any(image.startswith("python:3.11-slim") for image in froms), (
        "运行时阶段必须基于 python:3.11-slim"
    )

    assert "npm ci" in text
    assert "npm run build" in text
    assert "frontend/dist" in text
    assert re.search(r"(?m)^COPY --from=frontend-build .*frontend/dist", text), (
        "必须把前端构建产物 frontend/dist 拷入运行镜像"
    )
    assert "TRACEPILOT_STATIC_DIR=/app/frontend/dist" in text
    assert "TRACEPILOT_DB_PATH=/app/data/tracepilot.db" in text


def test_dockerfile_exposes_only_7860_and_runs_non_root() -> None:
    text = _dockerfile_text()

    expose_ports = re.findall(r"(?m)^EXPOSE\s+(\d+)", text)
    assert expose_ports == ["7860"], f"只能 EXPOSE 7860，实际 {expose_ports}"
    assert "EXPOSE 8000" not in text
    assert not re.search(r"(?m)^EXPOSE\s+8000\b", text)
    assert "7860" in text

    assert re.search(r"(?m)^USER\s+10001\s*$", text), "必须切换到 uid 10001 运行"
    assert re.search(r"useradd[^\n]*10001", text), "必须创建 uid 10001 的非 root 用户"
    assert re.search(r"chown[^\n]*10001[^\n]*/app/data[^\n]*/app/backend/data", text), (
        "必须把 /app/data 与 /app/backend/data 的属主改为 10001"
    )
    assert "mkdir -p /app/data /app/backend/data" in text


def test_dockerfile_has_healthcheck_on_api_health() -> None:
    text = _dockerfile_text()
    assert "HEALTHCHECK" in text, "Dockerfile 必须包含 HEALTHCHECK"

    start = text.index("HEALTHCHECK")
    block = text[start : start + 500]
    assert "/api/health" in block
    assert "urllib" in block, "健康检查应使用 Python 标准库（镜像内无 curl）"
    assert "7860" in block


def test_dockerfile_cmd_listens_on_7860() -> None:
    text = _dockerfile_text()
    cmd_lines = [line for line in text.splitlines() if re.match(r"^\s*CMD\s+\[", line)]
    assert len(cmd_lines) == 1, "应有且只有一个 JSON 形式的 CMD（HEALTHCHECK 里的 CMD 除外）"
    cmd = cmd_lines[0]
    assert "uvicorn" in cmd
    assert "0.0.0.0" in cmd
    assert "--port" in cmd
    assert "7860" in cmd


def test_dockerfile_has_no_plaintext_secret_or_legacy_port() -> None:
    text = _dockerfile_text()
    for pattern in PLAINTEXT_SECRET_PATTERNS:
        match = pattern.search(text)
        assert match is None, f"Dockerfile 疑似包含明文敏感信息: {match.group(0)!r}"

    assert "DEEPSEEK_API_KEY" not in text, "密钥只允许运行时由 K8s Secret 注入"
    assert "EXPOSE 8000" not in text


# ---------------------------------------------------------------------------
# 11. README：操作步骤与本机验证边界
# ---------------------------------------------------------------------------
def test_readme_documents_workflow_and_limitations() -> None:
    path = K8S_DIR / "README.md"
    assert path.is_file(), f"缺少 {_repo_relative(path)}"
    text = path.read_text(encoding="utf-8")

    for required in (
        "kind create cluster",
        "kind-config.yaml",
        "kind load docker-image",
        "kubectl apply -k",
        "kubectl delete",
        "kind delete cluster",
        "local-path",
        "Dockerfile.dockerignore",  # 与根 .dockerignore 的冲突必须有说明
    ):
        assert required in text, f"README 缺少关键说明: {required}"

    # 如实说明本机未实际拉起集群、仅静态校验
    assert "未实际拉起" in text
    assert "静态校验" in text
    # 端口链路与隔离说明
    assert "8080" in text and "30080" in text and "7860" in text
    assert "魔搭" in text


# ---------------------------------------------------------------------------
# 12. 构建上下文与运行期目录（kind 可用性关键约束）
# ---------------------------------------------------------------------------
def test_dockerignore_for_k8s_build_context() -> None:
    """kind 镜像必须能读取 frontend/ 源码，同时排除本地密钥/运行数据。"""
    path = K8S_DIR / "Dockerfile.dockerignore"
    assert path.is_file(), "缺少 deploy/k8s/Dockerfile.dockerignore（BuildKit 专用 ignore）"
    rules = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # 不能排除前端源码目录（否则前端构建阶段直接失败）
    assert "frontend" not in rules
    assert "frontend/" not in rules
    assert "frontend/dist" in rules  # 构建产物在镜像内生成，不需要从上下文带入
    # 必须排除本地密钥与运行数据
    for required in (".env", ".env.*", "backend/.env", "backend/data", "data"):
        assert required in rules, f"Dockerfile.dockerignore 缺少排除规则: {required}"


def test_deployment_command_prepares_writable_data_dirs() -> None:
    """emptyDir 会覆盖镜像目录，容器启动命令必须先创建运行期目录再 exec uvicorn。"""
    import yaml

    docs = [
        doc
        for doc in yaml.safe_load_all((K8S_DIR / "deployment.yaml").read_text(encoding="utf-8"))
        if doc
    ]
    deployment = next(doc for doc in docs if doc.get("kind") == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    command = " ".join(str(part) for part in (container.get("command") or []))
    assert "mkdir -p" in command
    for directory in ("/app/data/documents", "/app/backend/data/documents", "/app/data/traces"):
        assert directory in command, f"启动命令缺少目录创建: {directory}"
    assert "exec python -m uvicorn" in command
    assert "--port 7860" in command
