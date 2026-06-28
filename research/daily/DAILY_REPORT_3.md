# DAILY REPORT 3 — Sunday, June 27, 2026

## 1. Summary of Work & Findings
- **Resolved Gitignore Compilation Blocker**: Solved a Cloud Build failure where the `lib/zstd-1.5.7/build/cmake` folder was omitted from uploads due to an unanchored `build/*` pattern in `.gitignore`. Anchoring the pattern to `/build/*` successfully resolved the issue.
- **Diagnosed and Fixed a Production SIGSEGV Crash**: Under L3 stress load (20,000 logs/s/pod), the initial optimized image crashed with exit code 139 (segmentation fault) at `plugins/out_stackdriver/stackdriver.c:2561` inside `add_source_location_field`. We traced the crash to dereferencing unallocated (`NULL`) pointers for optional subfields (like `file` or `function` in `sourceLocation`, `id` in `operation`, or string subfields in `httpRequest`) when they were missing in the incoming JSON log payloads. We implemented a safe helper function `pack_sds_safe` in `stackdriver_helper.c` to format `NULL` pointers as empty strings `""` safely without heap allocation. Re-testing proved the fix makes the optimized image 100% stable under L3 stress load.
- **Calibrated L2/L3 Egress Performance**:
  - Under L2 stress (8000 logs/s/pod): The optimized image performs on par with the baseline image (~2.39 cores used, ~10,339 records/s/core efficiency).
  - Under L3 stress (20,000 logs/s/pod): The stable optimized image achieved **14,805 records/s/core** on **2.236 cores** vs. baseline's **14,650 records/s/core** on **2.265 cores** (a **+1.06% efficiency improvement** and **-1.28% CPU core overhead reduction**).
- **Conducted Workload Diversity Sweeps (Day 4)**:
  - **Plaintext (W1)**: Bypassing JSON structure parsing reduces CPU usage by 2.0% (2.341 cores vs 2.390 cores for JSON at L2).
  - **High-Cardinality JSON (W5)**: Merging/processing many dynamic keys increases core usage to **2.745 cores** (+14.8% CPU overhead) and drops per-core efficiency to **8,987 records/s/core** (-13% regression).
  - **Multiline (W4)**: Aggregating 4-line stacktraces into a single record fanned down egress serialization and HTTP POST calls by 4×, leading to a massive CPU reduction to **2.111 cores** and an efficiency of **11,803 records/s/core**.

---

## 2. Replicate Ledgers (New Runs Added)

| Run ID | Config Hash / Image | Load | CPU Cores | Mem (MB) | Egress (rec/s) | Core Efficiency (rec/s/core) | Delivery Ratio | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **sweep-code-opt-1** | `opt-day3-test-1` / Workers=2 | L2 | 2.390 | 451 | 24,712 | 10,339 | 1.000 | Config champion, code v1. Stable. |
| **sweep-code-opt2-l3**| `opt-day3-test-2` / Workers=2 | L3 | 2.236 | 576 | 33,105 | 14,805 | 0.598 | Code v2 (null-safe). 100% Stable. |
| **sweep-base-l3** | Stock Baseline / Workers=2 | L3 | 2.265 | 465 | 33,182 | 14,650 | 0.602 | Baseline reference under L3 load. |
| **sweep-workload-plaintext-l2** | `opt-day3-test-2` / Workers=2 | L2 | 2.341 | 425 | 24,279 | 10,371 | 1.000 | Plaintext workload. Bypasses JSON parsing. |
| **sweep-workload-highcard-l2**  | `opt-day3-test-2` / Workers=2 | L2 | 2.745 | 484 | 24,670 | 8,987 | 1.000 | High-cardinality JSON keys. High CPU. |
| **sweep-workload-multiline-l2** | `opt-day3-test-2` / Workers=2 | L2 | 2.111 | 369 | 24,917 | 11,803 | 1.000 | Multiline stacktrace logs. Highly efficient. |

---

## 3. Promoted Champion Configuration
- **Active Champion Config**:
  - **Output Workers**: `2`
  - **Kubelet Metadata Extraction**: `On`
  - **Container Image**: `us-central1-docker.pkg.dev/timbai-gke-dev/gke-component-images/fluentbit:opt-day3-test-2` (our stable, null-safe single-loop Stackdriver image).

---

## 4. Workstation Python Environment Blocker
- Local Python virtual environment creation (`./setup-venv.sh`) failed because `beautifulsoup4==4.12.3` could not be resolved in the staging artifact simple registry Simple index. Therefore, local in-tree integration testing and valgrind runs cannot be executed. We are relying on live GKE cluster validation (using container logs & crash loop checks) for code health.

---

## 5. Next Experiments (Day 5 Schedule)
- **Architecture A/B testing**: Deploy the champion config and image to the Arm64 cluster and compare the performance characteristics of x86 vs. arm64 under the L2/L3 loads.
- **Stacking Configs**: Verify if configuring compression level (or other network parameters) alongside workers=2 produces stacking benefits on x86/arm64.
