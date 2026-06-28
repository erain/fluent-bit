# Day 2: Kubelet Metadata Extraction Sweep

## Work Completed
- SWEEPED `Use_Kubelet` configuration parameter (enabled `On` in champion vs. disabled `Off` on the experiment GKE cluster) under L2 stress load (24,000 records/s).
- RESTORED champion configuration (Use_Kubelet=On, stackdriver workers=2, cpu resource limit=1).
- UPDATED experiment ledger and state.

## Sweep Results (L2 Stress Load, Rate=8000/pod, 3 replicas)
| Run ID | Configuration | CPU Cores (total) | Memory (total MB) | Egress Rate (logs/s) | Per-core Efficiency (logs/s/core) | Delivery Ratio | Backlog Trend |
|---|---|---|---|---|---|---|---|
| `sweep-limit-1` | Workers=2, CPU Limit=1.0, Kubelet=On | 2.394 | 412 | 24,867 | 10,387 | 1.0000 | down |
| `sweep-kubelet-off` | Workers=2, CPU Limit=1.0, Kubelet=Off | 2.513 | 456 | 20,566 | 8,184 | 1.0000 | down (slow) |

## Key Findings & Decisions
- **Use_Kubelet=On is critical for throughput and efficiency**:
  - Toggling `Use_Kubelet` to `Off` causes a major performance regression: throughput drops by **17.3%** and CPU efficiency drops by **21.2%**.
  - This is because when disabled, the kubernetes filter must query the remote Kubernetes API server over HTTPS for each new pod tailing connection, which introduces network round-trip overhead and blocks the main event loop thread.
  - Enabling `Use_Kubelet` (GKE default) routes these requests to node-local kubelet cache endpoints over localhost, avoiding network hops and minimizing event-loop latency.
  - The champion config remains: **Use_Kubelet=On, workers=2, CPU limit=1.0**.

## Next Steps
- Transition state to Day 3.
- Day 3 will transition to **Day 3: Code Compilation & Optimization Sweep**.
- We will implement code-level changes on the GKE Fluent Bit binary:
  1. Optimize `stackdriver_format` to use a single event decode loop instead of a double loop (H-009).
  2. Implement lazy initialization of `httpRequest` fields in `stackdriver_http_request.c` to prevent heap allocation churn (H-011).
  3. Rebuild the container image, push it to GCR/GAR, deploy it on the GKE experiment cluster, and run calibration runs.
