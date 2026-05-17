# Kube Release Doctor

Kube Release Doctor 是一个用于排查 Kubernetes Deployment 发布故障的轻量级 CLI 工具。

当前版本是 MVP v0.1，只使用 Python 标准库，不依赖 Kubernetes Python Client，也不使用任何 Web 框架。工具通过 `subprocess` 调用本机 `kubectl` 获取集群信息，并生成 Markdown 诊断报告。

## 核心能力

- 读取指定 Deployment 的 JSON 信息。
- 根据 `deployment.spec.selector.matchLabels` 自动查找关联 Pods。
- 检查 Deployment 当前副本状态：
  - `spec.replicas`
  - `status.availableReplicas`
  - `status.updatedReplicas`
  - `status.readyReplicas`
  - `status.conditions`
- 检查 Pod 当前状态：
  - `phase`
  - `containerStatuses`
  - `ready`
  - `restartCount`
  - `waiting.reason`
- 识别常见发布异常：
  - `ImagePullBackOff`
  - `ErrImagePull`
  - `CrashLoopBackOff`
  - `CreateContainerConfigError`
  - `Pending`
  - `ContainerCreating`
- 从 Deployment 的 `env` 和 `envFrom` 中解析 Secret / ConfigMap 引用：
  - `secretKeyRef`
  - `configMapKeyRef`
  - `envFrom.secretRef`
  - `envFrom.configMapRef`
- 检查被引用的 Secret / ConfigMap 当前是否存在。
- 对异常 Pod 执行 `kubectl describe pod`，提取 Events 中的错误关键词。
- 区分当前故障和历史告警：
  - 如果 Deployment、Pod、Secret、ConfigMap 当前都健康，则报告整体状态为 `Healthy`。
  - 健康状态下，历史 Events 中的 `Failed`、`secret not found` 等关键词不会被当成当前根因。
  - 这些历史事件会进入 `Historical Warnings` 部分。
- 输出 Markdown 报告到终端，并保存到文件。

## 环境要求

- Python 3.8+
- 已安装 `kubectl`
- 当前 kubeconfig 已指向目标集群
- 当前用户具备以下资源的读取权限：
  - Deployment
  - Pod
  - Secret
  - ConfigMap

不需要安装任何第三方 Python 依赖。

## 快速开始

```bash
python kube_release_doctor.py --namespace prod --deployment devops-cicd-demo
```

Windows PowerShell：

```powershell
python .\kube_release_doctor.py --namespace prod --deployment devops-cicd-demo
```

默认报告路径：

```text
reports/release-doctor-<namespace>-<deployment>.md
```

示例：

```text
reports/release-doctor-prod-devops-cicd-demo.md
```

## 指定输出文件

```bash
python kube_release_doctor.py \
  --namespace prod \
  --deployment devops-cicd-demo \
  --output reports/prod-demo-release-report.md
```

PowerShell：

```powershell
python .\kube_release_doctor.py `
  --namespace prod `
  --deployment devops-cicd-demo `
  --output reports\prod-demo-release-report.md
```

## 命令行参数

| 参数 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--namespace` | 是 | 无 | Kubernetes namespace，例如 `prod` |
| `--deployment` | 是 | 无 | Deployment 名称，例如 `devops-cicd-demo` |
| `--output` | 否 | `reports/release-doctor-<namespace>-<deployment>.md` | Markdown 报告输出路径 |

查看帮助：

```bash
python kube_release_doctor.py --help
```

## 报告结构

生成的 Markdown 报告包含以下部分：

- `Basic Info`：namespace、deployment、生成时间、执行命令。
- `Current Status`：整体判断为 `Healthy` 或 `Unhealthy or Unknown`。
- `Deployment Status`：Deployment 副本和 conditions。
- `Pod Status`：Pod phase、容器 ready、重启次数、waiting reason、问题和告警。
- `Secret / ConfigMap Check`：Deployment 引用的 Secret / ConfigMap 是否存在。
- `Events Analysis`：异常 Pod 或健康态 Pod 的 Events 关键词分析。
- `Historical Warnings`：当前健康时，将历史错误事件放在这里，不作为当前故障根因。
- `Possible Root Cause`：当前故障的可能根因；如果当前健康，会明确说明没有检测到当前发布故障。
- `Suggested Fix`：修复建议和可执行命令示例。

## 健康状态判断

当同时满足以下条件时，工具会把整体状态判断为 `Healthy`：

- `availableReplicas == spec.replicas`
- 所有 Pod 的 `phase == Running`
- 所有普通容器的 `ready == True`
- Deployment 引用的 Secret / ConfigMap 当前都存在

在 `Healthy` 状态下，即使 Pod 历史 Events 中出现过错误关键词，也不会被归类为当前故障。

## RestartCount 处理

如果容器 `restartCount > 0`，但容器当前 `Ready=True`，工具会把它标记为 warning/info：

```text
RestartCount=1, container currently ready
```

这类情况不会直接作为当前故障根因。

## Secret / ConfigMap 修复示例

当检测到 Secret 缺失时，报告会给出命令示例：

```bash
kubectl create secret generic <secret-name> \
  --from-literal=<KEY>=<VALUE> \
  -n <namespace>
```

当检测到 ConfigMap 缺失时，报告会给出命令示例：

```bash
kubectl create configmap <configmap-name> \
  --from-literal=<KEY>=<VALUE> \
  -n <namespace>
```

`<KEY>` 和 `<VALUE>` 是占位符。工具不会猜测真实 key/value，执行前需要替换为应用实际需要的配置。

## 工具执行的 kubectl 命令

工具会自动执行类似以下命令：

```bash
kubectl get deployment <deployment> -n <namespace> -o json
kubectl get pods -n <namespace> -l key=value -o json
kubectl get secret <secret-name> -n <namespace>
kubectl get configmap <configmap-name> -n <namespace>
kubectl describe pod <pod-name> -n <namespace>
```

如果某个 `kubectl` 命令失败，程序不会直接崩溃，而是把错误写入报告。

## 示例输出结论

当前资源健康，但存在历史告警时：

```text
No current release failure detected. Historical warning events were found, but current resources are healthy.
```

当前健康时的修复建议：

```text
No immediate fix required. Historical warnings can be reviewed with kubectl describe pod if needed.
```

## 当前限制

- v0.1 只支持 Deployment 维度诊断。
- 只解析 `spec.selector.matchLabels`，暂不支持复杂 `matchExpressions` selector。
- `ContainerCreating` 是否卡住目前按 `waiting.reason` 识别，暂不根据持续时间判断。
- Events 分析基于常见错误关键词匹配，不替代人工排查。
- 工具只读取集群状态，不会自动修改 Kubernetes 资源。

## 开发说明

语法检查：

```bash
python -m py_compile kube_release_doctor.py
```

本项目目标是保持简单、可读、易于在受限环境中运行，因此 v0.1 不引入第三方依赖。
