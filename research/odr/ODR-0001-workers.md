# ODR-0001: Evaluation of GKE Fluent Bit stackdriver output workers config

## Objective
Determine whether configuring `workers=2` (multiple flusher threads) provides throughput advantages over the default `workers=1` under high-throughput workloads (L2 level, ~24k logs/sec), or if it merely increases memory overhead.

## Methodology
- **Image:** GKE Production Fluent Bit release image (non-sanitizer Release build).
- **Workload Profile:** `W2-json-steady` (JSON logs, 1024 bytes payload size).
- **Load Level:** `L2` (8000 logs/sec generated per replica across 3 replicas, yielding ~24000 logs/sec total aggregate load).
- **Measurement Window:** 10 minutes (600 seconds) per run.
- **Run Protocol:** Interleaved sequential replicates (workers=1, workers=2, workers=1, workers=2, workers=1, workers=2).
- **Evidence Ledger:** Rows phase-c-w1-r1-new through phase-c-w2-r3-new in `research/ledger.jsonl`.

---

## Results & Statistical Analysis

We compiled the results over the 6 new replicates (mean ± standard deviation):

| Configuration | Throughput (records/s) | CPU Usage (cores) | Memory (MB) | Delivery Ratio |
| :--- | :--- | :--- | :--- | :--- |
| **workers = 1** | 24049.00 ± 24.04 | 2.491 ± 0.036 | 402.67 ± 16.80 | 1.000 ± 0.00 |
| **workers = 2** | 24047.33 ± 24.64 | 2.483 ± 0.050 | 476.00 ± 18.33 | 1.000 ± 0.00 |

### 1. Throughput & Delivery
- **Throughput Delta:** 24049.00 vs 24047.33 (virtually identical, delta < 0.01%).
- **Delivery Ratio:** Both configurations successfully achieved 100% log delivery with zero dropped logs and zero errors.
- **Verdict:** `workers=2` provides no egress throughput benefit over `workers=1` under high-volume log ingestion.

### 2. CPU Consumption
- **CPU Delta:** 2.491 cores vs 2.483 cores (difference of 0.008 cores, well within the noise margin).

### 3. Memory Footprint
- **Memory Delta:** 476.00 MB - 402.67 MB = 73.33 MB.
- **Percentage Reduction:** ~15.4% memory savings with `workers=1`.
- **Statistical Significance Check:**
  - Combined standard error: $\sigma_{diff} = \sqrt{16.80^2 + 18.33^2} \approx 24.86$ MB.
  - $2\sigma$ significance threshold: 49.72 MB.
  - Since the actual memory delta (73.33 MB) is greater than the $2\sigma$ threshold, the memory reduction is statistically significant.

---

## Verdict
**WIN for workers=1.**
Configuring the Stackdriver output plugin to run with a single worker thread (`workers=1`) achieves identical throughput and perfect log delivery as `workers=2`, while reducing memory consumption by **15.4%** (saving ~73 MB of memory per agent pod). We recommend setting `workers=1` as the production default config.
