#!/usr/bin/env python3
"""
Kube Release Doctor v0.2

A small Kubernetes release diagnostic CLI that only uses the Python standard
library and shells out to kubectl.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


PROBLEM_REASONS = {
    "ImagePullBackOff",
    "ErrImagePull",
    "CrashLoopBackOff",
    "CreateContainerConfigError",
}

EVENT_ERROR_KEYWORDS = [
    "Failed",
    "Error",
    "BackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CrashLoopBackOff",
    "CreateContainerConfigError",
    "FailedScheduling",
    "FailedMount",
    "Unhealthy",
    "Warning",
]

HEALTHY = "Healthy"
WARNING = "Warning"
CRITICAL = "Critical"
UNKNOWN = "Unknown"

HEALTH_LEVEL_LABELS = {
    HEALTHY: "\u5065\u5eb7\uff08Healthy\uff09",
    WARNING: "\u8b66\u544a\uff08Warning\uff09",
    CRITICAL: "\u4e25\u91cd\u5f02\u5e38\uff08Critical\uff09",
    UNKNOWN: "\u672a\u77e5\uff08Unknown\uff09",
}


@dataclass
class CommandResult:
    command: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def command_text(self) -> str:
        return " ".join(self.command)


def run_kubectl(args: Sequence[str], timeout: int = 30) -> CommandResult:
    command = ["kubectl"] + list(args)
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout.strip(),
            stderr=completed.stderr.strip(),
        )
    except FileNotFoundError as exc:
        return CommandResult(command=command, returncode=127, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return CommandResult(
            command=command,
            returncode=124,
            stdout=stdout.strip() if isinstance(stdout, str) else "",
            stderr=(stderr.strip() if isinstance(stderr, str) else "") or "kubectl \u547d\u4ee4\u6267\u884c\u8d85\u65f6\u3002",
        )
    except Exception as exc:
        return CommandResult(command=command, returncode=1, stdout="", stderr=str(exc))


def make_skipped_result(command: Sequence[str], reason: str) -> CommandResult:
    return CommandResult(command=list(command), returncode=1, stdout="", stderr=reason)


def run_preflight_checks(namespace: str) -> Tuple[List[Dict[str, str]], bool]:
    checks: List[Dict[str, str]] = []
    kubectl_path = shutil.which("kubectl")

    if kubectl_path:
        checks.append(
            {
                "name": "kubectl binary",
                "status": "OK",
                "command": "shutil.which('kubectl')",
                "detail": kubectl_path,
            }
        )
    else:
        checks.append(
            {
                "name": "kubectl binary",
                "status": "Failed",
                "command": "shutil.which('kubectl')",
                "detail": "\u672a\u5728 PATH \u4e2d\u627e\u5230 kubectl\u3002\u8bf7\u5b89\u88c5 kubectl\uff0c\u6216\u5c06 kubectl \u52a0\u5165 PATH\u3002",
            }
        )
        return checks, False

    version_result = run_kubectl(["version", "--client"])
    checks.append(
        {
            "name": "kubectl client version",
            "status": "OK" if version_result.ok else "Failed",
            "command": version_result.command_text,
            "detail": version_result.stdout or version_result.stderr or "\u65e0\u8f93\u51fa\uff08No output\uff09",
        }
    )

    namespace_result = run_kubectl(["get", "namespace", namespace])
    checks.append(
        {
            "name": "cluster namespace access",
            "status": "OK" if namespace_result.ok else "Failed",
            "command": namespace_result.command_text,
            "detail": namespace_result.stdout or namespace_result.stderr or "\u65e0\u8f93\u51fa\uff08No output\uff09",
        }
    )

    return checks, version_result.ok and namespace_result.ok


def parse_json_result(result: CommandResult) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not result.ok:
        return None, result.stderr or "kubectl \u547d\u4ee4\u6267\u884c\u5931\u8d25\u3002"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"\u89e3\u6790 kubectl JSON \u8f93\u51fa\u5931\u8d25: {exc}"


def get_nested(data: Dict[str, Any], path: Sequence[str], default: Any = None) -> Any:
    current: Any = data
    for item in path:
        if not isinstance(current, dict) or item not in current:
            return default
        current = current[item]
    return current


def selector_to_label_arg(match_labels: Dict[str, Any]) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(match_labels.items()))


def selector_matches_labels(selector: Dict[str, Any], labels: Dict[str, Any]) -> bool:
    if not selector:
        return False
    for key, value in selector.items():
        if labels.get(key) != value:
            return False
    return True


def list_deployment_containers(deployment: Dict[str, Any]) -> List[Dict[str, Any]]:
    return get_nested(deployment, ["spec", "template", "spec", "containers"], []) or []


def extract_config_refs_from_container(container: Dict[str, Any]) -> Set[Tuple[str, str]]:
    refs: Set[Tuple[str, str]] = set()

    for env in container.get("env", []) or []:
        value_from = env.get("valueFrom") or {}
        secret_ref = value_from.get("secretKeyRef") or {}
        configmap_ref = value_from.get("configMapKeyRef") or {}
        if secret_ref.get("name"):
            refs.add(("secret", secret_ref["name"]))
        if configmap_ref.get("name"):
            refs.add(("configmap", configmap_ref["name"]))

    for env_from in container.get("envFrom", []) or []:
        secret_ref = env_from.get("secretRef") or {}
        configmap_ref = env_from.get("configMapRef") or {}
        if secret_ref.get("name"):
            refs.add(("secret", secret_ref["name"]))
        if configmap_ref.get("name"):
            refs.add(("configmap", configmap_ref["name"]))

    return refs


def extract_config_refs(deployment: Optional[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    if not deployment:
        return set()
    refs: Set[Tuple[str, str]] = set()
    for container in list_deployment_containers(deployment):
        refs.update(extract_config_refs_from_container(container))
    return refs


def check_config_refs(namespace: str, refs: Iterable[Tuple[str, str]]) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    for kind, name in sorted(refs):
        kubectl_kind = "secret" if kind == "secret" else "configmap"
        result = run_kubectl(["get", kubectl_kind, name, "-n", namespace])
        results.append(
            {
                "kind": kind,
                "name": name,
                "status": "Found" if result.ok else "Missing or inaccessible",
                "error": "" if result.ok else (result.stderr or "kubectl \u547d\u4ee4\u6267\u884c\u5931\u8d25\u3002"),
                "command": result.command_text,
            }
        )
    return results


def summarize_deployment(deployment: Optional[Dict[str, Any]], error: Optional[str]) -> Dict[str, Any]:
    if not deployment:
        return {"error": error or "Deployment \u6570\u636e\u4e0d\u53ef\u7528\u3002"}

    spec = deployment.get("spec") or {}
    status = deployment.get("status") or {}
    return {
        "replicas": spec.get("replicas", 0),
        "availableReplicas": status.get("availableReplicas", 0),
        "updatedReplicas": status.get("updatedReplicas", 0),
        "readyReplicas": status.get("readyReplicas", 0),
        "conditions": status.get("conditions", []) or [],
    }


def analyze_pod(pod: Dict[str, Any]) -> Dict[str, Any]:
    metadata = pod.get("metadata") or {}
    status = pod.get("status") or {}
    pod_name = metadata.get("name", "<unknown>")
    phase = status.get("phase", "Unknown")
    container_statuses = status.get("containerStatuses", []) or []
    init_container_statuses = status.get("initContainerStatuses", []) or []

    problems: List[str] = []
    warnings: List[str] = []
    containers: List[Dict[str, Any]] = []

    if phase == "Pending":
        problems.append("Pending")

    all_statuses = [(item, True) for item in init_container_statuses] + [(item, False) for item in container_statuses]
    for item, is_init in all_statuses:
        state = item.get("state") or {}
        waiting = state.get("waiting") or {}
        waiting_reason = waiting.get("reason") or ""
        ready = item.get("ready", False)
        restart_count = item.get("restartCount", 0)

        if waiting_reason in PROBLEM_REASONS:
            problems.append(waiting_reason)
        if waiting_reason == "ContainerCreating":
            problems.append("ContainerCreating")
        if restart_count and restart_count > 0:
            if ready:
                warnings.append(f"RestartCount={restart_count}, container currently ready")
            else:
                problems.append(f"RestartCount={restart_count}, container not ready")

        containers.append(
            {
                "name": item.get("name", "<unknown>"),
                "ready": ready,
                "restartCount": restart_count,
                "waitingReason": waiting_reason,
                "waitingMessage": waiting.get("message", ""),
                "isInit": is_init,
            }
        )

    return {
        "name": pod_name,
        "phase": phase,
        "containers": containers,
        "problems": sorted(set(problems)),
        "warnings": sorted(set(warnings)),
        "is_abnormal": bool(problems),
    }


def get_pod_summaries(pods_data: Optional[Dict[str, Any]], error: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not pods_data:
        return [], error or "Pod \u6570\u636e\u4e0d\u53ef\u7528\u3002"
    pods = pods_data.get("items", []) or []
    return [analyze_pod(pod) for pod in pods], None


def analyze_services(
    namespace: str,
    deployment: Optional[Dict[str, Any]],
    pod_summaries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    result = run_kubectl(["get", "services", "-n", namespace, "-o", "json"])
    services_data, error = parse_json_result(result)
    analysis: Dict[str, Any] = {
        "command": result.command_text,
        "ok": result.ok and error is None,
        "error": error or "",
        "matched": [],
        "unmatched": [],
        "warning": "",
    }

    if not analysis["ok"] or not services_data:
        analysis["warning"] = "Service \u68c0\u67e5\u672a\u80fd\u5b8c\u6210\u3002"
        return analysis

    deployment_labels = get_nested(deployment or {}, ["spec", "template", "metadata", "labels"], {}) or {}
    pod_names = {pod["name"] for pod in pod_summaries}
    services = services_data.get("items", []) or []

    for service in services:
        metadata = service.get("metadata") or {}
        spec = service.get("spec") or {}
        selector = spec.get("selector") or {}
        ports = spec.get("ports", []) or []
        service_info = {
            "name": metadata.get("name", "<unknown>"),
            "type": spec.get("type", ""),
            "selector": selector,
            "ports": ports,
            "matches": selector_matches_labels(selector, deployment_labels),
        }
        if service_info["matches"]:
            analysis["matched"].append(service_info)
        else:
            analysis["unmatched"].append(service_info)

    if not analysis["matched"] and pod_names:
        analysis["warning"] = "\u672a\u627e\u5230 selector \u80fd\u5339\u914d\u5f53\u524d Deployment Pod template labels \u7684 Service\u3002"

    return analysis


def collect_recent_logs(namespace: str, pod_summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    logs: List[Dict[str, Any]] = []
    for pod in pod_summaries:
        if not pod.get("is_abnormal"):
            continue
        result = run_kubectl(["logs", pod["name"], "-n", namespace, "--tail=50"], timeout=45)
        logs.append(
            {
                "pod": pod["name"],
                "command": result.command_text,
                "ok": result.ok,
                "log": result.stdout,
                "error": "" if result.ok else (result.stderr or "kubectl logs \u6267\u884c\u5931\u8d25\u3002"),
            }
        )
    return logs


def extract_events_section(describe_text: str) -> str:
    lines = describe_text.splitlines()
    start_index: Optional[int] = None
    for index, line in enumerate(lines):
        if line.strip() == "Events:":
            start_index = index
            break
    if start_index is None:
        return ""
    return "\n".join(lines[start_index:])


def extract_error_event_lines(events_text: str) -> List[str]:
    matches: List[str] = []
    for line in events_text.splitlines():
        if any(keyword in line for keyword in EVENT_ERROR_KEYWORDS):
            matches.append(line.rstrip())
    return matches


def is_currently_healthy(
    deployment_summary: Dict[str, Any],
    pod_summaries: List[Dict[str, Any]],
    pod_error: Optional[str],
    config_checks: List[Dict[str, str]],
) -> bool:
    if deployment_summary.get("error") or pod_error:
        return False

    replicas = deployment_summary.get("replicas")
    available = deployment_summary.get("availableReplicas")
    if not isinstance(replicas, int) or not isinstance(available, int):
        return False
    if available != replicas:
        return False

    if replicas > 0 and not pod_summaries:
        return False

    if any(item["status"] != "Found" for item in config_checks):
        return False

    for pod in pod_summaries:
        if pod.get("phase") != "Running":
            return False
        for container in pod.get("containers", []) or []:
            if container.get("isInit"):
                continue
            if container.get("ready") is not True:
                return False

    return True


def collect_event_warning_lines(event_analyses: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    warnings: List[Tuple[str, str]] = []
    for analysis in event_analyses:
        for line in analysis.get("error_lines", []):
            warnings.append((analysis["pod"], line.strip()))
    return warnings


def has_restart_warnings(pod_summaries: List[Dict[str, Any]]) -> bool:
    return any(pod.get("warnings") for pod in pod_summaries)


def format_health_level(health_level: str) -> str:
    return HEALTH_LEVEL_LABELS.get(health_level, f"{health_level}\uff08{health_level}\uff09")


def describe_current_status(
    health_level: str,
    pod_summaries: List[Dict[str, Any]],
    event_analyses: List[Dict[str, Any]],
    service_analysis: Dict[str, Any],
) -> List[str]:
    historical_events = any(analysis.get("error_lines") for analysis in event_analyses)
    restart_warnings = has_restart_warnings(pod_summaries)
    service_warning = bool(service_analysis.get("warning"))

    if health_level == HEALTHY:
        return [
            "\u5f53\u524d\u8d44\u6e90\u5b8c\u5168\u6b63\u5e38\uff0c\u672a\u53d1\u73b0\u660e\u663e\u5386\u53f2\u544a\u8b66\u3002",
        ]

    if health_level == WARNING:
        messages = [
            "\u5f53\u524d\u53d1\u5e03\u8d44\u6e90\u662f\u5065\u5eb7\u7684\uff0c\u4f46\u5b58\u5728\u9700\u8981\u5173\u6ce8\u7684\u544a\u8b66\u4fe1\u53f7\u3002",
        ]
        if historical_events:
            messages.append("\u5f53\u524d\u53d1\u5e03\u662f\u5065\u5eb7\u7684\uff0c\u4f46\u53d1\u73b0\u5386\u53f2 Events \u544a\u8b66\u3002")
        if restart_warnings:
            messages.append("\u4e00\u4e2a\u6216\u591a\u4e2a container \u7684 RestartCount > 0\uff0c\u4f46\u5f53\u524d\u5904\u4e8e Ready \u72b6\u6001\u3002")
        if service_warning:
            messages.append(str(service_analysis["warning"]))
        return unique_preserve_order(messages)

    if health_level == CRITICAL:
        return [
            "\u68c0\u6d4b\u5230\u660e\u786e\u7684\u5f53\u524d\u53d1\u5e03\u6545\u969c\u3002",
            "\u8bf7\u91cd\u70b9\u68c0\u67e5 rollout \u72b6\u6001\u3001Pod \u72b6\u6001\u3001\u955c\u50cf\u62c9\u53d6\u72b6\u6001\uff0c\u4ee5\u53ca Secret / ConfigMap \u5f15\u7528\u3002",
        ]

    return [
        "\u65e0\u6cd5\u5224\u65ad\u5f53\u524d\u53d1\u5e03\u72b6\u6001\uff0c\u901a\u5e38\u662f\u56e0\u4e3a kubectl \u4e0d\u53ef\u7528\u3001\u96c6\u7fa4\u4e0d\u53ef\u8fbe\u3001\u8d44\u6e90\u4e0d\u5b58\u5728\u6216\u6743\u9650\u4e0d\u8db3\u3002",
    ]


def evaluate_health_level(
    preflight_ok: bool,
    deployment_summary: Dict[str, Any],
    pod_summaries: List[Dict[str, Any]],
    pod_error: Optional[str],
    config_checks: List[Dict[str, str]],
    event_analyses: List[Dict[str, Any]],
    service_analysis: Dict[str, Any],
) -> str:
    if not preflight_ok or deployment_summary.get("error") or pod_error:
        return UNKNOWN

    missing_refs = any(item["status"] != "Found" for item in config_checks)
    pod_problems: Set[str] = set()
    for pod in pod_summaries:
        pod_problems.update(pod.get("problems", []))

    replicas = deployment_summary.get("replicas")
    available = deployment_summary.get("availableReplicas")
    updated = deployment_summary.get("updatedReplicas")
    rollout_incomplete = False
    if isinstance(replicas, int) and replicas > 0:
        rollout_incomplete = available != replicas or updated != replicas

    critical_reasons = {
        "Pending",
        "CreateContainerConfigError",
        "ImagePullBackOff",
        "ErrImagePull",
        "CrashLoopBackOff",
    }
    if rollout_incomplete or missing_refs or pod_problems.intersection(critical_reasons):
        return CRITICAL

    current_healthy = is_currently_healthy(deployment_summary, pod_summaries, pod_error, config_checks)
    historical_events = any(analysis.get("error_lines") for analysis in event_analyses)
    service_warning = bool(service_analysis.get("warning"))
    if current_healthy and (historical_events or has_restart_warnings(pod_summaries) or service_warning):
        return WARNING
    if current_healthy:
        return HEALTHY

    return CRITICAL


def analyze_events(namespace: str, pod_summaries: List[Dict[str, Any]], include_healthy_pods: bool = False) -> List[Dict[str, Any]]:
    analyses: List[Dict[str, Any]] = []
    for pod in pod_summaries:
        if not include_healthy_pods and not pod.get("is_abnormal"):
            continue
        result = run_kubectl(["describe", "pod", pod["name"], "-n", namespace], timeout=45)
        events_text = extract_events_section(result.stdout) if result.ok else ""
        analyses.append(
            {
                "pod": pod["name"],
                "command": result.command_text,
                "ok": result.ok,
                "error": "" if result.ok else (result.stderr or "kubectl \u547d\u4ee4\u6267\u884c\u5931\u8d25\u3002"),
                "events": events_text,
                "error_lines": extract_error_event_lines(events_text),
            }
        )
    return analyses


def infer_root_causes(
    deployment_summary: Dict[str, Any],
    pod_summaries: List[Dict[str, Any]],
    config_checks: List[Dict[str, str]],
    event_analyses: List[Dict[str, Any]],
    service_analysis: Dict[str, Any],
    health_level: str,
) -> List[str]:
    causes: List[str] = []

    if health_level == UNKNOWN:
        causes.append("\u65e0\u6cd5\u5224\u65ad\u5f53\u524d\u53d1\u5e03\u72b6\u6001\uff1akubectl \u4e0d\u53ef\u7528\u3001\u96c6\u7fa4\u4e0d\u53ef\u8fbe\u3001\u8d44\u6e90\u4e0d\u5b58\u5728\u6216\u6743\u9650\u4e0d\u8db3\u3002")
        return causes

    if health_level in {HEALTHY, WARNING}:
        historical_warnings_found = any(analysis.get("error_lines") for analysis in event_analyses)
        restart_warnings_found = has_restart_warnings(pod_summaries)
        service_warning = service_analysis.get("warning")
        if health_level == WARNING:
            causes.append(
                "\u672a\u68c0\u6d4b\u5230\u5f53\u524d\u53d1\u5e03\u5931\u8d25\uff1b\u5f53\u524d\u8d44\u6e90\u5065\u5eb7\uff0c\u4f46\u5b58\u5728\u9700\u8981\u5173\u6ce8\u7684\u544a\u8b66\u4fe1\u53f7\u3002"
            )
            if historical_warnings_found:
                causes.append("\u53d1\u73b0\u5386\u53f2 Events \u544a\u8b66\uff0c\u4f46\u5f53\u524d Deployment / Pod / Secret / ConfigMap \u72b6\u6001\u5065\u5eb7\u3002")
            if restart_warnings_found:
                causes.append("\u4e00\u4e2a\u6216\u591a\u4e2a container \u7684 RestartCount > 0\uff0c\u4f46\u5f53\u524d\u5904\u4e8e Ready \u72b6\u6001\u3002")
            if service_warning:
                causes.append(str(service_warning))
        else:
            causes.append("\u672a\u68c0\u6d4b\u5230\u5f53\u524d\u53d1\u5e03\u6545\u969c\uff1b\u5f53\u524d\u8d44\u6e90\u72b6\u6001\u5065\u5eb7\u3002")
        return causes

    if deployment_summary.get("error"):
        causes.append("\u65e0\u6cd5\u91c7\u96c6 Deployment \u4fe1\u606f\uff0c\u8bf7\u68c0\u67e5 namespace\u3001Deployment \u540d\u79f0\u548c kubectl \u8bbf\u95ee\u6743\u9650\u3002")

    replicas = deployment_summary.get("replicas")
    available = deployment_summary.get("availableReplicas")
    updated = deployment_summary.get("updatedReplicas")
    if isinstance(replicas, int) and replicas > 0:
        if not isinstance(available, int) or available < replicas:
            causes.append("Deployment availableReplicas \u5c0f\u4e8e desiredReplicas\uff0crollout \u672a\u8fbe\u5230\u671f\u671b\u72b6\u6001\u3002")
        if not isinstance(updated, int) or updated < replicas:
            causes.append("Deployment updatedReplicas \u5c0f\u4e8e desiredReplicas\uff0crollout \u53ef\u80fd\u672a\u5b8c\u6210\u3002")

    missing_refs = [item for item in config_checks if item["status"] != "Found"]
    if missing_refs:
        names = ", ".join(f"{item['kind']}/{item['name']}" for item in missing_refs)
        causes.append(f"Deployment \u5f15\u7528\u7684 Secret / ConfigMap \u7f3a\u5931\u6216\u65e0\u6743\u8bbf\u95ee: {names}\u3002")

    reasons: Set[str] = set()
    for pod in pod_summaries:
        reasons.update(pod.get("problems", []))

    if "ImagePullBackOff" in reasons or "ErrImagePull" in reasons:
        causes.append("\u5b58\u5728 ImagePullBackOff / ErrImagePull\uff0c\u53ef\u80fd\u662f\u955c\u50cf\u540d\u79f0\u3001tag\u3001registry \u51ed\u636e\u6216\u7f51\u7edc\u8bbf\u95ee\u5f02\u5e38\u3002")
    if "CrashLoopBackOff" in reasons:
        causes.append("\u5b58\u5728 CrashLoopBackOff\uff0ccontainer \u542f\u52a8\u540e\u53cd\u590d\u5d29\u6e83\uff0c\u53ef\u80fd\u4e0e\u542f\u52a8\u547d\u4ee4\u3001\u914d\u7f6e\u6216\u4f9d\u8d56\u672a\u5c31\u7eea\u6709\u5173\u3002")
    if "CreateContainerConfigError" in reasons:
        causes.append("\u5b58\u5728 CreateContainerConfigError\uff0c\u5e38\u89c1\u539f\u56e0\u662f secretKeyRef / configMapKeyRef / envFrom \u5f15\u7528\u7684 Secret\u3001ConfigMap \u6216 key \u7f3a\u5931\u3002")
    if "Pending" in reasons:
        causes.append("\u5b58\u5728 Pending Pod\uff0c\u53ef\u80fd\u662f\u8c03\u5ea6\u3001\u8d44\u6e90\u914d\u989d\u3001\u8282\u70b9\u5bb9\u91cf\u3001PVC \u6216\u955c\u50cf\u62c9\u53d6\u524d\u7f6e\u6761\u4ef6\u963b\u585e\u3002")
    if "ContainerCreating" in reasons:
        causes.append("Pod \u5904\u4e8e ContainerCreating\uff0c\u53ef\u80fd\u4e0e volume mount\u3001image pull\u3001CNI \u6216\u8282\u70b9 runtime \u6709\u5173\u3002")

    for analysis in event_analyses:
        for line in analysis.get("error_lines", []):
            if "FailedScheduling" in line:
                causes.append("Events \u4e2d\u51fa\u73b0 FailedScheduling\uff0c\u8bf4\u660e scheduler \u672a\u80fd\u6210\u529f\u8c03\u5ea6 Pod\u3002")
            if "FailedMount" in line:
                causes.append("Events \u4e2d\u51fa\u73b0 FailedMount\uff0c\u8bf4\u660e Pod volume mount \u5931\u8d25\u3002")
            if "Unhealthy" in line:
                causes.append("Events \u4e2d\u51fa\u73b0 Unhealthy\uff0c\u53ef\u80fd\u662f readiness / liveness probe \u5931\u8d25\u3002")

    if service_analysis.get("warning"):
        causes.append(str(service_analysis["warning"]))

    if not causes:
        causes.append("\u57fa\u4e8e\u5df2\u91c7\u96c6\u7684 Deployment\u3001Pod\u3001Secret\u3001ConfigMap \u548c Events \u4fe1\u606f\uff0c\u6682\u672a\u53d1\u73b0\u660e\u786e\u6839\u56e0\u3002")

    return unique_preserve_order(causes)


def suggest_fixes(
    root_causes: List[str],
    config_checks: List[Dict[str, str]],
    health_level: str,
    namespace: str,
) -> List[str]:
    if health_level == HEALTHY:
        return [
            "\u5f53\u524d\u65e0\u9700\u7acb\u5373\u4fee\u590d\u3002Deployment\u3001Pod\u3001Secret\u3001ConfigMap \u548c Service \u68c0\u67e5\u672a\u53d1\u73b0\u5f53\u524d\u6545\u969c\u3002"
        ]
    if health_level == WARNING:
        return [
            "\u5f53\u524d\u65e0\u9700\u7acb\u5373\u4fee\u590d\u3002\u53ef\u6839\u636e\u9700\u8981\u590d\u6838\u5386\u53f2 Events\u3001RestartCount \u6216 Service selector \u5339\u914d\u60c5\u51b5\u3002"
        ]
    if health_level == UNKNOWN:
        return [
            "\u8bf7\u68c0\u67e5 kubectl \u662f\u5426\u5df2\u5b89\u88c5\u3001kubeconfig \u662f\u5426\u6307\u5411\u6b63\u786e\u96c6\u7fa4\uff0c\u5e76\u786e\u8ba4 namespace / Deployment \u5b58\u5728\u4e14\u5f53\u524d\u7528\u6237\u5177\u5907\u8bfb\u53d6\u6743\u9650\u3002"
        ]

    fixes: List[str] = []
    text = "\n".join(root_causes)
    missing_refs = [item for item in config_checks if item["status"] != "Found"]

    if "Secret or ConfigMap" in text or missing_refs:
        fixes.append("\u521b\u5efa\u7f3a\u5931\u7684 Secret / ConfigMap\uff0c\u6216\u4fee\u6b63 Deployment \u4e2d env / envFrom \u5f15\u7528\u7684\u540d\u79f0\u548c key\u3002")
        fixes.append("\u4ee5\u4e0b\u547d\u4ee4\u4e2d `<KEY>` \u548c `<VALUE>` \u662f\u5360\u4f4d\u7b26\uff0c\u6267\u884c\u524d\u8bf7\u66ff\u6362\u4e3a\u5e94\u7528\u771f\u5b9e\u9700\u8981\u7684\u914d\u7f6e\u503c\u3002")
        for item in missing_refs:
            if item["kind"] == "secret":
                fixes.append(
                    "\u7f3a\u5931 Secret `{name}` \u7684\u4fee\u590d\u547d\u4ee4\u793a\u4f8b:\n\n"
                    "```bash\n"
                    "kubectl create secret generic {name} \\\n"
                    "  --from-literal=<KEY>=<VALUE> \\\n"
                    "  -n {namespace}\n"
                    "```".format(name=item["name"], namespace=namespace)
                )
            elif item["kind"] == "configmap":
                fixes.append(
                    "\u7f3a\u5931 ConfigMap `{name}` \u7684\u4fee\u590d\u547d\u4ee4\u793a\u4f8b:\n\n"
                    "```bash\n"
                    "kubectl create configmap {name} \\\n"
                    "  --from-literal=<KEY>=<VALUE> \\\n"
                    "  -n {namespace}\n"
                    "```".format(name=item["name"], namespace=namespace)
                )
    if "ImagePullBackOff" in text or "ErrImagePull" in text:
        fixes.append("\u68c0\u67e5\u955c\u50cf\u4ed3\u5e93\u3001tag\u3001imagePullSecrets\u3001\u955c\u50cf\u4ed3\u5e93\u6743\u9650\uff0c\u4ee5\u53ca\u8282\u70b9\u5230\u955c\u50cf\u4ed3\u5e93\u7684\u7f51\u7edc\u8fde\u901a\u6027\u3002")
    if "CrashLoopBackOff" in text:
        fixes.append("\u67e5\u770b container logs\uff0c\u68c0\u67e5\u542f\u52a8\u547d\u4ee4\u3001\u5e94\u7528\u914d\u7f6e\u3001\u6570\u636e\u5e93 / Service \u4f9d\u8d56\u548c\u8d44\u6e90\u9650\u5236\u3002")
    if "FailedScheduling" in text or "Pending" in text:
        fixes.append("\u68c0\u67e5\u8282\u70b9\u8d44\u6e90\u3001taints / tolerations\u3001affinity\u3001resource requests\u3001PVC \u72b6\u6001\u548c namespace quota\u3002")
    if "ContainerCreating" in text or "FailedMount" in text:
        fixes.append("\u68c0\u67e5 volume mounts\u3001PVC / PV \u7ed1\u5b9a\u3001CSI driver\u3001CNI \u72b6\u6001\uff0c\u4ee5\u53ca\u76ee\u6807\u8282\u70b9\u4e0a\u7684 kubelet events\u3002")
    if "Unhealthy" in text:
        fixes.append("\u590d\u6838 readiness / liveness probe \u7684 path\u3001port\u3001timeout\u3001initialDelaySeconds\uff0c\u4ee5\u53ca\u5e94\u7528\u5065\u5eb7\u68c0\u67e5\u63a5\u53e3\u884c\u4e3a\u3002")

    if not fixes:
        fixes.append("\u53ef\u7ee7\u7eed\u6267\u884c kubectl rollout status\u3001kubectl describe deployment\u3001kubectl describe pod \u548c kubectl logs \u505a\u8fdb\u4e00\u6b65\u6392\u67e5\u3002")

    return unique_preserve_order(fixes)


def unique_preserve_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def md_escape(value: Any) -> str:
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def format_preflight_status(status: str) -> str:
    if status == "OK":
        return "\u901a\u8fc7\uff08OK\uff09"
    if status == "Failed":
        return "\u5931\u8d25\uff08Failed\uff09"
    return status


def format_preflight_name(name: str) -> str:
    names = {
        "kubectl binary": "kubectl \u53ef\u6267\u884c\u6587\u4ef6",
        "kubectl client version": "kubectl \u5ba2\u6237\u7aef\u7248\u672c",
        "cluster namespace access": "\u96c6\u7fa4 namespace \u8bbf\u95ee",
    }
    return names.get(name, name)


def format_config_status(status: str) -> str:
    if status == "Found":
        return "\u5b58\u5728\uff08Found\uff09"
    if status == "Missing or inaccessible":
        return "\u7f3a\u5931\u6216\u65e0\u6743\u8bbf\u95ee\uff08Missing or inaccessible\uff09"
    return status


def format_conditions(conditions: List[Dict[str, Any]]) -> str:
    if not conditions:
        return "_\u672a\u8fd4\u56de Conditions\u3002_"
    lines = ["| \u7c7b\u578b\uff08Type\uff09 | \u72b6\u6001\uff08Status\uff09 | \u539f\u56e0\uff08Reason\uff09 | \u6d88\u606f\uff08Message\uff09 |", "| --- | --- | --- | --- |"]
    for condition in conditions:
        lines.append(
            "| {type} | {status} | {reason} | {message} |".format(
                type=md_escape(condition.get("type", "")),
                status=md_escape(condition.get("status", "")),
                reason=md_escape(condition.get("reason", "")),
                message=md_escape(condition.get("message", "")),
            )
        )
    return "\n".join(lines)


def build_diagnosis_summary(
    health_level: str,
    deployment_summary: Dict[str, Any],
    pod_summaries: List[Dict[str, Any]],
    service_analysis: Dict[str, Any],
    config_checks: List[Dict[str, str]],
    event_analyses: List[Dict[str, Any]],
    recent_logs: List[Dict[str, Any]],
) -> List[str]:
    abnormal_pods = [pod for pod in pod_summaries if pod.get("is_abnormal")]
    restart_warning_count = sum(1 for pod in pod_summaries if pod.get("warnings"))
    missing_refs = [item for item in config_checks if item["status"] != "Found"]
    event_error_count = sum(len(analysis.get("error_lines", [])) for analysis in event_analyses)
    event_error_label = "\u5386\u53f2\u544a\u8b66 Events \u6570" if health_level in {HEALTHY, WARNING} else "\u5f53\u524d\u6545\u969c Events \u6570"
    matched_services = service_analysis.get("matched", []) or []

    lines = [
        f"- \u6574\u4f53\u72b6\u6001\uff1a{format_health_level(health_level)}",
        f"- Deployment desiredReplicas: `{deployment_summary.get('replicas', 'N/A')}`",
        f"- Deployment availableReplicas: `{deployment_summary.get('availableReplicas', 'N/A')}`",
        f"- Deployment updatedReplicas: `{deployment_summary.get('updatedReplicas', 'N/A')}`",
        f"- Pod \u603b\u6570: `{len(pod_summaries)}`\uff0c\u5f02\u5e38 Pod: `{len(abnormal_pods)}`",
        f"- Service \u5339\u914d\u6570: `{len(matched_services)}`",
        f"- Secret / ConfigMap \u7f3a\u5931\u6570: `{len(missing_refs)}`",
        f"- {event_error_label}: `{event_error_count}`",
        f"- RestartCount \u544a\u8b66 Pod \u6570: `{restart_warning_count}`",
        f"- \u5df2\u91c7\u96c6\u6700\u8fd1\u65e5\u5fd7 Pod \u6570: `{len(recent_logs)}`",
    ]
    return lines


def build_report(
    namespace: str,
    deployment_name: str,
    output_path: str,
    preflight_checks: List[Dict[str, str]],
    deployment_result: CommandResult,
    pod_result: Optional[CommandResult],
    selector: str,
    deployment_summary: Dict[str, Any],
    pod_summaries: List[Dict[str, Any]],
    pod_error: Optional[str],
    service_analysis: Dict[str, Any],
    config_checks: List[Dict[str, str]],
    event_analyses: List[Dict[str, Any]],
    recent_logs: List[Dict[str, Any]],
    health_level: str,
    root_causes: List[str],
    suggested_fixes: List[str],
) -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: List[str] = [
        "# Kube Release Doctor \u8bca\u65ad\u62a5\u544a",
        "",
        "## \u57fa\u672c\u4fe1\u606f",
        "",
        f"- Namespace: `{namespace}`",
        f"- Deployment: `{deployment_name}`",
        f"- \u751f\u6210\u65f6\u95f4: `{now}`",
        f"- \u62a5\u544a\u8def\u5f84: `{output_path}`",
        "",
        "### kubectl \u547d\u4ee4",
        "",
        f"- `{deployment_result.command_text}`",
    ]

    if pod_result:
        lines.append(f"- `{pod_result.command_text}`")
    if not deployment_result.ok:
        lines.extend(["", f"> Deployment \u547d\u4ee4\u6267\u884c\u5931\u8d25: `{deployment_result.stderr or 'unknown error'}`"])
    if pod_result and not pod_result.ok:
        lines.extend(["", f"> Pod \u547d\u4ee4\u6267\u884c\u5931\u8d25: `{pod_result.stderr or 'unknown error'}`"])

    lines.extend(["", "## \u73af\u5883\u9884\u68c0", ""])
    lines.extend(["| \u68c0\u67e5\u9879 | \u72b6\u6001 | \u547d\u4ee4 | \u8be6\u60c5 |", "| --- | --- | --- | --- |"])
    for check in preflight_checks:
        lines.append(
            "| {name} | {status} | `{command}` | {detail} |".format(
                name=md_escape(format_preflight_name(check.get("name", ""))),
                status=md_escape(format_preflight_status(check.get("status", ""))),
                command=md_escape(check.get("command", "")),
                detail=md_escape(check.get("detail", "")),
            )
        )

    lines.extend(["", "## \u5f53\u524d\u72b6\u6001", ""])
    lines.append(f"- \u6574\u4f53\u72b6\u6001\uff1a{format_health_level(health_level)}")
    for message in describe_current_status(health_level, pod_summaries, event_analyses, service_analysis):
        lines.append(f"- {message}")

    lines.extend(["", "## \u8bca\u65ad\u6458\u8981", ""])
    lines.extend(build_diagnosis_summary(
        health_level,
        deployment_summary,
        pod_summaries,
        service_analysis,
        config_checks,
        event_analyses,
        recent_logs,
    ))

    lines.extend(["", "## Deployment \u72b6\u6001", ""])
    if deployment_summary.get("error"):
        lines.append(f"- \u9519\u8bef: `{deployment_summary['error']}`")
    else:
        lines.extend(
            [
                f"- desiredReplicas: `{deployment_summary.get('replicas', 0)}`",
                f"- availableReplicas: `{deployment_summary.get('availableReplicas', 0)}`",
                f"- updatedReplicas: `{deployment_summary.get('updatedReplicas', 0)}`",
                f"- readyReplicas: `{deployment_summary.get('readyReplicas', 0)}`",
                f"- Pod Selector: `{selector or 'N/A'}`",
                "",
                "### Conditions",
                "",
                format_conditions(deployment_summary.get("conditions", []) or []),
            ]
        )

    lines.extend(["", "## Pod \u72b6\u6001", ""])
    if pod_error:
        lines.append(f"- \u9519\u8bef: `{pod_error}`")
    elif not pod_summaries:
        lines.append("_\u672a\u627e\u5230\u5339\u914d Deployment selector \u7684 Pod\u3002_")
    else:
        lines.extend(
            [
                "| Pod | Phase | Container | Ready | \u91cd\u542f\u6b21\u6570\uff08Restarts\uff09 | \u7b49\u5f85\u539f\u56e0\uff08Waiting Reason\uff09 | \u95ee\u9898\uff08Problems\uff09 | \u544a\u8b66\uff08Warnings\uff09 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for pod in pod_summaries:
            containers = pod.get("containers", []) or [{"name": "", "ready": "", "restartCount": "", "waitingReason": "", "isInit": False}]
            for container in containers:
                container_name = container.get("name", "")
                if container.get("isInit"):
                    container_name = f"init:{container_name}"
                lines.append(
                    "| {pod} | {phase} | {container} | {ready} | {restarts} | {reason} | {problems} | {warnings} |".format(
                        pod=md_escape(pod["name"]),
                        phase=md_escape(pod["phase"]),
                        container=md_escape(container_name),
                        ready=md_escape(container.get("ready", "")),
                        restarts=md_escape(container.get("restartCount", "")),
                        reason=md_escape(container.get("waitingReason", "")),
                        problems=md_escape(", ".join(pod.get("problems", [])) or "-"),
                        warnings=md_escape(", ".join(pod.get("warnings", [])) or "-"),
                    )
                )

    lines.extend(["", "## Service \u68c0\u67e5", ""])
    lines.append(f"- \u547d\u4ee4: `{service_analysis.get('command', 'N/A')}`")
    if not service_analysis.get("ok"):
        service_error = service_analysis.get("error") or service_analysis.get("warning") or "Service \u68c0\u67e5\u5931\u8d25\u3002"
        lines.append(f"- \u9519\u8bef: `{service_error}`")
    else:
        matched = service_analysis.get("matched", []) or []
        if service_analysis.get("warning"):
            lines.append(f"- \u544a\u8b66: {service_analysis['warning']}")
        if not matched:
            lines.append("_\u672a\u627e\u5230\u5339\u914d Deployment Pod template labels \u7684 Service\u3002_")
        else:
            lines.extend(
                [
                    "",
                    "| Service | \u7c7b\u578b\uff08Type\uff09 | Selector | Ports |",
                    "| --- | --- | --- | --- |",
                ]
            )
            for service in matched:
                selector_text = ",".join(f"{key}={value}" for key, value in sorted((service.get("selector") or {}).items()))
                port_texts = []
                for port in service.get("ports", []) or []:
                    port_texts.append(
                        "name={name}, port={port}, targetPort={target}, protocol={protocol}".format(
                            name=port.get("name", "-"),
                            port=port.get("port", "-"),
                            target=port.get("targetPort", "-"),
                            protocol=port.get("protocol", "-"),
                        )
                    )
                lines.append(
                    "| {name} | {type} | {selector} | {ports} |".format(
                        name=md_escape(service.get("name", "")),
                        type=md_escape(service.get("type", "")),
                        selector=md_escape(selector_text or "-"),
                        ports=md_escape("; ".join(port_texts) or "-"),
                    )
                )

    lines.extend(["", "## Secret / ConfigMap \u68c0\u67e5", ""])
    if not config_checks:
        lines.append("_Deployment env / envFrom \u4e2d\u672a\u53d1\u73b0 Secret \u6216 ConfigMap \u5f15\u7528\u3002_")
    else:
        lines.extend(["| \u7c7b\u578b | \u540d\u79f0 | \u72b6\u6001\uff08Status\uff09 | \u9519\u8bef |", "| --- | --- | --- | --- |"])
        for item in config_checks:
            lines.append(
                f"| {md_escape(item['kind'])} | {md_escape(item['name'])} | {md_escape(format_config_status(item['status']))} | {md_escape(item['error'] or '-')} |"
            )

    lines.extend(["", "## Events \u5206\u6790", ""])
    if not event_analyses:
        lines.append("_\u6ca1\u6709\u9700\u8981\u5206\u6790 Events \u7684 Pod\uff0c\u6216 Pod \u6570\u636e\u4e0d\u53ef\u7528\u3002_")
    else:
        for analysis in event_analyses:
            lines.extend(["", f"### Pod `{analysis['pod']}`", ""])
            lines.append(f"- \u547d\u4ee4: `{analysis['command']}`")
            if health_level in {HEALTHY, WARNING}:
                lines.append("- \u5206\u7c7b: `\u5386\u53f2\u544a\u8b66\uff08Historical Warning\uff09`")
            else:
                lines.append("- \u5206\u7c7b: `\u5f53\u524d\u6545\u969c\u5206\u6790\uff08Current Failure Investigation\uff09`")
            if not analysis["ok"]:
                lines.append(f"- \u9519\u8bef: `{analysis['error']}`")
                continue
            error_lines = analysis.get("error_lines", [])
            if error_lines:
                lines.append("- \u547d\u4e2d\u7684 Events \u9519\u8bef\u884c:")
                for event_line in error_lines:
                    lines.append(f"  - `{event_line.strip()}`")
            else:
                lines.append("- Events \u4e2d\u672a\u547d\u4e2d\u5df2\u77e5\u9519\u8bef\u5173\u952e\u8bcd\u3002")

    historical_warnings = collect_event_warning_lines(event_analyses) if health_level in {HEALTHY, WARNING} else []
    if historical_warnings:
        lines.extend(["", "## \u5386\u53f2\u544a\u8b66", ""])
        lines.append("_\u4ee5\u4e0b Events \u5c5e\u4e8e\u5386\u53f2\u544a\u8b66\uff0c\u4e0d\u4f5c\u4e3a\u5f53\u524d\u6545\u969c\u6839\u56e0\u3002_")
        lines.extend(["", "| Pod | Events \u884c |", "| --- | --- |"])
        for pod_name, event_line in historical_warnings:
            lines.append(f"| {md_escape(pod_name)} | {md_escape(event_line)} |")

    lines.extend(["", "## \u6700\u8fd1\u65e5\u5fd7", ""])
    if not recent_logs:
        lines.append("_\u672a\u53d1\u73b0\u5f02\u5e38 Pod\uff0c\u56e0\u6b64\u672a\u91c7\u96c6\u6700\u8fd1\u65e5\u5fd7\u3002_")
    else:
        for item in recent_logs:
            lines.extend(["", f"### Pod `{item['pod']}`", ""])
            lines.append(f"- \u547d\u4ee4: `{item['command']}`")
            if item["ok"]:
                log_text = item.get("log") or "<empty log output>"
                lines.extend(["", "```text", log_text, "```"])
            else:
                lines.append(f"- \u9519\u8bef: `{item['error']}`")

    lines.extend(["", "## \u53ef\u80fd\u6839\u56e0", ""])
    for cause in root_causes:
        lines.append(f"- {cause}")

    lines.extend(["", "## \u5efa\u8bae\u4fee\u590d", ""])
    for fix in suggested_fixes:
        if "\n" in fix:
            lines.extend(["", fix])
        else:
            lines.append(f"- {fix}")

    lines.append("")
    return "\n".join(lines)


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def default_output_path(namespace: str, deployment: str) -> str:
    filename = f"release-doctor-{namespace}-{deployment}.md"
    return os.path.join("reports", filename)


def collect_diagnostics(namespace: str, deployment_name: str, output_path: str) -> str:
    preflight_checks, preflight_ok = run_preflight_checks(namespace)

    if not preflight_ok:
        deployment_result = make_skipped_result(
            ["kubectl", "get", "deployment", deployment_name, "-n", namespace, "-o", "json"],
            "\u56e0\u73af\u5883\u9884\u68c0\u5931\u8d25\uff0c\u8df3\u8fc7\u8be5\u547d\u4ee4\u3002",
        )
        deployment_summary = {"error": "\u56e0 kubectl \u73af\u5883\u9884\u68c0\u5931\u8d25\uff0c\u8df3\u8fc7 Deployment \u91c7\u96c6\u3002"}
        pod_error = "\u56e0 kubectl \u73af\u5883\u9884\u68c0\u5931\u8d25\uff0c\u8df3\u8fc7 Pod \u67e5\u8be2\u3002"
        service_analysis = {
            "command": "kubectl get services -n {namespace} -o json".format(namespace=namespace),
            "ok": False,
            "error": "\u56e0 kubectl \u73af\u5883\u9884\u68c0\u5931\u8d25\uff0c\u8df3\u8fc7 Service \u68c0\u67e5\u3002",
            "matched": [],
            "unmatched": [],
            "warning": "\u56e0 kubectl \u73af\u5883\u9884\u68c0\u5931\u8d25\uff0cService \u68c0\u67e5\u5df2\u8df3\u8fc7\u3002",
        }
        health_level = UNKNOWN
        event_analyses: List[Dict[str, Any]] = []
        recent_logs: List[Dict[str, Any]] = []
        root_causes = infer_root_causes(deployment_summary, [], [], event_analyses, service_analysis, health_level)
        suggested_fixes = suggest_fixes(root_causes, [], health_level, namespace)
        return build_report(
            namespace=namespace,
            deployment_name=deployment_name,
            output_path=output_path,
            preflight_checks=preflight_checks,
            deployment_result=deployment_result,
            pod_result=None,
            selector="",
            deployment_summary=deployment_summary,
            pod_summaries=[],
            pod_error=pod_error,
            service_analysis=service_analysis,
            config_checks=[],
            event_analyses=event_analyses,
            recent_logs=recent_logs,
            health_level=health_level,
            root_causes=root_causes,
            suggested_fixes=suggested_fixes,
        )

    deployment_result = run_kubectl(["get", "deployment", deployment_name, "-n", namespace, "-o", "json"])
    deployment_data, deployment_error = parse_json_result(deployment_result)
    deployment_summary = summarize_deployment(deployment_data, deployment_error)

    selector = ""
    pod_result: Optional[CommandResult] = None
    pods_data: Optional[Dict[str, Any]] = None
    pod_error: Optional[str] = None

    if deployment_data:
        match_labels = get_nested(deployment_data, ["spec", "selector", "matchLabels"], {}) or {}
        if match_labels:
            selector = selector_to_label_arg(match_labels)
            pod_result = run_kubectl(["get", "pods", "-n", namespace, "-l", selector, "-o", "json"])
            pods_data, pod_error = parse_json_result(pod_result)
        else:
            pod_error = "Deployment spec.selector.matchLabels \u4e3a\u7a7a\u6216\u4e0d\u53ef\u7528\u3002"
    else:
        pod_error = "\u56e0 Deployment \u6570\u636e\u4e0d\u53ef\u7528\uff0c\u8df3\u8fc7 Pod \u67e5\u8be2\u3002"

    pod_summaries, parsed_pod_error = get_pod_summaries(pods_data, pod_error)
    config_refs = extract_config_refs(deployment_data)
    config_checks = check_config_refs(namespace, config_refs)
    current_healthy = is_currently_healthy(deployment_summary, pod_summaries, parsed_pod_error, config_checks)
    service_analysis = analyze_services(namespace, deployment_data, pod_summaries)
    event_analyses = analyze_events(namespace, pod_summaries, include_healthy_pods=current_healthy)
    recent_logs = collect_recent_logs(namespace, pod_summaries)
    health_level = evaluate_health_level(
        preflight_ok,
        deployment_summary,
        pod_summaries,
        parsed_pod_error,
        config_checks,
        event_analyses,
        service_analysis,
    )
    root_causes = infer_root_causes(deployment_summary, pod_summaries, config_checks, event_analyses, service_analysis, health_level)
    suggested_fixes = suggest_fixes(root_causes, config_checks, health_level, namespace)

    return build_report(
        namespace=namespace,
        deployment_name=deployment_name,
        output_path=output_path,
        preflight_checks=preflight_checks,
        deployment_result=deployment_result,
        pod_result=pod_result,
        selector=selector,
        deployment_summary=deployment_summary,
        pod_summaries=pod_summaries,
        pod_error=parsed_pod_error,
        service_analysis=service_analysis,
        config_checks=config_checks,
        event_analyses=event_analyses,
        recent_logs=recent_logs,
        health_level=health_level,
        root_causes=root_causes,
        suggested_fixes=suggested_fixes,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kube Release Doctor: diagnose common Kubernetes Deployment release failures."
    )
    parser.add_argument("--namespace", required=True, help="Kubernetes namespace, for example: prod")
    parser.add_argument("--deployment", required=True, help="Deployment name, for example: devops-cicd-demo")
    parser.add_argument(
        "--output",
        default=None,
        help="Markdown report path. Default: reports/release-doctor-<namespace>-<deployment>.md",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    output_path = args.output or default_output_path(args.namespace, args.deployment)

    report = collect_diagnostics(args.namespace, args.deployment, output_path)
    print(report)

    try:
        ensure_parent_dir(output_path)
        with open(output_path, "w", encoding="utf-8") as report_file:
            report_file.write(report)
    except OSError as exc:
        print(f"\n\u5199\u5165\u62a5\u544a\u5931\u8d25 {output_path}: {exc}", file=sys.stderr)
        return 1

    print(f"Report saved to: {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
