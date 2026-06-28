# Day 1: Output Workers & CPU Resource Limit Sweep

## Work Completed
- SWEEPED output plugin `workers` configuration parameter (1 -> 2 -> 4 -> 8) on the experiment GKE cluster under L2 stress load (24,000 records/s).
- SWEEPED container CPU limits (baseline: no limit vs. 1.0 core limit) to measure kernel scheduling overhead/throttling.
- UPDATED experiment ledger and state.

## Sweep Results (L2 Stress Load, Rate=8000/pod, 3 replicas)
| Run ID | Configuration | CPU Cores (total) | Memory (total MB) | Egress Rate (logs/s) | Per-core Efficiency (logs/s/core) | Delivery Ratio | Backlog Trend |
|---|---|---|---|---|---|---|---|
| `calib-l2-test-2` | Baseline (Workers=1, No limit) | 2.667 | 380 | 24,051 | 9,018 | 1.0000 | down |
| `sweep-workers-2` | Workers=2, No limit | 2.452 | 411 | 24,797 | 10,113 | 1.0000 | down (fast) |
| `sweep-workers-4` | Workers=4, No limit | 2.401 | 501 | 24,675 | 10,277 | 1.0000 | down (fast) |
| `sweep-workers-8` | Workers=8, No limit | 2.423 | 647 | 24,748 | 10,213 | 1.0000 | down |
| `sweep-limit-1` | Workers=2, CPU Limit=1.0 | 2.394 | 412 | 24,867 | 10,387 | 1.0000 | down (fast) |

## Key Findings & Decisions
- **Workers sweet spot is 2**:
  - Transitioning from 1 to 2 workers improves efficiency by **12.1%** and reduces CPU consumption by **8%**, due to offloading formatting and I/O from the main engine loop thread to parallel workers.
  - Scaling beyond 2 workers (e.g. 4 or 8) leads to diminishing efficiency gains (<1.6%) and introduces significant memory overhead (+90MB for 4 workers, +236MB for 8 workers).
  - Therefore, **2 workers** is selected as the champion configuration.
- **CPU Resource Limit of 1.0 core is safe**:
  - The champion config with 2 workers consumes only ~0.8 cores/pod under 24,000 records/s stress load. Setting a hard CPU limit of 1.0 core is safe, preventing CPU runaways without triggering CFS throttle or log drops.

## Next Steps
- Transition state to Day 2.
- Day 2 will investigate **Use_Kubelet On** (H-006) which shifts GKE pod metadata lookup to local kubelet caches instead of querying API server, potentially reducing kubernetes filter CPU cost significantly.
- Restore ConfigMap to workers=2 and CPU limit=1 as the new champion baseline config.
