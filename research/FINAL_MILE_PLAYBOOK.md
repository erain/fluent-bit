# Final-Mile Playbook — close out the research (reconciled with k8s-stackdriver#1186)

**Operator (Agent):** `antigravity-cli` running `gemini-flash-3.5`
**Author (planner, no env access):** Opus 4.8
**Branch:** `research-opt-results`
**Inputs to read first:** the two prior audits, `research/REMEDIATION_PLAYBOOK.md`, `research/PROGRAM.md`,
`research/STATE.json`, `research/ledger.jsonl`, and **GoogleCloudPlatform/k8s-stackdriver#1186** (MERGED).

> Sync the repo before doing anything: `git fetch origin && git checkout research-opt-results && git pull --ff-only origin research-opt-results`. Work phases in order; do not pass a phase until its **Acceptance gate** is met. Commit + push after each phase. Code/image deploys remain human-gated.

---

## 0. Reconciled status — what is DONE, what is LEFT

| Item | Status | Source |
|---|---|---|
| Concurrency double-free/UAF on resource strings (workers≥2) | **FIXED & correct** — per-flush `struct stackdriver_format_ctx` (stack-local in `stackdriver_format`), all helpers converted, single `cleanup:` frees all 5 fields; TSan shows the resource-path races gone. | commit `e17b0c3cf`, audit #2 |
| Dead state left behind | **OPEN** — `struct flb_stackdriver` still has the 5 fields + `resource_mutex` (init'd/destroyed, never locked); `conf_destroy` still frees the now-NULL fields. Commit message wrongly says "removed". | audit #2 → Phase A |
| Report claim "zero data races in out_stackdriver" | **FALSE** — TSan-after still reports 6 warnings (`flb_metrics_sum`, `cmt_map`) reached via `cb_stackdriver_flush`; benign, pre-existing; the `flb_metrics` one already has upstream fix **fluent/fluent-bit#12007**. | audit #2 → Phase A |
| Perf validation (`collect_metrics.json`) | **INVALID** — single sanitizer-build run, broken delivery accounting (`generated=0/delivered=0/ratio=1.0`), 1154 MB (TSan-inflated), backlog up. | audit #2 → Phase C |
| Genuine bug fixes (atoll over-read, NULL-safety, `pack_sds_safe`, insertId refactor) | **DONE, not isolated** — belong in a separate focused upstream PR + tests. | audit #1/#2 → Phase D |
| `workers=2` config win | **Provisional** — modest L2 gain (~+4% egress, ~−16% CPU), single runs; **L3 is quota-bound** (delivery ≈0.60 stock and tuned alike — NOT a win). | audit #1 → Phase C |
| **Attribution trust (NEW, from #1186)** | **UNAUDITED** — see §1. | this playbook → Phase B |

### How #1186 reconciles with this work
- **#1186 is in a different component** (`event-exporter`, Go) and a different repo (`k8s-stackdriver`). It does **not** touch fluent-bit and does **not** reduce any of the remaining fluent-bit work above.
- **What it gives us is a principle**, now blessed and merged: *resource attribution from an untrusted input must be cross-checked against an RBAC-enforced source of truth; on mismatch/empty, fall back to the cluster-scoped resource (still export the data, just don't misattribute it).* In #1186: trust `event.Namespace` (RBAC-validated), not `event.InvolvedObject.Namespace`; pod resource only if they agree, else `defaultResource`.
- **The log path has the same shape and is currently un-hardened** (Phase B).

---

## 1. The #1186 parallel in the log path (the substantive new finding)

`out_stackdriver` derives the monitored-resource labels (`pod_name`, `namespace_name`,
`container_name`, `node_name`) from one of two sources, in this priority
(`process_local_resource_id`):
1. **the tag** — set by the tail input from the log **file path** (`/var/log/pods/<ns>_<name>_<uid>/...`).
   This is **trusted** (kubelet/runtime create it; a workload cannot rename its own log file). `is_tag_match_regex` → `from_tag=TRUE`.
2. **the payload field `logging.googleapis.com/local_resource_id`** — read by
   `extract_local_resource_id` directly from the **log record body**
   (`get_str_value_from_msgpack_map(map, LOCAL_RESOURCE_ID_KEY)`). This is **workload-writable**: a
   container can emit `{"logging.googleapis.com/local_resource_id":"k8s_container.kube-system.victim.ctr"}`.
   Used when the tag does not match, via `is_local_resource_id_match_regex` / `set_monitored_resource_labels`.

In the GKE pipeline the resource labels are intended to come from the **trusted `parser.lua`** filter
(derived from the trusted path + kubernetes filter). **But `out_stackdriver` itself performs no
cross-check and has no cluster-resource fallback** — it trusts whatever `local_resource_id` is present.
The entire defense currently rests on `parser.lua` overwriting/sanitizing the field. If a workload can
get a `logging.googleapis.com/local_resource_id` past `parser.lua` (it sets the field rather than the
plugin trusting the payload, OR the field survives into the record `out_stackdriver` sees), its logs
are **misattributed to another namespace/pod** — exactly the bug #1186 fixed for events.

**This is the additional work #1186 implies: audit reachability, then add defense-in-depth in
`out_stackdriver` mirroring #1186 (prefer the trusted tag attribution; treat payload-supplied
attribution as untrusted; on mismatch fall back to the cluster/global resource).** Whether to change
behavior is a product decision (some deployments intentionally inject `local_resource_id` for
forwarder/sidecar scenarios) — so this is **audit + propose + human-gate**, not a unilateral change.

---

## PHASE A — Land the concurrency fix cleanly (finish what `e17b0c3cf` started)

Goal: remove the dead state so the fix is upstream-quality and the commit message becomes true.

1. In `plugins/out_stackdriver/stackdriver.h`, **delete** from `struct flb_stackdriver`: `namespace_name`,
   `pod_name`, `container_name`, `node_name`, `local_resource_id` (the per-flush copies now live in
   `struct stackdriver_format_ctx`), and `resource_mutex` + `resource_mutex_initialized`.
2. In `stackdriver.c` `cb_stackdriver_init`: delete the `pthread_mutex_init(&ctx->resource_mutex,...)`
   block and the `resource_mutex_initialized = FLB_TRUE;` line, and the matching destroy in the
   `error:` path.
3. In `stackdriver_conf.c` `flb_stackdriver_conf_destroy`: delete the five
   `flb_sds_destroy(ctx->namespace_name|pod_name|container_name|node_name|local_resource_id)` calls
   and the `resource_mutex` destroy block.
4. `git grep -nE "ctx->(pod_name|namespace_name|container_name|node_name|local_resource_id|resource_mutex)" plugins/out_stackdriver/`
   must return **nothing** after this (only `fmt_ctx->...` remain).
5. Rebuild `RelWithDebInfo`; `ctest -R stackdriver` green (Appendix A of REMEDIATION_PLAYBOOK).
6. Re-run the local **TSan** build + the `resource_k8s_container_concurrency` test (workers=2) under
   TSan → confirm still **no** races in the stackdriver resource path. Save to
   `research/findings/asan_repro_after_cleanup.txt`.
7. Fix the report wording (TECHNICAL_REPORT.md §3.2): replace "Zero data races … in out_stackdriver
   code paths" with the accurate statement — *"resource-metadata races eliminated; the 6 residual TSan
   warnings are pre-existing benign races in the metrics/cmetrics layer (`flb_metrics_sum`,
   `cmt_map`) reached via `cb_stackdriver_flush`; the `flb_metrics` counter race has an upstream fix in
   fluent/fluent-bit#12007."*

**Acceptance gate A:** no `ctx->`(5 fields|resource_mutex) references remain; ctest green; TSan clean in
the resource path; report claim corrected; one coherent commit.

---

## PHASE B — Attribution-trust audit + proposal (the #1186 parallel)

### B1 — Determine reachability empirically (do this before writing any code)
On the experiment cluster, deploy a benign test pod in a **user namespace** that emits a log line with
a **forged** field, e.g.:
`{"message":"attribution-probe","logging.googleapis.com/local_resource_id":"k8s_container.kube-system.kube-dns-PROBE.ctr"}`
Then query Cloud Logging for that message and inspect its `resource.labels`:
- If it lands as `namespace_name=<the real user namespace>` (or under the cluster resource) → the
  trusted path / `parser.lua` won; **not reachable** → record as "defended by parser.lua".
- If it lands as `namespace_name=kube-system, pod_name=kube-dns-PROBE` → **reachable** → confirmed
  misattribution (the #1186 class), high-priority finding.
Repeat with the field placed where the tag would NOT match the k8s regex (to exercise the payload
branch). Capture the Cloud Logging entries to `research/findings/attribution_probe.md`.

### B2 — Read `parser.lua` (the GKE asset) and the filter ordering
Determine whether `parser.lua` *sets* `local_resource_id` from trusted metadata unconditionally
(overwriting any workload value) or *preserves* a workload-supplied value. Record the exact behavior.
This is what decides whether out_stackdriver is the last line of defense.

### B3 — Propose defense-in-depth in `out_stackdriver` (human-gated; mirror #1186)
Regardless of B1's result, write a short proposal (`research/findings/attribution_hardening_proposal.md`)
for a defense-in-depth change, modeled on #1186:
- **Prefer the trusted tag-derived attribution** (already first in `process_local_resource_id` via
  `is_tag_match_regex`); make this the authoritative source when a tag is present.
- **Treat payload-supplied `logging.googleapis.com/local_resource_id` as untrusted** by default: add a
  config flag (e.g. `trust_payload_local_resource_id`, default Off for GKE) gating
  `extract_local_resource_id`'s use of the payload field. When Off and the tag does not yield a
  resource, **fall back to the cluster/`global` resource** (the same "still export, don't misattribute"
  behavior #1186 uses) instead of trusting the payload.
- Reference #1186 in the proposal for the precedent and the fallback rationale.
This is a **behavior change** → must go to the human gate before implementing (some deployments rely on
payload `local_resource_id`). Do **not** flip the default unilaterally.

### B4 — If approved at the gate: implement + test
Implement behind the flag; add runtime tests: (a) forged payload `local_resource_id` with a matching
trusted tag → trusted attribution wins; (b) forged payload with no matching tag + flag Off → cluster
fallback; (c) flag On → legacy behavior preserved. `ctest -R stackdriver` green.

**Acceptance gate B:** reachability determined and documented (`attribution_probe.md`); `parser.lua`
behavior recorded; hardening proposal written and presented at the human gate; code only if approved.

---

## PHASE C — Honest performance (replace the broken artifact)

1. Discard `collect_metrics.json` as evidence. Re-measure on a **non-sanitizer** `RelWithDebInfo`/release
   image (sanitizer builds inflate CPU/memory and are not representative).
2. **Fix delivery accounting** so `generated`/`delivered` are real (Cloud Logging count by `gen_id`,
   PROGRAM §4B.4). A run with `generated=0` is not a measurement.
3. `workers=1` vs `workers=2` at **W2 @ L2**, **≥3 interleaved replicates each**; report mean±stddev;
   win must clear max(5%, 2σ) AND keep delivery ≥0.99. Write `research/odr/ODR-0001-workers-2.md`
   (or mark neutral honestly).
4. **L3:** re-label as `bottleneck=logging_api_quota`, `verdict=regression` (delivery ≈0.60 for both
   stock and tuned). Find the real FB ceiling by sweeping load **down** until delivery ≥0.99; record it.
   Do not cite L3 as an optimization win.

**Acceptance gate C:** non-sanitizer, ≥3-replicate, delivery-accurate numbers; workers=2 verdict with
ODR (or neutral); L3 reclassified.

---

## PHASE D — Package for upstream (fluent/fluent-bit)

Three independent, reviewable units (do NOT bundle):
1. **PR-A — bug fixes (ready now):** `atoll` non-terminated-string over-read fix
   (`try_assign_subfield_int`), NULL-safe `try_assign_subfield_str`, `pack_sds_safe`, the
   `*_extracted == FLB_TRUE` guards, and the dynamic `flb_mp_array_header` insertId refactor — with the
   runtime tests covering them. Verify invalid-insertId reject semantics match the original.
2. **PR-B — concurrency fix (after Phase A):** the `stackdriver_format_ctx` thread-local refactor
   **including the Phase-A struct cleanup**, as one coherent commit on top of upstream master (not
   stacked on the old broken-mutex commit `850f8b4cd`). Include the `resource_k8s_container_concurrency`
   test and a note that CI should run it under TSan. Do **not** include the unbounded-buffer change or
   the `Dockerfile`/`__asan_default_options` build tweaks.
3. **PR-C — attribution hardening (only if Phase B approved):** the defense-in-depth change, **explicitly
   referencing GoogleCloudPlatform/k8s-stackdriver#1186** as the precedent and aligning the
   fallback-to-cluster-resource behavior.
Each PR: DCO `Signed-off-by`, one component prefix per commit (`stackdriver:` / per the upstream
commit linter `.github/scripts/commit_prefix_check.py`), subject ≤80 chars, tests green.
Separately bound the HTTP response buffer (4 MB, not 0) and revert the `Dockerfile nproc` change in
whichever local branch ships to the cluster — these are not upstream PR material.

**Acceptance gate D:** PR-A opened (or staged) with tests; PR-B staged as one clean commit; PR-C staged
iff approved; buffer bounded.

---

## PHASE E — Final report & close the loop

Update `research/findings/REPORT.md` (PROGRAM §9) with: the concurrency root-cause + fix (TSan
before/after), the #1186 reconciliation and the log-path attribution finding (B1 result), the honest
perf numbers + L3 quota reclassification, the upstream PR set, and the assumptions registry final
state. Update `STATE.json`; write `DAILY_REPORT_<n>.md` listing human-gate items (attribution policy,
any image deploy). Leave the cluster at the stock baseline.

---

## Priority if time is short
1. **Phase A** (finish the fix cleanly — it's 80% done and currently has a false commit message).
2. **Phase B1–B2** (the #1186 reachability probe — this is the new, high-value finding; even just
   "reachable: yes/no + parser.lua behavior" is a real deliverable).
3. **Phase C** (honest numbers — the current ones are invalid).
4. Phases D/E (packaging) once the above are solid.

Phase A is mechanical and unblocks PR-B. Phase B is the genuinely new work #1186 implies. Everything
else is rigor and packaging.
