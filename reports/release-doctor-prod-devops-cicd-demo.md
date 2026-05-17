# Kube Release Doctor Report

## Basic Info

- Namespace: `prod`
- Deployment: `devops-cicd-demo`
- Generated At: `2026-05-17 10:59:07`
- Output: `reports\release-doctor-prod-devops-cicd-demo.md`

### Commands

- `kubectl get deployment devops-cicd-demo -n prod -o json`

> Deployment command failed: `[WinError 2] 系统找不到指定的文件。`

## Deployment Status

- Error: `[WinError 2] 系统找不到指定的文件。`

## Pod Status

- Error: `Skipped Pod query because Deployment data is unavailable.`

## Secret / ConfigMap Check

_No Secret or ConfigMap references found in Deployment env/envFrom._

## Events Analysis

_No abnormal Pods found, so pod describe events were not collected._

## Possible Root Cause

- Deployment information could not be collected. Check namespace, deployment name, and kubectl access.

## Suggested Fix

- Run kubectl rollout status, kubectl describe deployment, kubectl describe pod, and kubectl logs for deeper inspection.
