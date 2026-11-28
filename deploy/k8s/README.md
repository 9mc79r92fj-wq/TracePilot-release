# TracePilot · kind 单节点 K8s 交付说明

本目录是**纯新增**的 kind/K8s 交付物：面向「FastAPI + React 单端口同源应用」的
镜像构建、K8s 清单与静态校验，用于本地 kind 单节点演练。**不修改、不替换**仓库现有
部署资产（根 `Dockerfile`、`docker-compose.yml`、`render.yaml`、魔搭创空间入口脚本等）。

## 1. 交付物

| 文件 | 作用 |
| --- | --- |
| `kind-config.yaml` | kind 单节点集群：control-plane；host `127.0.0.1:8080` -> 节点 `30080` |
| `namespace.yaml` | 命名空间 `tracepilot`（baseline Pod Security） |
| `configmap.yaml` | 非敏感运行配置：生产档位、公开演示、静态目录、DB 路径、持久化开关、限流 |
| `deployment.yaml` | 单副本，`Recreate`，容器端口 `http=7860`，探针 `/api/health`，非 root uid 10001 |
| `service.yaml` | NodePort：`80 -> 7860`，`nodePort: 30080`，selector 与 Pod labels 完全一致 |
| `pvc.yaml` | **可选**持久化示例（`ReadWriteOnce`，需 local-path-provisioner；默认不纳入 kustomization） |
| `kustomization.yaml` | 默认资源：namespace/configmap/deployment/service（不含 pvc/kind 配置） |
| `Dockerfile` | 多阶段：node 22 构建前端 -> python:3.11-slim 运行；`EXPOSE 7860`；非 root 10001；HEALTHCHECK |

静态校验测试：`tests/test_k8s_manifests.py`（只读解析 YAML/文本，不依赖集群与网络）。

## 2. 隔离说明（不破坏魔搭线上）

- 本目录所有文件均为新增；**未改动**根 `Dockerfile`、`docker-entrypoint.sh`、
  `docker-compose.yml`、`render.yaml`、`backend/`、`frontend/`、`docs/` 等现有资产。
- 新镜像 tag 为 `tracepilot:local`，仅通过 `kind load docker-image` 进入本地 kind 集群，
  与魔搭（ModelScope）创空间线上镜像 / 部署副本 / 数据卷无任何关联。
- 新命名空间 `tracepilot` 与线上环境隔离；`TRACEPILOT_PUBLIC_DEMO=1`、
  `TRACEPILOT_ALLOW_REAL_API=0` 保证演示环境不会发起付费真实调用。
- 现有「8000 端口后端 + 8080 前端 nginx」的双容器方案继续有效，本交付不改动它。

## 3. 前置条件

