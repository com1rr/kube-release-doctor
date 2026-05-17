# Kube Release Doctor

Kube Release Doctor 是一个轻量级 Kubernetes Deployment 发布诊断 CLI 工具。它通过本机 `kubectl` 读取集群状态，分析 Deployment、Pod、Service、Secret / ConfigMap、Events 和最近日志，并生成一份 Markdown 诊断报告。

当前版本仍然只使用 Python 标准库，不依赖 Kubernetes Python Client，也不依赖任何 Web 框架。

## 核心能力

- 执行 kubectl 环境预检：
  - 检查 `kubectl` 是否在 `PATH` 中。
  - 执行 `kubectl version --client`。
  - 执行 `kubectl get namespace <namespace>` 验证集群连接和 namespace 可访问性。
- 读取指定 Deployment 的 JSON 信息。
- 根据 `deployment.spec.selector.matchLabels` 自动查找关联 Pods。
- 检查 Deployment 当前副本状态和 rollout 状态。
- 检查 Pod phase、容器 ready、restartCount、waiting reason。
- 检查 Deployment 引用的 Secret / ConfigMap 是否存在。
- 自动查询 namespace 下的 Service，并判断 Service selector 是否匹配当前 Deployment 的 Pod template labels。
- 输出匹配 Service 的 selector、port、targetPort、protocol 信息。
- 对需要分析的 Pod 收集 Events。
- 对异常 Pod 尝试采集最近 50 行日志。
- 生成健康等级：`健康`、`警告`、`严重`、`未知`。
- 终端打印完整 Markdown 报告，并保存一份到文件。

## 环境要求

- Python 3.8+
- 已安装 `kubectl`
- 当前 kubeconfig 指向目标集群
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
python3 kube_release_doctor.py --namespace prod --deployment devops-cicd-demo
```

Windows PowerShell：

```powershell
python .\kube_release_doctor.py --namespace prod --deployment devops-cicd-demo
```

默认报告路径：

```text
reports/release-doctor-<namespace>-<deployment>.md
```

例如：

```text
reports/release-doctor-prod-devops-cicd-demo.md
```

## 指定输出文件

可以使用 `--output` 指定 Markdown 报告保存路径：

```bash
python3 kube_release_doctor.py \
  --namespace prod \
  --deployment devops-cicd-demo \
  --output examples/v0.2-healthy-report.md
```

Windows PowerShell：

```powershell
python .\kube_release_doctor.py `
  --namespace prod `
  --deployment devops-cicd-demo `
  --output examples\v0.2-healthy-report.md
```

如果输出路径中的目录不存在，脚本会自动创建目录。报告中的“报告路径”会显示最终实际保存路径。

## 命令行参数

| 参数 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--namespace` | 是 | 无 | Kubernetes namespace，例如 `prod` |
| `--deployment` | 是 | 无 | Deployment 名称，例如 `devops-cicd-demo` |
| `--output` | 否 | `reports/release-doctor-<namespace>-<deployment>.md` | Markdown 报告输出路径 |

查看帮助：

```bash
python3 kube_release_doctor.py --help
```

Windows PowerShell：

```powershell
python .\kube_release_doctor.py --help
```

## 健康等级

`健康`：

- Deployment `availableReplicas == spec.replicas`
- Deployment `updatedReplicas == spec.replicas`
- 所有 Pod 都是 `Running`
- 所有普通容器都 `Ready=True`
- Secret / ConfigMap 都存在
- 没有历史 Events 告警、restart warning 或 Service selector warning

`警告`：

- 当前核心资源健康
- 但存在历史 Events 告警、`restartCount > 0` 且容器当前 ready，或没有匹配的 Service

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

- `基本信息`
- `环境预检`
- `当前状态`
- `诊断摘要`
- `Deployment 状态`
- `Pod 状态`
- `Service 检查`
- `Secret / ConfigMap 检查`
- `Events 分析`
- `历史告警`
- `最近日志`
- `可能根因`
- `建议修复`

其中 `历史告警` 只会在健康或警告状态下发现历史 Events 告警时出现。

## 诊断摘要口径

诊断摘要会展示关键计数，例如：

- `Service 匹配数`
- `Secret / ConfigMap 缺失数`
- `历史告警 Events 数` 或 `当前故障 Events 数`
- `RestartCount 告警 Pod 数`
- `已尝试采集最近日志 Pod 数`

Events 计数会根据健康等级自动调整名称：

- 健康或警告场景：显示 `历史告警 Events 数`
- 当前故障或未知场景：显示 `当前故障 Events 数`

日志计数使用“已尝试采集”，因为容器可能尚未真正启动，`kubectl logs` 不一定能拿到应用输出。

## Service 检查

工具会执行：

```bash
kubectl get services -n <namespace> -o json
```

然后根据每个 Service 的 selector 判断是否能匹配当前 Deployment 的 Pod template labels。

如果找到匹配 Service，报告会展示判断结果：

```text
Service selector 可匹配当前 Deployment 的 Pod 标签。
```

并列出：

- Service 名称
- Service 类型
- selector
- port
- targetPort
- protocol

如果没有匹配 Service，报告会提示：

```text
未找到 selector 能匹配当前 Deployment Pod template labels 的 Service。
```

如果 Service 检查无法完成，报告会提示：

```text
Service 检查未能完成。
```

## Events 分析

工具会对需要分析的 Pod 执行：

```bash
kubectl describe pod <pod-name> -n <namespace>
```

并从 `Events:` 段落中匹配常见错误关键词。

在健康或警告场景中，命中的 Events 会被归类为：

```text
历史告警（Historical Warning）
```

在当前故障场景中，命中的 Events 会被归类为：

```text
当前故障分析（Current Failure Investigation）
```

## 异常 Pod 日志

对异常 Pod，工具会尝试执行：

```bash
kubectl logs <pod-name> -n <namespace> --tail=50
```

如果日志无法获取，程序不会退出，会把失败原因写入 `最近日志`。这通常发生在容器还没有真正启动、镜像拉取失败、配置错误导致容器未创建等场景。

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

- 当前只支持 Deployment 维度诊断。
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

本项目目标是保持简单、可读、易于在受限环境中运行，因此不引入第三方依赖。
