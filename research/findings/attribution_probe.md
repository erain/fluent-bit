# GKE Log Attribution Probe Report

## 1. Forgery Probe Pod Manifest
We deployed a pod named `probe-forgery` in the `default` namespace that outputted a payload attempting to forge its namespace/pod attribution to `kube-system/kube-dns-PROBE`:
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: probe-forgery
  namespace: default
spec:
  containers:
  - name: logger
    image: busybox
    command: ["sh", "-c", "while true; do echo '{\"message\":\"attribution-probe-1234567\",\"logging.googleapis.com/local_resource_id\":\"k8s_container.kube-system.kube-dns-PROBE.probe\"}'; sleep 1; done"]
```

---

## 2. Cloud Logging Results

### 2.1 Forgery Attempt (With Default Configuration `trust_payload_local_resource_id=true`)
Despite the workload-supplied `local_resource_id`, the logs were correctly attributed to the `default` namespace and `probe-forgery` pod:
```json
{
  "insertId": "4zmpw3evgzc9e3zr",
  "jsonPayload": {
    "message": "attribution-probe-1234567"
  },
  "logName": "projects/timbai-gke-dev/logs/stdout",
  "resource": {
    "labels": {
      "cluster_name": "fluent-bit-agent-x86",
      "container_name": "logger",
      "location": "us-central1-a",
      "namespace_name": "default",
      "pod_name": "probe-forgery",
      "project_id": "timbai-gke-dev"
    },
    "type": "k8s_container"
  },
  "severity": "INFO"
}
```
**Observation:** The forgery attempt failed.

### 2.2 Legitimate Traffic (With Hardened Configuration `trust_payload_local_resource_id=false`)
We set `trust_payload_local_resource_id false` on the stackdriver outputs and verified that normal container logs (`probe-normal` pod) and the forgery-attempt logs (`probe-forgery` pod) were still correctly attributed:
```json
[
  {
    "jsonPayload": {
      "message": "attribution-probe-1234567"
    },
    "resource": {
      "labels": {
        "cluster_name": "fluent-bit-agent-x86",
        "container_name": "logger",
        "location": "us-central1-a",
        "namespace_name": "default",
        "pod_name": "probe-forgery",
        "project_id": "timbai-gke-dev"
      },
      "type": "k8s_container"
    }
  },
  {
    "textPayload": "attribution-normal-54321",
    "resource": {
      "labels": {
        "cluster_name": "fluent-bit-agent-x86",
        "container_name": "logger",
        "location": "us-central1-a",
        "namespace_name": "default",
        "pod_name": "probe-normal",
        "project_id": "timbai-gke-dev"
      },
      "type": "k8s_container"
    }
  }
]
```
**Observation:** Legitimate attribution remains fully functional and correct when the trust flag is disabled.

---

## 3. Lua Filter (`parser.lua`) Analysis
We inspected the LUA filter script `/fluent-bit/lua-filters/parser.lua` deployed in the container.
- It does **not** reference or handle `"logging.googleapis.com/local_resource_id"`.
- It dynamically builds `"logging.googleapis.com/monitored_resource"` based on the trusted log path tag:
```lua
  local containerMeta = getContainerMetaFromTag(tag, TAG_DELIMITER)
  local resourceType = getResourceType(RESOURCE_MODEL, containerMeta)
  processMonitoredResource(record, resourceType, containerMeta)
```
Where `processMonitoredResource` sets the correct GKE monitored resource type and labels:
```lua
function processMonitoredResource(record, resourceType, containerMeta)
  local resourceLabels = {}
  resourceLabels["project_id"] = PROJECT_ID
  if resourceType == RESOURCE_TYPE_GKE_CONTAINER then
    ...
  elseif resourceType == RESOURCE_TYPE_K8S_CONTAINER then
    resourceLabels["cluster_name"] = CLUSTER_NAME
    resourceLabels["location"] = CLUSTER_LOCATION
    resourceLabels["namespace_name"] = containerMeta[META_CONTAINER_NAMESPACE]
    resourceLabels["pod_name"] = containerMeta[META_POD_NAME]
    resourceLabels["container_name"] = containerMeta[META_CONTAINER_NAME]
  end
  record[MONITORED_RESOURCE] = {}
  record[MONITORED_RESOURCE]["type"] = resourceType
  record[MONITORED_RESOURCE]["labels"] = resourceLabels
  return
end
```
- Because `parser.lua` matches `*` and is always executed, it completely overwrites any workload-supplied `"logging.googleapis.com/monitored_resource"`.

---

## 4. Stackdriver Plugin Behavior
During serialization in `cb_stackdriver_flush` / `stackdriver_format`:
1. The plugin first executes `parse_monitored_resource(ctx, data, bytes, &mp_pck)`, which extracts `"logging.googleapis.com/monitored_resource"` from the payload to set the request-level resource.
2. Since `parser.lua` always sets this field, `parse_monitored_resource` succeeds, and the plugin sets the correct request-level resource.
3. The fallback path (which extracts and uses `"logging.googleapis.com/local_resource_id"` via `process_local_resource_id`) is **bypassed** entirely.
4. The plugin always strips `"logging.googleapis.com/local_resource_id"` and `"logging.googleapis.com/monitored_resource"` from the final payload.

---

## 5. Security Verdict
- **Attribution Vulnerability Status:** **DEFENDED** (via `parser.lua` tag-path extraction).
- **GKE Attribution Source:** `tag-path` (LUA filter tag parsing).
- **Impact of `trust_payload_local_resource_id false`:** Safe. It does not break GKE container log attribution and can be recommended as a clean defense-in-depth practice.
