# Multi-Day Autonomous Research Program — Fluent Bit → Stackdriver (LoggingV3) on GKE

**Operator (Agent):** `antigravity-cli` running `gemini-flash-3.5`
**Author (planner, no env access):** Opus 4.8
**Mode:** Multi-day, semi-autonomous "loop engineering." Agent runs continuously; human reviews once per day at a gate.
**Capabilities granted:** read+modify the live Fluent Bit **config**; read, **modify, and rebuild** the Fluent Bit **source**; deploy custom container images to the **experiment** cluster; create/run arbitrary **log generators**.
**Targets:** GKE `fluent-bit-agent-x86` (n4-standard-32) + `fluent-bit-agent-arm64` (n4a-standard-32), project `timbai-gke-dev`, *test* env (synthetic data only — drive freely).
**Component under test:** GKE Fluent Bit `1.36.3-gke.0` (LoggingV3, `out_stackdriver`).
**Local source tree = Fluent Bit 5.0.5** — version reconciliation is Day-0 Task 1 (Appendix A).

---

## PART 0 — Mission & success metrics

Run a **closed-loop research program** (autoresearch): form hypotheses → run controlled experiments → measure → learn → record → generate new hypotheses. Optimize Fluent Bit's logging pipeline for **throughput and resource utilization**, across **config** and **source code**, without increasing data loss. The *loop and its documented knowledge* are as much a deliverable as the optimizations.

**North-star metric:** sustained **delivered** log throughput per CPU-core (records/s per core), at delivery-ratio ≥ 0.99.

**Guardrail metrics (a win may not regress these):** delivery ratio (≥0.99), dropped records, failed retries, error rate, p99 end-to-end latency, memory headroom (no OOM), node neighbor impact.

