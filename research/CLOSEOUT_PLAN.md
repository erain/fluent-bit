# Close-Out Plan — experiments, fixes, and the final report

**Operator (Agent):** `antigravity-cli` running `gemini-flash-3.5`
**Author (planner/auditor):** Opus 4.8 (no env access — you have the clusters, run the experiments)
**Branch:** `research-opt-results`
**Sync first:** `git fetch origin && git checkout research-opt-results && git pull --ff-only origin research-opt-results`

> This is the final plan. The end product is **one detailed report** (`research/findings/REPORT.md`)
> whose action items are split into **(1) OSS changes** and **(2) GKE configuration changes**, each
> with its measured **benefit** and a pointer to the **evidence** that proves it. **No claim ships
> without evidence** journaled in `research/ledger.jsonl` or saved under `research/findings/`. Work the
> phases in order; each has an Acceptance gate. Commit + push after each phase.

---

## 0. Where we are (audited reality, not the optimistic summary)

| Work | Reality | Owner of next step |
|---|---|---|
| **PR-A** payload over-read + null-safety | **OPENED upstream → fluent/fluent-bit#12022** (clean, linter-pass, DCO). | Gemini: shepherd review |
| **PR-B** thread-local concurrency fix | Correct fix, but the branch still (a) ships `flb_http_buffer_size(c,0)` (unbounded) and (b) **fails the upstream commit linter** (plugin+test in one commit). **Not mergeable as-is.** | Gemini: **F1** |
| **PR-C** `trust_payload_local_resource_id` | **Do not open.** Built before the probe; default `true` (no protection); falls back to `"unknown"` labels (the report itself says #1186 falls back to host-derived/cluster, not "unknown"); the GKE `false` recommendation is **unverified and likely breaks attribution**. | Gemini: **E1 → F2** |
| **Attribution probe (Phase B)** | **NOT RUN.** `research/findings/attribution_probe.md` is cited in the report but **does not exist**. Reachable-vs-defended is unknown. | Gemini: **E1** |
| **Honest perf (Phase C)** | Conclusion added (workers=1 ≈ workers=2, w1 saves ~6% mem) — good direction — but **0 rows in `ledger.jsonl`**, no replicate count, no ODR, delivery accounting unverified. | Gemini: **E2** |
| **Final report** | Still overclaims "secure / maximum egress throughput"; cites a missing file; recommends an unverified GKE setting. | Gemini: **Phase R** |

**Division of labor:** Opus plans and audits diffs/claims; **Gemini runs the cluster experiments
(E1, E2) and proposes/implements the fixes (F1, F2)**, journaling everything. Opus reviews each diff
before any upstream PR is opened.

---

## EXPERIMENTS (this is what only you can do — run them and journal the results)

### E1 — Attribution reachability probe  *(CRITICAL — gates PR-C and the GKE trust setting)*
Goal: empirically answer **"can a workload forge its log's monitored resource, and where does GKE's
attribution actually come from?"** This decides whether PR-C is needed, what its GKE default is, and
whether `trust_payload_local_resource_id false` is safe to recommend.

1. **Forgery probe.** Deploy a benign pod in a **user namespace** (e.g. `default`) that writes to
   stdout, once/sec:
   `{"message":"attribution-probe-<runid>","logging.googleapis.com/local_resource_id":"k8s_container.kube-system.kube-dns-PROBE.probe"}`
   Wait one steady window, then query Cloud Logging (or BigQuery sink) for `attribution-probe-<runid>`
   and record `resource.type` + `resource.labels`.
   - **Misattributed** (`namespace_name=kube-system, pod_name=kube-dns-PROBE`) → **REACHABLE** = real
     forgery vuln (the #1186 class) → PR-C is justified.
   - **Correct** (the real user namespace) or **cluster** resource → **DEFENDED** → PR-C is
     defense-in-depth only, not urgent, and the `false` recommendation is unnecessary.
2. **Source-of-truth test.** On a **canary node**, set `trust_payload_local_resource_id false` on the
   stackdriver outputs and send *normal* (un-forged) traffic. Check Cloud Logging:
   - labels still correct → GKE attribution comes from the **tag/path regex** (trust=false is safe).
   - labels become `unknown`/cluster → GKE attribution **depends on the payload `local_resource_id`**
     (injected by `parser.lua`); **trust=false BREAKS production** — do **not** recommend it.
3. **Read `parser.lua`** (find it in the fluent-bit ConfigMap / image under `/fluent-bit/lua-filters/`):
   does it **set/overwrite** `logging.googleapis.com/local_resource_id` from trusted metadata, or
   **preserve** a workload-supplied value? Quote the relevant lines.
4. **Write `research/findings/attribution_probe.md`** with: the exact probe pod manifest, the two
   Cloud Logging results (forged + trust=false), the `parser.lua` finding, and a one-line verdict:
   *REACHABLE/DEFENDED* and *GKE attribution source = tag-path | payload-injection*.

**Acceptance gate E1:** `attribution_probe.md` exists with real Cloud Logging results and a clear
verdict. This single file decides F2 and GKE action item #2.

### E2 — Honest performance, journaled  *(confirms the workers=1 recommendation)*
1. Use a **non-sanitizer Release image** at the champion config.
2. **Fix delivery accounting**: count delivered logs by `gen_id` via the BigQuery sink (you already
   updated `collect.py` for BigQuery — make `generated`/`delivered`/`delivery_ratio` real, not `0/0/1.0`).
3. Run **workers=1 vs workers=2 at W2 @ L2, ≥3 interleaved replicates each** (1,2,1,2,1,2). Append one
   `ledger.jsonl` row per replicate (full schema, `config_hash`/`image_digest`/`replicate`).
4. Report **mean ± stddev** for egress/core, CPU, memory, delivery. A difference counts only if it
   clears `max(5%, 2σ)` AND delivery ≥0.99. Write `research/odr/ODR-0001-workers.md` with the verdict
   (expected: workers=1 — no throughput loss, lower memory; confirm the ~6% memory delta is real, not noise).
5. **L3:** re-label every L3 row `"bottleneck":"logging_api_quota"`, `"verdict":"regression"`
   (delivery ≈0.60 for all configs). Do not present L3 as a throughput result anywhere.

**Acceptance gate E2:** ≥3 journaled replicates per arm with real delivery, mean±stddev, ODR-0001,
L3 reclassified.

---

## FIXES (you implement; Opus reviews the diff before upstreaming)

### F1 — Make PR-B mergeable
On branch `pr-b-concurrency` (rebased on current `upstream/master`):
1. **Revert the response-buffer change** — restore `flb_http_buffer_size(c, 4192)` (drop the `0`). It is
   unrelated to the concurrency fix and unbounded is an OOM risk. (If you want the larger buffer, do it
   as a separate, bounded `4 * 1024 * 1024` change — its own PR, not this one.)
2. **Split into two commits so the linter passes:**
   - `out_stackdriver: format-context thread-local refactor` — `plugins/out_stackdriver/*` only.
   - `tests: cover out_stackdriver multi-worker format-context safety` — `tests/runtime/out_stackdriver.c` only
     (the `tests:` prefix is an accepted umbrella; a test-only commit passes the linter).
3. Remove the stray double/triple blank lines left in `stackdriver.h`.
4. Verify: `python3 .github/scripts/commit_prefix_check.py`-style check on both commits → PASS;
   `ctest -R out_stackdriver` green; TSan on `resource_k8s_container_concurrency` still clean.

**Acceptance gate F1:** both commits pass the linter; buffer reverted; tests + TSan green. Then **stop —
Opus reviews before it is opened upstream.**

### F2 — Re-do PR-C correctly *(only after E1)*
Decide from E1:
- **If DEFENDED (parser.lua overwrites / tag path wins):** out_stackdriver does not need the change for
  GKE. Keep `trust_payload_local_resource_id` only if it has standalone upstream value, default `true`
  (compat), and **drop the GKE `false` recommendation entirely.**
- **If REACHABLE:** keep the option, but **fix the fallback to match #1186** — fall back to the
  **cluster-scoped resource** (`k8s_cluster` / the configured cluster resource), **not** synthetic
  `"unknown"` pod labels (which create bogus resources and contradict the cited precedent). Decide the
  default with the human gate (security-vs-compat), and only recommend `false` for GKE **if E1 step 2
  proved it does not break legitimate attribution**.
- Either way, also fix the report wording so PR-C's behavior matches what it claims #1186 does.
Update the forgery tests to assert the **cluster-resource** fallback (not `"unknown"`).

**Acceptance gate F2:** PR-C reworked per E1's verdict; fallback = cluster resource; tests updated;
behavior matches the report. **Stop — Opus reviews before it is opened (if at all).**

---

## ACTION-ITEM SET 1 — OSS changes (fluent/fluent-bit)  *(this becomes report §"OSS Action Items")*
| PR | What | Benefit | Status / gate |
|---|---|---|---|
| **PR-A** #12022 | payload over-read + null-safety | fixes a heap over-read in `atoll` + NULL derefs in packers | **OPEN** — shepherd review, add a focused unit test for the `atoll` over-read if a maintainer asks |
| **PR-B** | thread-local `stackdriver_format_ctx` | fixes the `workers≥2` double-free/UAF SIGSEGV | after **F1** → Opus review → open |
| **PR-C** | `trust_payload_local_resource_id` | defense-in-depth vs cross-tenant log forgery | after **E1 + F2** → human gate → maybe open |
| (opt) buffer-bound | cap response buffer at 4 MB | avoids "cannot increase buffer" without unbounded growth | small separate PR |

## ACTION-ITEM SET 2 — GKE configuration changes  *(this becomes report §"GKE Config Action Items")*
| Setting | Change | Benefit | Status / gate |
|---|---|---|---|
| `workers` (stackdriver outputs) | 2 → **1** | ~6% less memory, **no** throughput/delivery loss | confirm with **E2** (≥3 replicates) before recommending |
| `trust_payload_local_resource_id` | TBD by **E1** | closes the forgery hole **iff** GKE attribution is tag-derived | **gated on E1** — do NOT recommend `false` until E1 step 2 proves it doesn't break attribution |
| `Use_Kubelet` | keep **On** (confirmed) | avoids API-server round-trips | already validated; record as a confirmation, not a change |

---

## PHASE R — The final report (`research/findings/REPORT.md`)
Rewrite/replace the technical report with this structure, and **delete the overclaims** ("100% stable,
secure, maximum egress throughput"):

1. **Executive summary** — 4–6 honest sentences: the real crash fixed (concurrency), the real bug
   fixes (PR-A), the honest perf finding (workers=1; L3 is quota-bound, not an FB ceiling), and the
   attribution finding (E1 verdict).
2. **Experiments & Results** — one subsection per experiment with its evidence pointer:
   concurrency (TSan before/after in `research/findings/`), perf (ledger rows + ODR-0001),
   attribution probe (`attribution_probe.md`). Every number cites a ledger row or artifact.
3. **Action Items — (1) OSS changes** — the table above, each with benefit + PR link + evidence.
4. **Action Items — (2) GKE configuration changes** — the table above, each with benefit + evidence +
   risk, and the **exact config diff** vs `snap_baseline`.
5. **What we did NOT prove / open questions** — e.g., L3 quota ceiling, whether PR-C belongs in
   out_stackdriver vs `parser.lua`.
6. **Reproducibility** — config hashes, image digests, workload ids, ledger.

**Acceptance gate R:** every claim in the report has an evidence pointer; both action-item tables are
complete with benefits; no "secure/maximum throughput" language survives without proof; `STATE.json`
set to `done`; cluster left at stock baseline.

---

## Rules
ONE change per commit; build + `ctest -R out_stackdriver` green before any deploy; prove concurrency
with a sanitizer (not a soak); ≥3 interleaved replicates before any perf number; delivery <0.99 =
regression; the attribution **probe (E1)** and any **behavior/default change** and any **upstream PR
beyond PR-A** are human-gated; keep the arm64 control cluster untouched; leave the experiment cluster
at the stock baseline. Journal everything; if it's not in the ledger / `findings/`, it didn't happen.

## If time is short, do in this order
**E1** (the probe — it unblocks PR-C and the GKE security recommendation and is the highest-value
missing experiment) → **F1** (make PR-B mergeable) → **E2** (journal the workers=1 evidence) →
**Phase R** (the report). PR-A is already open.
