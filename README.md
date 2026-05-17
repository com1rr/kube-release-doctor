# Kube Release Doctor

Kube Release Doctor 是一个 Kubernetes 发布故障诊断 CLI 工具。MVP v0.1 只使用 Python 标准库，通过 `subprocess` 调用 `kubectl` 获取 Deployment、Pod、Secret、ConfigMap 和异常 Pod Events 信息，并生成 Markdown 诊断报告。

## 功能

- 根据指定 namespace 和 deployment 采集 Deployment JSON。
- 根据 `deployment.spec.selector.matchLabels` 自动查询关联 Pods。
- 检查 Deployment 的 `replicas`、`availableReplicas`、`updatedReplicas`、`conditions`。
- 检查 Pod 的 `phase`、`containerStatuses`、`ready`、`restartCount`、`waiting.reason`。
- 识别常见异常：
  - `ImagePullBackOff`
  - `ErrImagePull`
  - `CrashLoopBackOff`
  - `CreateContainerConfigError`
  - `Pending`
  - `ContainerCreating`
- 从 Deployment 的 `env` / `envFrom` 中解析 Secret 和 ConfigMap 引用：
  - `secretKeyRef`
  - `configMapKeyRef`
  - `envFrom.secretRef`
  - `envFrom.configMapRef`
- 检查引用的 Secret / ConfigMap 是否存在。
- 对异常 Pod 执行 `kubectl describe pod`，提取 Events 中的错误关键词。
- 同时输出 Markdown 报告到终端和文件。

## 环境要求

- Python 3.8+
- 本机已安装 `kubectl`
- 当前 kubeconfig 有目标集群、namespace、Deployment、Pod、Secret、ConfigMap 的读取权限

不需要安装 Kubernetes Python Client，也不需要任何第三方 Python 包。

## 使用方法

```bash
python kube_release_doctor.py --namespace prod --deployment devops-cicd-demo
```

默认报告路径：

```text
reports/release-doctor-<namespace>-<deployment>.md
```

例如：

```text
reports/release-doctor-prod-devops-cicd-demo.md
```

指定输出路径：

```bash
python kube_release_doctor.py \
  --namespace prod \
  --deployment devops-cicd-demo \
  --output reports/prod-demo-release-report.md
```

Windows PowerShell 示例：

```powershell
python .\kube_release_doctor.py --namespace prod --deployment devops-cicd-demo
```

## 报告内容

生成的 Markdown 报告包含以下章节：

- Basic Info
- Deployment Status
- Pod Status
- Secret / ConfigMap Check
- Events Analysis
- Possible Root Cause
- Suggested Fix

## kubectl 命令

工具会自动执行类似以下命令：

```bash
kubectl get deployment <deployment> -n <namespace> -o json
kubectl get pods -n <namespace> -l key=value -o json
kubectl get secret <name> -n <namespace>
kubectl get configmap <name> -n <namespace>
kubectl describe pod <pod> -n <namespace>
```

如果某个 `kubectl` 命令失败，程序不会直接崩溃，而是把失败信息写入报告，便于继续查看已采集到的信息。

## 示例

```bash
python kube_release_doctor.py --namespace prod --deployment devops-cicd-demo
```

输出会打印到终端，并保存为：

```text
reports/release-doctor-prod-devops-cicd-demo.md
```

## 当前限制

- v0.1 只支持 Deployment 维度诊断。
- 只解析 `spec.selector.matchLabels`，暂不支持复杂 `matchExpressions` selector。
- `ContainerCreating` 是否“卡住”在 v0.1 中按 waiting reason 识别，暂不根据持续时间判断。
- Events 分析基于常见错误关键词匹配，不替代人工排查。
