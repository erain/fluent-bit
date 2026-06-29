# Daily Report 5 — Security Hardening, Tooling Fixes, and Honest Performance Benchmarking

## Summary
We completed the final research phase (Phase C) on the GKE experiment cluster. We implemented the security hardening patch to prevent resource label spoofing from untrusted payloads, resolved a critical log counting bug in our BigQuery-based data collection harness, built a native non-sanitizer Release image (`linux/amd64`), and ran a rigorous comparison between `workers=1` and `workers=2` configurations.

---

## 1. Security Hardening Implementation
We addressed the GKE log attribution risk where untrusted payload fields could maliciously override GCP MonitoredResource labels (e.g. `namespace`, `pod_name`):
- Added configuration flag `trust_payload_local_resource_id` (boolean, default: `true`).
- When set to `false`, the plugin ignores payload-provided resource IDs and falls back to safe tag regex matching. If tag matching fails, it assigns `"unknown"` labels.
- Wrote robust unit test coverage in `tests/runtime/out_stackdriver.c` ensuring correctness and default tag-matching priorities under untrusted payloads.

---

## 2. Native Release Image Compilation
To perform honest performance measurements (without AddressSanitizer or ThreadSanitizer overhead), we built a native `linux/amd64` Release image:
- Configured a single-platform Cloud Build pipeline (`scratch/cloudbuild_release_amd64.yaml`) bypassing slow QEMU multi-arch emulation.
- Compiled Fluent Bit natively with `RelWithDebInfo` and pushed to registry: `us-central1-docker.pkg.dev/timbai-gke-dev/gke-component-images/fluentbit:opt-day5-phase-c-1`.
- Rolled out the image to the x86 experiment cluster and verified successful deployment.

---

## 3. Collection Harness Correction
We identified and fixed a log-counting bug in `collect.py`:
- The previous query used a large end timestamp buffer (`timestamp <= end_epoch + 300`) to catch delayed ingestion. Since the load generator runs indefinitely, this buffer erroneously counted logs generated *after* the measurement window closed, leading to inflated delivery ratios.
- Updated the SQL query to use precise bounds (`timestamp >= start_epoch - 5` and `timestamp <= end_epoch + 5`) matching the actual measurement window.
- Verified the fix via BigQuery diagnostics, achieving less than 2% deviation from the nominal generator rate.

---

## 4. Honest Performance Results (`workers=1` vs `workers=2`)
We ran 3 independent 10-minute replicate runs for both `workers=1` and `workers=2` under L2 load (nominal L2 load = 8000 logs/s/pod across 3 replicas, total ~24,000 logs/s).

### Replicate Run Data (L2 Load)

| Run ID | Configuration | CPU Cores | Memory (MB) | Egress (logs/s) | Delivery Ratio | Backlog Trend |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `phase-c-w2-r1` | `workers=2` | 2.49 | 405 | 24,072 | 1.0 | down |
| `phase-c-w2-r2` | `workers=2` | 2.519 | 417 | 23,915 | 1.0 | down |
| `phase-c-w2-r3` | `workers=2` | 2.517 | 404 | 23,911 | 1.0 | down |
| **Average (w=2)** | | **2.508** | **408.7** | **23,966** | **1.0** | |
| `phase-c-w1-r1` | `workers=1` | 2.523 | 355 | 24,049 | 1.0 | down |
| `phase-c-w1-r2` | `workers=1` | 2.571 | 409 | 24,021 | 1.0 | down |
| `phase-c-w1-r3` | `workers=1` | 2.533 | 385 | 23,981 | 1.0 | down |
| **Average (w=1)** | | **2.542** | **383.0** | **24,017** | **1.0** | |

### Analysis:
- **CPU Resource Usage**: Average CPU core usage is nearly identical (`2.508` cores for `workers=2` vs `2.542` cores for `workers=1`). At 24k records/sec, Fluent Bit is not CPU-bound, and the thread overhead has a negligible impact on total CPU usage.
- **Memory Footprint**: `workers=1` yields a **~6.3% memory reduction** (383.0 MB vs 408.7 MB) due to fewer active thread contexts and buffers.
- **Delivery & Stability**: Both configurations achieved 100% stable delivery, zero errors, zero retries, and successfully drained backlog.

---

## 5. Next Steps
We are transitioning to Phase D (Package for Upstream):
- Prepare separate patches for the GKE log attribution hardening (Phase B) and the concurrency safety fixes (Phase B).
- Format and push clean, lint-passing commits matching upstream guidelines.
