# Day 0: Harness Validation & Baseline Calibration

## Work Completed
- RETRIEVED credentials and initialized context endpoints for both clusters (`fluent-bit-agent-x86` and `fluent-bit-agent-arm64`).
- DEPLOYED synthetic log generator workload to `lab-loadgen` namespace (3 replicas fanned across nodes).
- RETRIEVED baseline ConfigMap and DaemonSet configurations.
- DEVELOPED metrics collection scraper `harness/collect.py` which dynamically port-forwards to GKE pods and queries Prometheus endpoints and GCM API.
- BUG FIX: Transitioned log delivery ratio verification from parsing Cloud Logging search results (hit read rate-limits) to querying Google Cloud Monitoring (GCM) timeseries aggregates.
- BUG FIX: Added dynamic access token extraction & refreshing to the collector script to prevent auth token expiration over long runs.
- CALIBRATED load-gen rates and established base metrics for L1 nominal load and L2 stress load on the baseline stock configuration.

## Calibration Baselines (Stock Config: Workers=1, No limits, Use_Kubelet=Off)
| Load Level | Generator Rate (per pod) | Egress Rate (total logs/s) | CPU Cores (total) | Memory (total MB) | Per-core Efficiency (logs/s/core) | Delivery Ratio | Backlog Trend |
|---|---|---|---|---|---|---|---|
| **L1 (Nominal)** | 300 | 902 | 0.194 | 180 | 4650 | 1.0000 | flat |
| **L2 (Stress)** | 8000 | 24051 | 2.667 | 380 | 9018 | 1.0000 | down (draining warmup) |

## Code Reading Discoveries
- **Double Scan in stackdriver_format**: The Stackdriver output plugin format loop scans and decodes the entire chunk messagepack body twice: once to count records and validate `insertId`, and a second time to format the payload. Proposing Hypothesis H-009 (single-loop using `flb_mp_array_header` dynamic array builders).
- **Heap Churn in init_http_request**: The output plugin allocates 8 structured `flb_sds_t` strings for `httpRequest` for *every* processed log record, only to free them immediately when `httpRequest` is not present (which is true for 99%+ GKE container logs). Proposing Hypothesis H-011 (lazy httpRequest initialization).

## Next Steps
- Transition state to Day 1.
- Begin the Day 1 config parameter sweep on the experiment cluster (`fluent-bit-agent-x86`):
  1. Test Sweep 1: Enable pod CPU limits & set resources limits (to see if node kernel throttles Fluent Bit under L2 stress load).
  2. Test Sweep 2: Scale output workers from 1 to 2, 4, and 8 to measure threading performance on multi-core VMs.
