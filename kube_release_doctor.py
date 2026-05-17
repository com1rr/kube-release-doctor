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

HEALTHY = "健康"
WARNING = "警告"
CRITICAL = "严重"
UNKNOWN = "未知"


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
            stderr=(stderr.strip() if isinstance(stderr, str) else "") or "kubectl command timed out",
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
                "detail": "kubectl was not found in PATH. Install kubectl or add it to PATH.",
            }
        )
        return checks, False

    version_result = run_kubectl(["version", "--client"])
    checks.append(
        {
            "name": "kubectl client version",
            "status": "OK" if version_result.ok else "Failed",
            "command": version_result.command_text,
            "detail": version_result.stdout or version_result.stderr or "No output",
        }
    )

    namespace_result = run_kubectl(["get", "namespace", namespace])
    checks.append(
        {
            "name": "cluster namespace access",
            "status": "OK" if namespace_result.ok else "Failed",
            "command": namespace_result.command_text,
            "detail": namespace_result.stdout or namespace_result.stderr or "No output",
        }
    )

    return checks, version_result.ok and namespace_result.ok


def parse_json_result(result: CommandResult) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not result.ok:
        return None, result.stderr or "kubectl command failed"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"Failed to parse kubectl JSON output: {exc}"


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
                "error": "" if result.ok else (result.stderr or "kubectl command failed"),
                "command": result.command_text,
            }
        )
    return results


def summarize_deployment(deployment: Optional[Dict[str, Any]], error: Optional[str]) -> Dict[str, Any]:
    if not deployment:
        return {"error": error or "Deployment data unavailable"}

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
        return [], error or "Pod data unavailable"
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
        analysis["warning"] = "Service check could not be completed."
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
        analysis["warning"] = "No Service selector matches the current Deployment pod template labels."

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
                "error": "" if result.ok else (result.stderr or "kubectl logs failed"),
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
                "error": "" if result.ok else (result.stderr or "kubectl command failed"),
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
        causes.append("Unable to determine current release health because kubectl access or required resources are unavailable.")
        return causes

    if health_level in {HEALTHY, WARNING}:
        historical_warnings_found = any(analysis.get("error_lines") for analysis in event_analyses)
        restart_warnings_found = has_restart_warnings(pod_summaries)
        service_warning = service_analysis.get("warning")
        if health_level == WARNING:
            causes.append(
                "No current release failure detected. Current resources are healthy, but warnings were found."
            )
            if historical_warnings_found:
                causes.append("Historical warning events were found, but current resources are healthy.")
            if restart_warnings_found:
                causes.append("One or more containers have restartCount > 0 but are currently ready.")
            if service_warning:
                causes.append(str(service_warning))
        else:
            causes.append("No current release failure detected. Current resources are healthy.")
        return causes

    if deployment_summary.get("error"):
        causes.append("Deployment information could not be collected. Check namespace, deployment name, and kubectl access.")

    replicas = deployment_summary.get("replicas")
    available = deployment_summary.get("availableReplicas")
    updated = deployment_summary.get("updatedReplicas")
    if isinstance(replicas, int) and replicas > 0:
        if not isinstance(available, int) or available < replicas:
            causes.append("Deployment does not have all desired replicas available.")
        if not isinstance(updated, int) or updated < replicas:
            causes.append("Deployment rollout may be incomplete because updatedReplicas is lower than desired replicas.")

    missing_refs = [item for item in config_checks if item["status"] != "Found"]
    if missing_refs:
        names = ", ".join(f"{item['kind']}/{item['name']}" for item in missing_refs)
        causes.append(f"Referenced Secret or ConfigMap is missing or inaccessible: {names}.")

    reasons: Set[str] = set()
    for pod in pod_summaries:
        reasons.update(pod.get("problems", []))

    if "ImagePullBackOff" in reasons or "ErrImagePull" in reasons:
        causes.append("Container image cannot be pulled. Image name, tag, registry credentials, or network access may be wrong.")
    if "CrashLoopBackOff" in reasons:
        causes.append("A container is repeatedly crashing after startup. Application command, config, or dependency readiness may be wrong.")
    if "CreateContainerConfigError" in reasons:
        causes.append("Container configuration is invalid, often because referenced Secret or ConfigMap keys are missing.")
    if "Pending" in reasons:
        causes.append("Pod is Pending. Scheduling, resource quota, node capacity, PVC, or image pull prerequisites may be blocking it.")
    if "ContainerCreating" in reasons:
        causes.append("Pod is stuck in ContainerCreating. Volume mount, image pull, CNI, or node runtime issues may be involved.")

    for analysis in event_analyses:
        for line in analysis.get("error_lines", []):
            if "FailedScheduling" in line:
                causes.append("Scheduler reported FailedScheduling events.")
            if "FailedMount" in line:
                causes.append("Pod volume mount failed.")
            if "Unhealthy" in line:
                causes.append("Readiness or liveness probe is failing.")

    if service_analysis.get("warning"):
        causes.append(str(service_analysis["warning"]))

    if not causes:
        causes.append("No obvious root cause detected from the collected Deployment, Pod, Secret, ConfigMap, and Event data.")

    return unique_preserve_order(causes)


