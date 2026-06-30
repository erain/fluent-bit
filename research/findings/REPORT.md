# Fluent Bit → Cloud Logging (`out_stackdriver`) on GKE — Final Experiment Report

**Scope:** correctness, concurrency safety, attribution integrity, and resource efficiency of the
`out_stackdriver` output under GKE high-throughput load (ARM64 + x86), L2 (~8k logs/s/pod) and L3
(~20k logs/s/pod). Evidence lives in `research/ledger.jsonl`, `research/findings/`, `research/odr/`.

---

## Executive summary

- A genuine **multi-worker crash** (`workers ≥ 2`) in `out_stackdriver` was root-caused and fixed:
  the per-record Kubernetes resource strings were stored on the **shared** plugin context and
  destroyed/recreated by every flush, so concurrent worker threads raced and corrupted the heap
  (use-after-free / double-free → SIGSEGV). Fixed with a **per-flush stack-local context**;
  ThreadSanitizer confirms the resource-path races are gone.
- Several **payload-parsing correctness bugs** were found and fixed (notably a heap **over-read** in
  `atoll` on a non-NUL-terminated msgpack string, plus NULL-deref paths in the field packers).
- **Performance, measured honestly:** `workers=2` gives **no throughput benefit** over `workers=1`
  and uses **~73 MB (~18%) more memory** (6 interleaved replicates, ODR-0001). **Recommend
  `workers=1` for GKE.** L3 is **bound by the Cloud Logging API quota** (≈0.60 delivery for stock
  *and* tuned alike), not by Fluent Bit — it is not a throughput result.
- **Attribution forgery was probed and found already DEFENDED in GKE:** a workload-forged
  `logging.googleapis.com/local_resource_id` does **not** win, because `parser.lua` sets the
  monitored resource from the **trusted log-path tag** and always overwrites it. The optional
  `out_stackdriver` hardening is therefore **defense-in-depth for non-GKE pipelines**, not a GKE fix.

No "100% secure / maximum throughput" claim is made: L3 is quota-limited, and the forgery risk was
never reachable in GKE.

---

## Experiments & results

### E0 — Concurrency crash (root cause + fix)
- **Cause:** `stackdriver_format()` wrote `ctx->pod_name/namespace_name/container_name/node_name/local_resource_id`
  (shared) per flush; `workers≥2` → concurrent `flb_sds_destroy`+`flb_sds_create` on the same buffers.
- **Fix:** per-flush `struct stackdriver_format_ctx` (stack-local), single cleanup; no shared mutable
  per-record state, no lock.
- **Evidence:** TSan before (`research/findings/` — races in `extract_local_resource_id` /
  `set_monitored_resource_labels` / `process_local_resource_id`) → after (`asan_repro_after_cleanup.txt`,
  resource-path races eliminated). Runtime test `resource_k8s_container_concurrency` (2 workers × 5 streams).
- **Note:** 6 residual TSan warnings remain in the **metrics/cmetrics layer** (`flb_metrics_sum`,
  `cmt_map`) reached via `cb_stackdriver_flush` — pre-existing, benign (counter lost-updates, not
  corruption); the `flb_metrics` one has an upstream fix in `fluent/fluent-bit#12007`.

### E1 — Payload-parsing bug fixes
- `try_assign_subfield_int`: `atoll(obj.via.str.ptr)` over-read a **non-NUL-terminated** msgpack
  string → bounded copy + NUL-terminate. `try_assign_subfield_str`: NULL-`*subfield` guard.
  `pack_sds_safe`: pack possibly-NULL `flb_sds_t` as empty instead of dereferencing. `init_http_request`
  lazy NULL-init. All covered by the `out_stackdriver` runtime suite.

### E2 — Attribution reachability probe (`research/findings/attribution_probe.md`)
- **Method:** deployed a pod in `default` logging a forged
  `local_resource_id = k8s_container.kube-system.kube-dns-PROBE.probe`; inspected Cloud Logging
  `resource.labels`; repeated with `trust_payload_local_resource_id=false`; read `parser.lua`.