**Prior-art finding — memory stability is part of throughput, not a separate axis.** A completed Google investigation of FB 0.422 in *this exact* LoggingV3 environment (Jun 2026, [go/fluent-bit-memleak](https://goto.google.com/fluent-bit-memleak)) found **no leaks** (15 h steady load → net **−2.3 MB** heap). Under high load, heap growth is **transient and legitimate**: ~59% in-memory chunk buffering (`cio_memfs_write` — logs waiting to flush) + ~29% tail file-tracking metadata (`flb_tail_file_append` — scales with files/pods); both recover once the backlog drains. **OOM kills were caused by CPU starvation stalling flushes — not by leaks.**

**Central thesis driving this program:** the binding constraint is **flush throughput, gated by CPU**. Every CPU saving in the flush/egress path (gzip cost, output `workers`, allocation churn, jemalloc) *simultaneously* (a) raises throughput, (b) raises CPU-utilization efficiency, and (c) shrinks the `cio_memfs` backlog that leads to OOM. Memory stability is therefore a **measured downstream outcome** of the throughput work — track it, don't chase it separately. Do **not** re-litigate "is there a leak" — build on the answer (no).

**Three optimization objectives (from the brief):**
1. Problems in **config** files.
2. Problems / bottlenecks / bugs in **source**, especially `out_stackdriver`.
3. **Optimal parameters** maximizing resource utilization & throughput — per architecture.

---

## PART 1 — Operating model

### 1.1 Loop engineering, three nested loops
- **Inner loop (minutes):** one experiment = one change → measure → keep/revert → record. (§5.1 config, §5.2 code.)
- **Daily loop (hours):** dequeue & run a day's experiments, then synthesize a daily report + update state. (§5.3)
- **Meta loop (daily, at the human gate):** improve the *apparatus* itself — better metrics, better load, better hypotheses — and re-examine assumptions. The human engineers the loop; the agent runs it. (§5.4)

### 1.2 Champion / challenger
Maintain a **champion** = current best (config + image digest) that has been *replicated*. Every experiment is a **challenger** vs champion. A challenger is promoted to champion only after it **wins twice** (initial + a confirmation replicate run on a later cycle, ideally interleaved). This is the guard against one-night false positives (regression to the mean / multiple-comparison luck).

### 1.3 Two clusters, three roles
- **Experiment cluster** = x86 (default). Receives config + image changes.
- **Control cluster** = arm64 (default). **Untouched baseline.** Its only job: detect environment drift. If control's baseline metrics move between days, discount experiment deltas accordingly.
- **Canary (within experiment cluster):** for *code/image* changes, roll out to **one node first**, validate, then the other two. (§4C)
- Phase 4 (Day ~5+) deliberately swaps roles for **architecture-specific** experiments (gzip, workers) — snapshot first, and never run config + arch experiments simultaneously on both (you'd lose your drift reference).

### 1.4 Human gate (once/day, ~15 min of human time)
Each morning the human: reads `DAILY_REPORT_<n>.md`; approves/rejects champion promotions; **approves any source change before it is built/canaried** (code changes are gated; config changes are not); reprioritizes the hypothesis backlog; and applies any loop improvements. The agent must **pause for this gate** when a code change is ready, and otherwise continue config work autonomously.

---

## PART 2 — Golden rules (never break; superset of the overnight rules)

- **G1 One variable per experiment.** Config key OR one code patch OR one build flag OR one workload knob — never combined (except explicit, labeled "combination/stacking" tests late in a cycle).
- **G2 Always revertible.** Config: snapshot before change. Code/image: pin by **immutable digest**, keep previous digest; rollback = repoint image. Test the rollback once before relying on it.
- **G3 Keep the control cluster untouched.** It is your drift detector.
- **G4 Steady state only.** ≥3 min warmup discarded + ≥10 min measured window; **≥3 replicates** for any result you intend to act on.
- **G5 No silent data loss.** Always compute delivered/generated. A throughput "win" with delivery <0.99 is a **regression**.
- **G6 Stay in scope.** Modify only: Fluent Bit ConfigMap, DaemonSet (incl. pod resources & image), and your own loadgen namespace, on the **experiment** cluster. Never: other namespaces/workloads, node/cluster/IAM config, KAM, the control cluster, the tail DB files.
- **G7 Code changes are gated + tested.** A source patch must (a) **pass the relevant test suite** (Appendix B), (b) pass **A/A validation** (self-built baseline ≈ stock), and (c) get **human approval**, before canary deploy.
- **G8 Reproducibility is mandatory.** Every experiment record pins: config hash, image digest, workload spec id, cluster, window, replicate index. If it can't be reproduced from the record, it doesn't count.
- **G9 Journal everything, cold-start safe.** All state lives in the research repo (§3) so the agent can resume from zero context each session by reading it.
- **G10 On any regression/health failure/uncertainty:** auto-revert to champion, mark the experiment, log it, continue. Never end a session with the experiment cluster broken or mid-rollout.

---

## PART 3 — Durable research repository (survives across days & cold starts)

Maintain this directory. **First action of every session: read all of it** to rebuild context.

```
research/
  PROGRAM.md                 # this file (read-only reference)
  STATE.json                 # cursor: current day, phase, champion ids, budget used, last action
  assumptions.md             # Assumptions Registry (§3.1)
  hypotheses.jsonl           # Hypothesis backlog, scored & prioritized (§3.2)
  ledger.jsonl              # Experiment ledger — immutable, one row per replicate (§3.3)
  odr/ODR-XXXX.md           # Optimization Decision Records (§3.4)
  champion/                  # current best: champion_cm.yaml + champion_image.txt(digest) + champion_metrics.json
  snapshots/                 # snap_<label>_cm.yaml / _ds.yaml (rollback artifacts)
  harness/                   # collect.sh, loadgen manifests, build+deploy scripts, profile scripts
  daily/DAILY_REPORT_<n>.md  # one per day (human gate input)
  findings/                  # source_findings.md, config_problems.md, profiles/ (flamegraphs etc.)
  raw/                       # raw metric scrapes, profile dumps, gcloud logging counts
```

### 3.1 Assumptions Registry (`assumptions.md`)

```
ID: A-001
Statement: "Baseline egress is CPU-bound on gzip compression, not network."
Status: assumed | validated | refuted | partial
Evidence: <ledger exp ids / profile path>
Owner-loop: <which experiment tests this>
Created/Updated: Day n
Notes/Consequences-if-wrong:
```

Seed it Day 0 with at least: A-001; A-002 "self-built image ≈ stock GKE image (A/A holds)"; A-003 "L2 load saturates baseline"; A-004 "delivery countable via gen_id in Cloud Logging"; A-005 "pod CPU limit < node capacity is the first ceiling"; A-006 "the local 5.0.5 source matches the deployed binary closely enough that source findings transfer".
Also seed: **A-007** "no memory leak in FB app code (15 h steady → -2.3 MB)"; **A-008** "OOM under load is caused by CPU starvation stalling flushes, not leaks"; **A-009** "≈59% of load-time heap growth is transient cio_memfs_write chunk buffering"; **A-010** "≈29% is flb_tail_file_append metadata that scales with file/pod count".

### 3.2 Hypothesis backlog (`hypotheses.jsonl`)
```json
{"id":"H-014","statement":"workers=4 raises egress/core ≥15% at L2","track":"config|code|workload", "param":"workers","predicted_direction":"up","expected_effect":"medium", "score":{"impact":5,"confidence":3,"effort":1,"risk":1},"priority":0.0, "source":"derived-from H-003 result | profile | assumption A-001","status":"open|running|done","result_exp":""}
```

### 3.3 Experiment ledger (`ledger.jsonl`)
```json
{"exp_id":"H014-workers4-r2","hyp_id":"H-014","cluster":"x86","track":"config", "config_hash":"sha256:…","image_digest":"sha256:…","workload_id":"W2-json-steady","load":"L2", "changed":{"key":"workers","from":"1","to":"4"},"replicate":2,"window_s":600, "egress_records_s":0,"egress_bytes_s":0,"ingest_records_s":0, "cpu_cores_used":0.0,"cpu_limit_cores":0,"cpu_util_pct":0,"mem_used_mb":0, "per_core_efficiency":0,"p99_latency_ms":0, "retries":0,"retries_failed":0,"errors":0,"dropped":0,"backlog_trend":"flat", "delivery_ratio":1.0,"control_drift_pct":0, "verdict":"win|neutral|regression","promoted":false,"notes":""}
```

### 3.4 Optimization Decision Record (`odr/ODR-XXXX.md`)
```
# ODR-0007: Enable gzip compression on out_stackdriver
...
```

---

## PART 4 — The harness

### 4A. Observability
- **Metrics:** scrape `:2020/api/v2/metrics/prometheus` (fallback v1) + `:2020/api/v1/storage` from all 3 pods. Core SLIs: input/output records & bytes, `output_errors_total`, `output_retries_total`, `output_retries_failed_total`, `output_dropped_records_total`, filter add/drop, storage chunks.
- **Resource:** `kubectl top pod/node`.
- **Delivery:** Cloud Logging count by `gen_id` over window (§4B.4).
- **Profiling:** CPU (`perf record -F 99 -p <pid> -g`) & Heap (`MALLOC_CONF` for jemalloc heap profiler).

### 4B. Load-generation suite
- **Matrix:** W1 plaintext, W2 JSON medium, W3 large lines, W4 multiline, W5 high-label-cardinality JSON, W6 bursty, W7 rotating files, W8 high pod density (100-200 pods/node).
- **Levels:** L1 nominal, L2 stress, L3 burst.
- **Delivery accounting:** `gcloud logging read 'jsonPayload.gen_id="<id>" AND timestamp>=...'`

### 4C. Build / deploy / rollback pipeline
- **Config:** edit ConfigMap → restart ds → verify.
- **Source:** Reconcile versions → patch → build locally (`ctest`) → docker build & push digest → A/A validation → Canary 1 node → Rollout 3 nodes.

### 4D. Measurement protocol & statistics
- **Noise floor:** repeat baseline 5× to compute stddev. Set threshold = max(5%, 2×stddev).
- **Replication:** N≥3 windows.
- **Drift control:** control-cluster baseline.

---

## PART 5 — The loops

### 5.1 Inner loop — CONFIG experiment
(See PROGRAM.md flowchart or spec.)

### 5.2 Inner loop — CODE experiment (adds gates)
(See PROGRAM.md flowchart or spec.)

### 5.3 Daily loop
(See PROGRAM.md flowchart or spec.)

### 5.4 Meta loop
(See PROGRAM.md flowchart or spec.)

---

## PART 6 — Multi-day schedule
- **Day 0:** Bootstrap & validate the harness. Version reconciliation; noise-floor 5× baseline; calibration.
- **Day 1:** Config sweep.
- **Day 2:** Profiling & hotspot map.
- **Day 3+:** Code experiments (gated).
- **Day 4:** Workload diversity & robustness.
- **Day 5:** Architecture A/B + stacking + validation.
- **Ongoing:** Regression guard.

---

## PART 7 — Experiment catalogs

### 7.1 Config catalog
- P0: pod CPU/mem limits
- P1: `workers` (1→2→4→8→16)
- P1b: `net.max_worker_connections`
- P2: `compress gzip`
- P3: keepalive (`net.keepalive`, etc.)
- P4: `flush` (1→5→10s)
- P5: tail buffer sizing & filesystem storage config.
- P6: kubernetes filter `Use_Kubelet On` and TTL cache settings.
- P7: retry limits & backoff base/cap.

### 7.2 Source / code catalog
- C1: gzip compression level (`src/flb_gzip.c:193` default 6)
- C2: jemalloc (`FLB_JEMALLOC=On`)
- C3: payload/JSON buffer reuse
- C4: metadata/resource caching
- C5: request batching/splitting alignment
- C6: partial-failure retry amplification prevention
- C7: compression off-thread/pipelining
- C8: build optimization flags

---

## PART 8 — Governance: safety, stop conditions, escalation
- **Health gates:** pods Ready, node Ready, no CrashLoop.
- **Auto-revert triggers:** CrashLoop/OOM, delivery <0.5, drops 10×, error spike, tests red.
- **Human approval:** required for source/image changes before canary deploy.
- **Budget:** stop experimenting at 85% budget, never start with <45 min.

---

## APPENDIX A — Version reconciliation
- Pod binary version vs local git tree version.
- Use `gcr.io/timbai-anthos-dev/fluent-bit:0.422-debug` as A/A baseline/profiling reference.
- Source path: `/home/ubuntu/src/fluent-bit`

## APPENDIX B — Build & test (local)
- Build flags: `-DCMAKE_BUILD_TYPE=RelWithDebInfo -DFLB_RELEASE=On -DFLB_TESTS_RUNTIME=On -DFLB_TESTS_INTERNAL=On`

## APPENDIX G — Memory & CPU profiling playbook
- Heap profiling via `MALLOC_CONF` and `jeprof`.
- CPU profiling via `perf`.