def suggest_fixes(
    root_causes: List[str],
    config_checks: List[Dict[str, str]],
    health_level: str,
    namespace: str,
) -> List[str]:
    if health_level == HEALTHY:
        return [
            "No immediate fix required."
        ]
    if health_level == WARNING:
        return [
            "No immediate fix required. Historical warnings, restart counts, and Service selector coverage can be reviewed if needed."
        ]
    if health_level == UNKNOWN:
        return [
            "Verify kubectl is installed, kubeconfig points to the correct cluster, and the namespace/deployment exists."
        ]

    fixes: List[str] = []
    text = "\n".join(root_causes)
    missing_refs = [item for item in config_checks if item["status"] != "Found"]

    if "Secret or ConfigMap" in text or missing_refs:
        fixes.append("Create the missing Secret/ConfigMap or fix the Deployment env/envFrom reference names and keys.")
        fixes.append("Replace `<KEY>` and `<VALUE>` with the real data required by the application before running these commands.")
        for item in missing_refs:
            if item["kind"] == "secret":
                fixes.append(
                    "Example command for missing Secret `{name}`:\n\n"
                    "```bash\n"
                    "kubectl create secret generic {name} \\\n"
                    "  --from-literal=<KEY>=<VALUE> \\\n"
                    "  -n {namespace}\n"
                    "```".format(name=item["name"], namespace=namespace)
                )
            elif item["kind"] == "configmap":
                fixes.append(
                    "Example command for missing ConfigMap `{name}`:\n\n"
                    "```bash\n"
                    "kubectl create configmap {name} \\\n"
                    "  --from-literal=<KEY>=<VALUE> \\\n"
                    "  -n {namespace}\n"
                    "```".format(name=item["name"], namespace=namespace)
                )
    if "image cannot be pulled" in text:
        fixes.append("Verify image repository, tag, imagePullSecrets, registry permissions, and node network access to the registry.")
    if "repeatedly crashing" in text:
        fixes.append("Check container logs, startup command, application config, database/service dependencies, and resource limits.")
    if "FailedScheduling" in text or "Pending" in text:
        fixes.append("Check node capacity, taints/tolerations, affinity rules, resource requests, PVC status, and namespace quota.")
    if "ContainerCreating" in text or "volume mount failed" in text:
        fixes.append("Inspect volume mounts, PVC/PV binding, CSI driver health, CNI status, and kubelet events on the target node.")
    if "probe is failing" in text:
        fixes.append("Review readiness/liveness probe path, port, timeout, initialDelaySeconds, and application health endpoint behavior.")

    if not fixes:
        fixes.append("Run kubectl rollout status, kubectl describe deployment, kubectl describe pod, and kubectl logs for deeper inspection.")

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