- Windows + PowerShell（命令以 PowerShell 为例，Linux/macOS 只需换成对应 shell 语法）；
- Docker Desktop（或等价 Docker 守护进程）；
- [`kind`](https://kind.sigs.k8s.io/) **≥ 0.20**，`kubectl` 可用；
- 目标 Kubernetes 版本 **≥ 1.27**（清单未使用任何 alpha API）。

> **本机验证边界（如实说明）**：本次交付所在机器**未实际拉起 kind 集群**
> （Docker 守护进程不可用，且未安装 kind 二进制），清单仅通过
> `pytest tests/test_k8s_manifests.py` 的静态校验；下列命令按
> kind ≥ 0.20 / Kubernetes ≥ 1.27 的接口编写，**未在本机执行**。
> 首次在真实环境运行时，请以 `kind` / `kubectl` 的实际输出为准。

## 4. 构建镜像

镜像是**纯新增**的（`deploy/k8s/Dockerfile`），不替换根目录 Dockerfile，也不影响魔搭线上镜像。

在仓库根目录执行（需要 Docker ≥ 23 / BuildKit，默认开启）：

```powershell
docker build -f deploy/k8s/Dockerfile -t tracepilot:local .
```

构建上下文说明：
- 仓库根 `.dockerignore` 为"后端镜像 + 预构建前端"场景排除了 `frontend/`；
- 本目录提供 `deploy/k8s/Dockerfile.dockerignore`，BuildKit 会优先使用它，**不排除 `frontend/`**，
  因此上面的命令可直接构建，无需修改根 `.dockerignore`，也不影响既有部署；
- 该规则同时排除了 `.env*`、`backend/data`、`data/`、`tests/`、`node_modules` 等，避免把本地密钥与运行数据带进镜像。

<details>
<summary>旧版 Docker（&lt; 23，无 BuildKit）的备选：临时构建上下文</summary>

```powershell
$ctx = Join-Path $env:TEMP "tracepilot-k8s-context"
if (Test-Path $ctx) { Remove-Item -Recurse -Force $ctx }
New-Item -ItemType Directory -Force $ctx | Out-Null
# 排除本地密钥与运行数据，避免把 backend/.env、backend/data 带进构建上下文
robocopy backend  $ctx\backend  /E /XD data __pycache__ /XF .env /NFL /NDL /NJH /NJS /NP | Out-Null
robocopy config   $ctx\config   /E /NFL /NDL /NJH /NJS /NP | Out-Null
robocopy scripts  $ctx\scripts  /E /NFL /NDL /NJH /NJS /NP | Out-Null
robocopy frontend $ctx\frontend /E /XD node_modules dist test-results /NFL /NDL /NJH /NJS /NP | Out-Null
Copy-Item requirements.txt $ctx\
Copy-Item deploy\k8s\Dockerfile $ctx\Dockerfile
docker build -t tracepilot:local $ctx
```

</details>

构建完成后本机校验（可选）：

```powershell
docker run --rm tracepilot:local python -c "import sys; print(sys.version)"
```

## 5. 创建 kind 集群

```powershell
kind create cluster --name tracepilot --config deploy\k8s\kind-config.yaml
kubectl config use-context kind-tracepilot
kubectl cluster-info --context kind-tracepilot
kubectl get nodes
```

预期：单节点 `tracepilot-control-plane`，`Ready`；宿主 `127.0.0.1:8080` 已映射到节点 `30080`。

## 6. 导入本地镜像

```powershell
kind load docker-image tracepilot:local --name tracepilot
```

Deployment 使用 `imagePullPolicy: IfNotPresent`，导入后不会再尝试从公网拉取。
若 Pod 出现 `ErrImagePull` / `ImagePullBackOff`，说明镜像未成功导入该节点，重新执行上面命令。

## 7. 可选：创建 Secret（默认不需要）

清单中敏感变量只允许 `secretKeyRef`（`tracepilot-secrets`，`optional: true`），
所以**不创建 Secret 也能正常启动**；公开演示模式（`TRACEPILOT_PUBLIC_DEMO=1`）下
真实 API 调用被强制关闭。需要为后续受控验证预留 Key 时，在集群内单独创建：

```powershell
kubectl create secret generic tracepilot-secrets -n tracepilot `
  --from-literal=DEEPSEEK_API_KEY='<your-deepseek-api-key>' `
  --from-literal=TRACEPILOT_ADMIN_TOKEN='<your-admin-token>'
```

注意：Secret 不要写入仓库或清单文件；本目录内所有 YAML 均不含任何明文 Key/Token。

## 8. 部署

```powershell
kubectl apply -k deploy\k8s
kubectl -n tracepilot rollout status deployment/tracepilot --timeout=180s
kubectl -n tracepilot get deploy,svc,pod -o wide
```

预期：

- `deployment.apps/tracepilot` 1/1 Ready；
- `service/tracepilot` NodePort `80:30080/TCP`；
- Pod 以 uid `10001` 运行（非 root）。

## 9. 验证

```powershell
# 健康检查（同源 /api）
Invoke-RestMethod http://127.0.0.1:8080/api/health

# 前端 SPA 根路径（应返回 200 + HTML，而不是 404）
(Invoke-WebRequest http://127.0.0.1:8080/).StatusCode

# 容器内 uid（应为 10001）
kubectl -n tracepilot exec deploy/tracepilot -- id

# 探针与事件
kubectl -n tracepilot describe pod -l app.kubernetes.io/name=tracepilot
kubectl -n tracepilot logs deploy/tracepilot --tail=50

# 可选：不经过 8080 映射，直接用端口转发验证
kubectl -n tracepilot port-forward svc/tracepilot 9080:80
# 另开终端：Invoke-RestMethod http://127.0.0.1:9080/api/health
```

## 10. 可选：启用持久化（PVC）

kind 默认集群**没有 StorageClass**，因此 `pvc.yaml` 不在默认 kustomization 中，
避免「一条命令 apply 后 PVC Pending / Pod 无法调度」。启用步骤：

```powershell
# 10.1 安装 local-path-provisioner（会创建名为 local-path 的 StorageClass）
kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.37/deploy/local-path-storage.yaml
kubectl -n local-path-storage rollout status deploy/local-path-provisioner --timeout=180s
kubectl get storageclass

# 10.2 创建 PVC（等待绑定时可先继续；首次被 Pod 使用时才真正绑定）
kubectl apply -f deploy\k8s\pvc.yaml
kubectl -n tracepilot get pvc tracepilot-data -w
```

然后编辑 `deployment.yaml`，把 `volumes` 中的两个 `emptyDir` 换成文件末尾注释里的
`persistentVolumeClaim`（`claimName: tracepilot-data`），再重新 apply：

```powershell
kubectl apply -k deploy\k8s
kubectl -n tracepilot rollout status deployment/tracepilot --timeout=180s
```

说明：

- PVC 为 `ReadWriteOnce`，只适合单节点单副本；数据目录 `/app/data`
  与 `/app/backend/data` 同时挂载同一 PVC（应用代码路径不变）。
- SQLite 只允许单写者，因此 Deployment 固定 `replicas: 1` + `strategy: Recreate`。
- 删除 PVC 会删除数据（`local-path` 默认回收策略）；本地演练请勿存放重要数据。

## 11. 清理

```powershell
kubectl delete -k deploy\k8s
kubectl delete -f deploy\k8s\pvc.yaml --ignore-not-found
kind delete cluster --name tracepilot
```

> 删除整个 kind 集群也会一并删除本地卷数据，无需再手动清理。

## 12. 常见问题

| 现象 | 排查方向 |
| --- | --- |
| `docker build` 报 `frontend/... not found` | 确认 Docker ≥ 23 / BuildKit 已开启（`docker buildx version`），`deploy/k8s/Dockerfile.dockerignore` 会自动生效；旧版 Docker 用第 4 节临时上下文方案 |
| Pod `ImagePullBackOff` | 镜像未导入：`kind load docker-image tracepilot:local --name tracepilot` |
| Pod `Pending` / PVC `Pending` | 未安装 local-path-provisioner，或 Deployment 已改 PVC 但 PVC 未创建 |
| `CrashLoopBackOff` | `kubectl -n tracepilot logs deploy/tracepilot --previous`；多为依赖安装或数据目录权限问题 |
| 探针失败 | 首次启动 `start-period` 内失败属正常；持续失败看容器日志与 `/api/health` |
| `8080` 无法访问 | 确认 kind 创建时用了 `kind-config.yaml`；`docker ps` 查看端口映射；改用 port-forward |
| 端口被占用 | Windows 上 8080 常被其他服务占用，可修改 `kind-config.yaml` 的 `hostPort` 后重建集群 |

## 13. 生产边界（kind 演示 ≠ 生产）

本清单定位为**本地 kind 单节点验收**，不是生产编排：

- 无 Ingress/TLS、无 HPA/PDB/NetworkPolicy、无多副本与反亲和；
- 默认 `emptyDir` 不持久化，重启即丢数据；启用 PVC 也只是单节点本地卷；
- 单副本 + 进程内限流（多实例需 Redis/边缘限流）；
- 镜像为本地 `tracepilot:local`，未经镜像仓库/签名/漏洞扫描；
- Secret 需在集群内单独创建，清单不含任何敏感值；
- 生产化建议：改用托管数据库或单写者存储、以 Deployment/StatefulSet 配合
  RWO/PVC 与备份、补 NetworkPolicy 与资源配额、接入镜像仓库与镜像扫描、
  通过 Ingress/Gateway 终结 TLS，并单独做压测与故障演练。
