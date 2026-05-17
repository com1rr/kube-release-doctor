# Kube Release Doctor

Kube Release Doctor 是一个用于排查 Kubernetes Deployment 发布故障的轻量级 CLI 工具。

当前版本为 Python 版 v0.2，仍然只使用 Python 标准库，不依赖 Kubernetes Python Client，也不使用任何 Web 框架。工具通过 `subprocess` 调用本机 `kubectl` 获取集群信息，并生成 Markdown 诊断报告。

## 核心能力

- 执行 kubectl 环境预检：
  - 检查 `kubectl` 是否存在于 PATH。
  - 执行 `kubectl version --client`。
  - 执行 `kubectl get namespace <namespace>` 验证集群连接和 namespace 可访问性。
- 读取指定 Deployment 的 JSON 信息。
- 根据 `deployment.spec.selector.matchLabels` 自动查找关联 Pods。
- 检查 Deployment 当前副本状态和 rollout 状态。
- 检查 Pod phase、容器 ready、restartCount、waiting reason。
- 检查 Deployment 引用的 Secret / ConfigMap 是否存在。
- 自动查询 namespace 下的 Service，并判断 Service selector 是否匹配当前 Deployment 的 Pod labels。
- 输出匹配 Service 的 port / targetPort 信息。
- 对异常 Pod 收集 Events 和最近 50 行日志。
- 生成健康等级：
  - `健康`
  - `警告`
  - `严重`
  - `未知`
- 输出 Markdown 报告到终端，并保存到文件。

## 环境要求

- Python 3.8+
- 已安装 `kubectl`
- 当前 kubeconfig 已指向目标集群
- 当前用户具备相关资源读取权限：
  - Namespace
  - Deployment
  - Pod
  - Service
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

## 健康等级

`健康`：

- Deployment `availableReplicas == spec.replicas`
- Deployment `updatedReplicas == spec.replicas`
- 所有 Pod 都是 `Running`
- 所有普通容器都 `Ready=True`
- Secret / ConfigMap 都存在
- 没有历史错误 Events、restart warning 或 Service selector warning

`警告`：

- 当前核心资源健康
- 但存在历史错误 Events、`restartCount > 0` 且容器当前 ready，或没有匹配的 Service

`严重`：

- Deployment rollout 未完成
- Pod Pending
- `CreateContainerConfigError`
- `ImagePullBackOff` / `ErrImagePull`
- `CrashLoopBackOff`
- Secret / ConfigMap 缺失

`未知`：

- `kubectl` 不可用
- 无法访问集群或 namespace
- Deployment / Pod 等关键资源无法读取

## 报告结构

生成的 Markdown 报告包含以下部分：

- `Basic Info`
- `Preflight Check`
- `Current Status`
- `Deployment Status`
- `Pod Status`
- `Service Check`
- `Secret / ConfigMap Check`
- `Events Analysis`
- `Recent Logs`
- `Possible Root Cause`
- `Suggested Fix`

## Service 检查

工具会执行：

```bash
kubectl get services -n <namespace> -o json
```

然后根据每个 Service 的 selector 判断是否能匹配当前 Deployment Pod template labels。

如果找到匹配 Service，报告会展示：

- Service 名称
- Service 类型
- selector
- port
- targetPort
- protocol

如果没有匹配 Service，健康等级会进入 `警告`，报告中会提示：

```text
No Service selector matches the current Deployment pod template labels.
```

## 异常 Pod 日志

对异常 Pod，工具会执行：

```bash
kubectl logs <pod-name> -n <namespace> --tail=50
```

如果日志无法获取，程序不会退出，会把失败原因写入 `Recent Logs`。

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
kubectl version --client
kubectl get namespace <namespace>
kubectl get deployment <deployment> -n <namespace> -o json
kubectl get pods -n <namespace> -l key=value -o json
kubectl get services -n <namespace> -o json
kubectl get secret <secret-name> -n <namespace>
kubectl get configmap <configmap-name> -n <namespace>
kubectl describe pod <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace> --tail=50
```

如果某个 `kubectl` 命令失败，程序不会直接崩溃，而是把错误写入报告。

## 当前限制

- v0.2 只支持 Deployment 维度诊断。
- 只解析 `spec.selector.matchLabels`，暂不支持复杂 `matchExpressions` selector。
- Service 检查基于 selector 和 Deployment Pod template labels 匹配。
- `ContainerCreating` 是否卡住目前按 `waiting.reason` 识别，暂不根据持续时间判断。
- Events 分析基于常见错误关键词匹配，不替代人工排查。
- 工具只读取集群状态，不会自动修改 Kubernetes 资源。

## 开发说明

语法检查：

```bash
python -m py_compile kube_release_doctor.py
```

本项目目标是保持简单、可读、易于在受限环境中运行，因此 v0.2 不引入第三方依赖。