def format_conditions(conditions: List[Dict[str, Any]]) -> str:
    if not conditions:
        return "_No conditions reported._"
    lines = ["| Type | Status | Reason | Message |", "| --- | --- | --- | --- |"]
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
        "# Kube Release Doctor Report",
        "",
        "## Basic Info",
        "",
        f"- Namespace: `{namespace}`",
        f"- Deployment: `{deployment_name}`",
        f"- Generated At: `{now}`",
        f"- Output: `{output_path}`",
        "",
        "### Commands",
        "",
        f"- `{deployment_result.command_text}`",
    ]

    if pod_result:
        lines.append(f"- `{pod_result.command_text}`")
    if not deployment_result.ok:
        lines.extend(["", f"> Deployment command failed: `{deployment_result.stderr or 'unknown error'}`"])
    if pod_result and not pod_result.ok:
        lines.extend(["", f"> Pod command failed: `{pod_result.stderr or 'unknown error'}`"])

    lines.extend(["", "## Preflight Check", ""])
    lines.extend(["| Check | Status | Command | Detail |", "| --- | --- | --- | --- |"])
    for check in preflight_checks:
        lines.append(
            "| {name} | {status} | `{command}` | {detail} |".format(
                name=md_escape(check.get("name", "")),
                status=md_escape(check.get("status", "")),
                command=md_escape(check.get("command", "")),
                detail=md_escape(check.get("detail", "")),
            )
        )

    lines.extend(["", "## Current Status", ""])
    lines.append(f"- 健康等级: `{health_level}`")
    if health_level == HEALTHY:
        lines.append("- Current resources are healthy.")
    elif health_level == WARNING:
        lines.append("- Current core resources are healthy, but warnings require review.")
    elif health_level == CRITICAL:
        lines.append("- Current release failure signals were detected.")
    else:
        lines.append("- Health could not be determined because kubectl access or required resources are unavailable.")

    lines.extend(["", "## Deployment Status", ""])
    if deployment_summary.get("error"):
        lines.append(f"- Error: `{deployment_summary['error']}`")
    else:
        lines.extend(
            [
                f"- Desired Replicas: `{deployment_summary.get('replicas', 0)}`",
                f"- Available Replicas: `{deployment_summary.get('availableReplicas', 0)}`",
                f"- Updated Replicas: `{deployment_summary.get('updatedReplicas', 0)}`",
                f"- Ready Replicas: `{deployment_summary.get('readyReplicas', 0)}`",
                f"- Pod Selector: `{selector or 'N/A'}`",
                "",
                "### Conditions",
                "",
                format_conditions(deployment_summary.get("conditions", []) or []),
            ]
        )

    lines.extend(["", "## Pod Status", ""])
    if pod_error:
        lines.append(f"- Error: `{pod_error}`")
    elif not pod_summaries:
        lines.append("_No Pods found for the Deployment selector._")
    else:
        lines.extend(
            [
                "| Pod | Phase | Container | Ready | Restarts | Waiting Reason | Problems | Warnings |",
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

    lines.extend(["", "## Service Check", ""])
    lines.append(f"- Command: `{service_analysis.get('command', 'N/A')}`")
    if not service_analysis.get("ok"):
        lines.append(f"- Error: `{service_analysis.get('error') or service_analysis.get('warning') or 'Service check failed'}`")
    else:
        matched = service_analysis.get("matched", []) or []
        if service_analysis.get("warning"):
            lines.append(f"- Warning: {service_analysis['warning']}")
        if not matched:
            lines.append("_No matching Service found for the Deployment pod template labels._")
        else:
            lines.extend(
                [
                    "",
                    "| Service | Type | Selector | Ports |",
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

    lines.extend(["", "## Secret / ConfigMap Check", ""])
    if not config_checks:
        lines.append("_No Secret or ConfigMap references found in Deployment env/envFrom._")
    else:
        lines.extend(["| Kind | Name | Status | Error |", "| --- | --- | --- | --- |"])
        for item in config_checks:
            lines.append(
                f"| {md_escape(item['kind'])} | {md_escape(item['name'])} | {md_escape(item['status'])} | {md_escape(item['error'] or '-')} |"
            )

    lines.extend(["", "## Events Analysis", ""])
    if not event_analyses:
        lines.append("_No Pods required event analysis, or Pod data was unavailable._")
    else:
        for analysis in event_analyses:
            lines.extend(["", f"### Pod `{analysis['pod']}`", ""])
            lines.append(f"- Command: `{analysis['command']}`")
            if health_level in {HEALTHY, WARNING}:
                lines.append("- Classification: `Historical warning check`")
            else:
                lines.append("- Classification: `Current failure investigation`")
            if not analysis["ok"]:
                lines.append(f"- Error: `{analysis['error']}`")
                continue
            error_lines = analysis.get("error_lines", [])
            if error_lines:
                lines.append("- Matched error event lines:")
                for event_line in error_lines:
                    lines.append(f"  - `{event_line.strip()}`")
            else:
                lines.append("- No known error keywords found in Events.")

    historical_warnings = collect_event_warning_lines(event_analyses) if health_level in {HEALTHY, WARNING} else []
    if historical_warnings:
        lines.extend(["", "### Historical Event Warnings", ""])
        lines.append("_These event lines are historical warnings, not current root causes._")
        lines.extend(["", "| Pod | Event Line |", "| --- | --- |"])
        for pod_name, event_line in historical_warnings:
            lines.append(f"| {md_escape(pod_name)} | {md_escape(event_line)} |")

    lines.extend(["", "## Recent Logs", ""])
    if not recent_logs:
        lines.append("_No abnormal Pods found, so recent logs were not collected._")
    else:
        for item in recent_logs:
            lines.extend(["", f"### Pod `{item['pod']}`", ""])
            lines.append(f"- Command: `{item['command']}`")
            if item["ok"]:
                log_text = item.get("log") or "<empty log output>"
                lines.extend(["", "```text", log_text, "```"])
            else:
                lines.append(f"- Error: `{item['error']}`")

    lines.extend(["", "## Possible Root Cause", ""])
    for cause in root_causes:
        lines.append(f"- {cause}")

    lines.extend(["", "## Suggested Fix", ""])
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
            "Skipped because preflight checks failed.",
        )
        deployment_summary = {"error": "Skipped because kubectl preflight checks failed."}
        pod_error = "Skipped Pod query because kubectl preflight checks failed."
        service_analysis = {
            "command": "kubectl get services -n {namespace} -o json".format(namespace=namespace),
            "ok": False,
            "error": "Skipped because kubectl preflight checks failed.",
            "matched": [],
            "unmatched": [],
            "warning": "Service check skipped because kubectl preflight checks failed.",
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
            pod_error = "Deployment spec.selector.matchLabels is empty or unavailable."
    else:
        pod_error = "Skipped Pod query because Deployment data is unavailable."

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
        print(f"\nFailed to write report to {output_path}: {exc}", file=sys.stderr)
        return 1

    print(f"Report saved to: {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