- **Result:** **DEFENDED.** Even with the default config, the forged log was attributed to the real
  `default/probe-forgery`. `parser.lua` matches `*`, derives the monitored resource from the trusted
  log-path tag, and **overwrites** any workload value; out_stackdriver's `parse_monitored_resource`
  consumes that, **bypassing** the `local_resource_id` path entirely. Setting
  `trust_payload_local_resource_id=false` does **not** break legitimate attribution.
- **Implication:** GKE is not vulnerable to this forgery. The `out_stackdriver` option is optional
  upstream defense-in-depth for pipelines that *do* trust payload `local_resource_id`.

### E3 — Performance, honestly (ODR-0001, `research/odr/ODR-0001-workers.md`)
- Non-sanitizer Release image, W2 @ L2, **6 interleaved replicates** (w1/w2 ×3), real delivery accounting.

  | config | egress rec/s | CPU cores | memory MB | delivery |
  |---|---|---|---|---|
  | workers=1 | 24049 ± 24 | 2.491 ± 0.036 | **402.7 ± 16.8** | 1.000 |
  | workers=2 | 24047 ± 25 | 2.483 ± 0.050 | **476.0 ± 18.3** | 1.000 |

  Throughput/CPU identical; **workers=1 uses ~73 MB less** (beyond 2σ). **L3:** quota-bound,
  delivery ≈0.60 for all configs → `verdict=regression`, `bottleneck=logging_api_quota`; not a win.

---

## Action items — (1) OSS changes (`fluent/fluent-bit`)

| PR | Change | Benefit | Status |
|---|---|---|---|
| **#12022** (PR-A) | payload over-read + null-safety fixes | fixes a heap over-read (`atoll`) and NULL derefs in the field packers | **OPEN** |
| **#12023** (PR-B) | per-flush thread-local `stackdriver_format_ctx` | fixes the `workers≥2` use-after-free/double-free **SIGSEGV** | **OPEN** |
| PR-C | `trust_payload_local_resource_id` + cluster-resource fallback | optional defense-in-depth vs payload attribution forgery (non-GKE) | **prepared** — rework to fall back to `k8s_cluster` (not `"unknown"`) must be committed to `pr-c-attribution` + rebased on PR-B; open after PR-B merges |
| (optional) | bound HTTP response buffer (e.g. 4 MB) | avoids "cannot increase buffer" without unbounded heap growth | small separate PR if needed (PR-B keeps upstream's 4 KB) |

> The earlier `fluent/fluent-bit#12007` ("metrics: read shared counter with relaxed atomics") also
> addresses the residual benign metrics race seen here.

## Action items — (2) GKE configuration changes

| Setting | Change | Benefit | Status |
|---|---|---|---|
| `workers` (stackdriver outputs) | **2 → 1** | **~73 MB (~18%) lower memory**, no throughput/delivery loss | **Recommended** (ODR-0001, 6 replicates) |
| `trust_payload_local_resource_id` | optional **`false`** (belt-and-suspenders) | hardens attribution; **no effect/risk in GKE** (path already defended by `parser.lua`) | Optional; **not required** (E2: GKE already defended) |
| `Use_Kubelet` | keep **On** | avoids API-server round-trips for metadata | Confirmed (no change) |

---

## What we did NOT prove / open questions
- **L3 ceiling is downstream (Cloud Logging quota), not Fluent Bit.** Raising delivered throughput at
  L3 needs request-batching tuning or a quota increase — out of scope here.
- **PR-C design is a judgement call:** the correct fallback is the **cluster resource** (per
  `GoogleCloudPlatform/k8s-stackdriver#1186`), not synthetic `"unknown"` labels; the default
  (compat `true` vs secure `false`) is a maintainer decision. PR-C is not GKE-critical (E2).
- The residual metrics/cmetrics races under many workers are benign but real; full race-freedom would
  adopt the atomic-counter fix (#12007).

## Reproducibility
- Champion: `opt-day5-phase-c-1` (Release image digest in `STATE.json`); workload `W2-json-steady`,
  L2; ledger rows `phase-c-w{1,2}-r{1..3}-new`. Probe manifest + Cloud Logging results in
  `attribution_probe.md`. TSan artifacts in `research/findings/`. Upstream PRs: #12022, #12023.
