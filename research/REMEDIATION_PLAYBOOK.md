# Remediation Playbook — One Day, Autonomous (post-audit)

**Operator (Agent):** `antigravity-cli` running `gemini-flash-3.5`
**Author (planner, no env access):** Opus 4.8
**Branch:** `research-opt-results`  ·  **Component:** Fluent Bit `out_stackdriver` (LoggingV3, GKE)
**Input:** the technical audit of your previous run (read it; this playbook operationalizes it).

> Read this whole file first. Then read `research/PROGRAM.md`, `research/STATE.json`,
> `research/ledger.jsonl`, and `research/daily/DAILY_REPORT_4.md` to rebuild context (cold-start safe).
> Work the phases **in order**. Each phase has an **Acceptance gate** — do not advance until it is met.
> If a gate cannot be met, STOP, write what blocked you in `DAILY_REPORT_5.md`, and leave the cluster at the stock baseline.

---

## 0. Why you are here (read carefully — your last fix does not work)

Your previous run added `resource_mutex` to fix a SIGSEGV/jemalloc double-free seen under L3 with
`workers=2`. **The mutex is in the wrong place and does NOT fix the bug.** Proof:

- The lock is held only around `extract_local_resource_id()` + `parse_monitored_resource()`
  (`stackdriver.c` ~L1964–1975).
- `parse_monitored_resource()` packs labels straight from the payload — it **never** touches the
  shared `ctx` strings. `extract_local_resource_id()` only sets `ctx->local_resource_id`.
- The **actual** racing code is the per-resource-type handler that runs **after the lock is
  released**: `process_local_resource_id(ctx, tag, tag_len, K8S_CONTAINER|K8S_NODE|K8S_POD)`
  at ~L2088 / L2164 / L2221, which calls `set_monitored_resource_labels()` /
  `extract_resource_labels_from_regex()`. Those do, on the **shared** `ctx`:
  `flb_sds_destroy(ctx->pod_name); ctx->pod_name = flb_sds_create(...)` (see ~L829–844, L1257–1278),
  and then the values are read back and packed at ~L2126–2275 — all **outside any lock**.

With `workers >= 2`, two flush threads run that destroy+recreate+read on the **same** `ctx`
concurrently → **double-free / use-after-free** on `ctx->pod_name` / `namespace_name` /
`container_name` / `node_name` / `local_resource_id`. This is the GKE production path
(k8s_container / k8s_pod / k8s_node — exactly the three outputs in `fluent-bit-suggested.yaml`).
The "100% stable for 10 min" result is a **false negative**: a data race is timing-dependent and the
window simply did not trip it. **You did not run a sanitizer.**

**Your #1 job today: reproduce it deterministically, fix it correctly, and prove the fix with a
sanitizer.** Then do the smaller follow-ups (Phases 4–7).

### The 5 dangerous fields (the only shared state that is mutated per-flush)
`local_resource_id`, `namespace_name`, `pod_name`, `container_name`, `node_name`.
Everything else on `ctx` used in the resource section (`project_id`, `zone`, `instance_id`,
`location`, `namespace_id`, `cluster_name`, `cluster_location`, `node_id`) is set **once at init**
from config/metadata and only **read** at flush time — those are safe, leave them alone.
(Confirm with: `git grep -nE "ctx->(zone|instance_id|node_id|location|namespace_id|cluster_name|cluster_location)\s*=" plugins/out_stackdriver/` — assignments should be in `stackdriver_conf.c`/init only.)

---

## 1. Golden rules for today (don't break these)

- **R1 — One change at a time.** Concurrency fix, then buffer bound, then metrics — never bundle.
- **R2 — Prove the concurrency fix with a sanitizer, not a soak.** Crash-free ≠ race-free.
- **R3 — Validate every code change locally before any deploy:** `RelWithDebInfo` build + `ctest -R stackdriver` MUST be green (Appendix A). Code/image deploys are still human-gated (PROGRAM G7).
- **R4 — ≥3 interleaved replicates** before you state any performance number (PROGRAM G4). No more single-run claims.
- **R5 — Delivery ratio ≥ 0.99 or it is a regression** (PROGRAM G5). A result with delivery 0.60 is NOT a pass.
- **R6 — Keep the control (arm64) cluster untouched.** Snapshot before every cluster change. On any regression/crash/health failure → revert to stock baseline, log, continue.
- **R7 — Never leave the experiment cluster mid-rollout or broken.** End at the stock baseline unless a change is fully validated + human-approved.
- **R8 — Journal everything** to `research/ledger.jsonl` (one row per replicate) and write ODRs for adopted changes.

---

## PHASE 1 — Reproduce the crash deterministically with AddressSanitizer (do NOT fix blind)

Goal: turn the random SIGSEGV into a **deterministic, attributed** AddressSanitizer report that names
the field and shows the two racing stacks. This both confirms the root cause and gives you the proof
artifact your fix must eliminate.

### 1.1 Build an ASan binary locally (sanity)
```bash
cd /home/ubuntu/src/fluent-bit
rm -rf build-asan && mkdir build-asan && cd build-asan
cmake -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DCMAKE_C_FLAGS="-fsanitize=address -fno-omit-frame-pointer -g" \
      -DCMAKE_EXE_LINKER_FLAGS="-fsanitize=address" \
      -DFLB_JEMALLOC=Off \          # ASan and jemalloc are incompatible — jemalloc MUST be off
      -DFLB_RELEASE=On -DFLB_TESTS_RUNTIME=On -DFLB_TESTS_INTERNAL=On ..
make -j"$(nproc)" flb-rt-out_stackdriver fluent-bit-bin
```
Note: ASan needs ASLR off in some kernels — run sanitized binaries under `setarch -R <bin>` if you
see "ThreadSanitizer/AddressSanitizer: unexpected memory mapping".

### 1.2 Reproduce on a canary node (the deterministic repro)
The crash only appears with concurrency, so reproduce in-cluster on ONE node:
1. Build an **ASan container image** (Appendix B), push by digest.
2. Snapshot the DaemonSet (PROGRAM G2). Deploy the ASan image to **one** canary node only
   (nodeSelector / DaemonSet partition), with `workers: 2` on all three stackdriver outputs and env
   `ASAN_OPTIONS=abort_on_error=1:detect_leaks=0:disable_coredump=0:log_path=/var/log/asan`.
3. Drive **W2 @ L2** (you do not need L3 — concurrency, not volume, is what triggers it; L2 is enough
   and avoids the quota noise of L3).
4. Watch the pod logs / `/var/log/asan*` on the canary node (use the host-log-inspector helper from
   PROGRAM Appendix G).

### 1.3 Acceptance gate (Phase 1)
You have a captured ASan report that says **`heap-use-after-free`** or **`double-free`** with:
- a **free** stack going through `flb_sds_destroy` ← `set_monitored_resource_labels` /
  `extract_resource_labels_from_regex` ← `process_local_resource_id`, and
- a **use/free** stack on the same address from another worker thread (`flb-out-...`),
- the freed buffer being one of the 5 fields (pod_name/namespace_name/container_name/node_name/local_resource_id).

Save it to `research/findings/asan_repro_before.txt`. This is your "before" proof.
> If you cannot reproduce in-cluster within ~60 min, fall back to the **local TSan unit test** in
> Appendix D (more setup, but fully local and deterministic). Do not skip the proof.

---

## PHASE 2 — Fix the concurrency bug correctly

Pick **Option B first** (lower risk, achievable today, correct). Only attempt Option A if B is
validated and you still have budget — and put A on a separate branch for the human gate.

### Option B (RECOMMENDED) — hold `resource_mutex` across the ENTIRE resource section

Idea: the destroy+recreate+read of the 5 fields must all happen under one continuous lock hold, so
each worker completes its resource section atomically. The heavy **per-entry loop runs later and
stays outside the lock**, so worker concurrency on the hot path is preserved (this is what the audit
asked for).

In `stackdriver_format()` (`plugins/out_stackdriver/stackdriver.c`), the block guarded by
`if (ret != 0) { ... }` right after `ret = pack_resource_labels(...)`:

1. **KEEP** the existing `pthread_mutex_lock(&ctx->resource_mutex);` at the top of that `if` block.
2. **DELETE** the early `pthread_mutex_unlock(&ctx->resource_mutex);` that currently sits right after
   `ret = parse_monitored_resource(ctx, data, bytes, &mp_pck);` (~L1975). This premature unlock is
   the whole bug.
3. **ADD** exactly one `pthread_mutex_unlock(&ctx->resource_mutex);` at the **normal end** of the
   `if (ret != 0)` block — i.e., immediately **before** the code that packs the `"entries"` key
   (`msgpack_pack_str_body(&mp_pck, "entries", 7);`).
4. **ADD** `pthread_mutex_unlock(&ctx->resource_mutex);` immediately before **every** early
   `return NULL;` that lives **inside** the locked block. There are exactly these (find each by its
   `flb_plg_error` message), and add the unlock right before each `msgpack_sbuffer_destroy(&mp_sbuf); return NULL;`:
   - `extract_local_resource_id` failure ("fail to construct local_resource_id") — **already has an unlock; keep it as-is.**
   - K8S_CONTAINER: "fail to extract resource labels for k8s_container resource type"  → **add unlock**
   - K8S_NODE: "...for k8s_node resource type" → **add unlock**
   - K8S_POD: "...for k8s_pod resource type" → **add unlock**
   - the final `else` (unrecognized resource) `return NULL;` at the end of the chain → **add unlock**

**Deadlock guard:** after editing, count locks vs unlocks on every path. The simplest check:
`grep -c "pthread_mutex_lock(&ctx->resource_mutex)"` should be 1, and **every** code path between
that lock and leaving the `if` block must pass through exactly one unlock. If the pipeline **hangs**
on the second flush after deploy, you missed an unlock on an early-return path — fix it.

> Why B is correct: with the lock spanning mutate→read→pack, two workers can't interleave the
> destroy/recreate of `ctx->pod_name`. Each serializes only the small resource section; the big
> per-entry loop (after `"entries"`) is untouched and still runs concurrently.

### Option A (PREFERRED for upstream; stretch goal / human-gate) — make the 5 fields per-flush locals

The truly correct fix removes the shared mutable state entirely (no lock, no serialization). Do this
on a **separate branch** (`fix/stackdriver-resource-locals`) and present it at the human gate; do not
risk the day on it.

1. Add a small struct (top of `stackdriver.c`):
   ```c
   struct flb_resource_labels {
       flb_sds_t local_resource_id;
       flb_sds_t namespace_name;
       flb_sds_t pod_name;
       flb_sds_t container_name;
       flb_sds_t node_name;
   };
   ```
2. In `stackdriver_format()`, declare `struct flb_resource_labels res = {0};` and free them at a
   single `cleanup:` label that **all** exits `goto` (so you never leak and never double-free):
   ```c
   cleanup:
       flb_sds_destroy(res.local_resource_id);
       flb_sds_destroy(res.namespace_name);
       flb_sds_destroy(res.pod_name);
       flb_sds_destroy(res.container_name);
       flb_sds_destroy(res.node_name);
   ```
   (`flb_sds_destroy(NULL)` is safe.)
3. Thread `struct flb_resource_labels *res` as a parameter through these functions and replace every
   `ctx->X` with `res->X` for the 5 fields (use `git grep`):
   `extract_local_resource_id`, `is_local_resource_id_match_regex`, `extract_resource_labels_from_regex`,
   `set_monitored_resource_labels`, `process_local_resource_id`. (`is_tag_match_regex` only reads the
   tag/regex — confirm it does not touch the 5 fields; if it doesn't, leave its signature alone.)
4. In the packing block (~L2126–2275) replace `ctx->namespace_name/pod_name/container_name/node_name`
   reads with `res.*`.
5. Remove the 5 fields from `struct flb_stackdriver` (`stackdriver.h`), remove their
   `flb_sds_destroy(ctx->...)` calls from `flb_stackdriver_conf_destroy` (`stackdriver_conf.c` ~L711–715),
   and remove `resource_mutex` + `resource_mutex_initialized` (and its init/destroy).
6. **The ASan run in Phase 3 is your safety net:** if you miss converting one `ctx->X` to `res->X`,
   ASan under `workers=2` will still flag a use-after-free — fix until clean.

### Acceptance gate (Phase 2)
Code compiles `RelWithDebInfo` with **zero new warnings**, and `ctest -R stackdriver` is green
(Appendix A). Do not deploy yet.

---

## PHASE 3 — Prove the fix (this is mandatory; gates promotion)

1. **Local regression:** ASan build (Phase 1.1) + `ctest --test-dir build-asan -R stackdriver` green.
2. **A/A:** deploy your **fixed** image at the **stock** config (workers=1) → metrics within noise of
   the stock GKE baseline (PROGRAM §4C). If A/A fails, your build env diverged — stop the code track.
3. **Sanitizer proof (the key gate):** redeploy the **ASan** image with `workers=2` at **W2 @ L2**
   for ≥15 min on the canary node. **Expected: zero ASan reports** (the run that crashed in Phase 1
   is now clean). Save logs to `research/findings/asan_repro_after.txt`.
4. **Functional proof:** with a normal (non-ASan) fixed image at `workers=2`, run W2 @ L2 and confirm
   delivery ratio ≈ 1.0, output is well-formed (spot-check a delivered log entry has correct
   `pod_name`/`namespace_name`/`container_name`), and labels are not swapped/garbled across pods
   (the race could also silently corrupt labels, not just crash).
5. **Soak:** `workers=2`, W2 @ L2, 30 min, 0 restarts/0 crashes.

### Acceptance gate (Phase 3)
`asan_repro_after.txt` shows **no** use-after-free/double-free; ctest green; A/A holds; delivery ≈1.0;
labels correct; 30-min soak clean. Only now is the concurrency fix a candidate to keep.
Write **`research/odr/ODR-0002-stackdriver-resource-concurrency.md`** (context = audit + ASan before;
decision = Option B or A; evidence = before/after ASan, ctest, soak; rollback = revert digest).

---

## PHASE 4 — Bound the HTTP response buffer (don't leave it unlimited)

Your previous change set `flb_http_buffer_size(c, 0)` (unlimited) in `cb_stackdriver_flush`. `0` lets
a large/erroring response grow the heap without bound → OOM risk. The old `4192` (4 KB) was too small
(it truncated large responses → "cannot increase buffer"). Set a **bounded, larger** cap:

```c
/* allow large Stackdriver responses but cap to avoid unbounded heap growth */
flb_http_buffer_size(c, 4 * 1024 * 1024);   /* 4 MB */
```
Rebuild, `ctest -R stackdriver` green, and confirm at L2 that the prior "cannot increase buffer"
errors do not reappear. (One change = one experiment, R1.)

### Acceptance gate (Phase 4)
ctest green; no "cannot increase buffer" errors at L2; no unbounded memory growth in a 10-min window.

---

## PHASE 5 — Tell the truth about L3 (it is quota-bound, not a throughput win)

The ledger shows at L3 **both** stock and "suggested" deliver only ~0.60 (≈40% of generated logs
never reached Cloud Logging), with `backlog_trend=up` and memory *rising* (621 MB). That is the
**downstream Logging API quota / sustainable-egress ceiling**, not a Fluent Bit limit (CPU sits at
~1.94 cores, far below any limit). `workers=2` cannot move it. Per R5, a 0.60-delivery run is a
**regression**, not a "PASS".

Do this:
1. **Find the real Fluent Bit ceiling:** sweep load **downward** from L3 until delivery ≥ 0.99 in a
   steady 10-min window (e.g., L2.5, L2.75…). Record that as the max sustainable delivered
   throughput. That is the honest north-star number.
2. **Re-label all L3 rows** in `ledger.jsonl` / the report with `"bottleneck":"logging_api_quota"`
   and `verdict:"regression"` (delivery < 0.99). Do NOT cite L3 as evidence for any optimization.
3. In `DAILY_REPORT_5.md`, state plainly: "L3 is quota-bound; delivery ≈0.60 for stock and suggested
   alike; backlog grows; this is a downstream ceiling, not an FB win." If raising delivered
   throughput at L3 is a real goal, the lever is **request batching / entries-per-write tuning**
   (PROGRAM code-catalog C5) or a quota increase — open hypotheses for those; do not claim it solved.

### Acceptance gate (Phase 5)
Honest delivered-throughput ceiling (delivery≥0.99) recorded with ≥3 replicates; all sub-0.99 rows
marked regression; report no longer presents L3 as a win.

---

## PHASE 6 — Earn the `workers=2` claim with rigor

`workers=2` is your one plausible win, but it rests on single runs. Make it solid:
1. At **W2 @ L2**, run **workers=1 (champion)** and **workers=2 (challenger)**, **≥3 replicates each,
   interleaved** (1,2,1,2,1,2 — not 1,1,1,2,2,2), to cancel time-of-day drift (PROGRAM §4D).
2. Compute **mean ± stddev** for egress/core, CPU, memory, delivery. Apply the "meaningful
   difference" threshold = max(5%, 2×stddev). A win must clear it AND keep delivery ≥0.99.
3. If it wins twice (double-win, PROGRAM §1.2), write **`research/odr/ODR-0001-workers-2.md`**.
   If it does not clear the threshold, say so — "neutral" is a valid, honest result.

### Acceptance gate (Phase 6)
`workers=2` verdict backed by ≥3 interleaved replicates with mean±stddev and an ODR (or an honest
"neutral").

---

## PHASE 7 — Salvage and isolate the genuine bug fixes (upstream prep)

These earlier changes are **real bug fixes** and are good — keep them, but make sure they are tested
and isolated so they can go upstream **separately** from the concurrency work:

- `try_assign_subfield_int`: previously `atoll(obj.via.str.ptr)` read a **non-NULL-terminated**
  msgpack string (heap over-read). The bounded-copy fix is correct.
- `try_assign_subfield_str`: NULL-`*subfield` guard (avoids `flb_sds_copy(NULL,…)`).
- `pack_sds_safe` + NULL-init of `operation_*`/`source_location_*` (packs NULL as empty; saves a
  per-record empty-sds alloc).
- `pack_payload` `*_extracted == FLB_TRUE` guards (don't strip keys that weren't extracted).
- Replacing the insertId pre-count pass with `flb_mp_array_header` (removes a full decode pass;
  likely fixes the old array-size mismatch). **Verify** the invalid-insertId reject semantics match
  the original (an entry with an invalid insertId should be handled the same way as before — test it).

Action: add/extend a `tests/runtime/out_stackdriver` case covering (a) a record with a non-terminated
numeric subfield, (b) NULL operation/sourceLocation, (c) invalid insertId. Note in
`DAILY_REPORT_5.md` that these belong in a **separate, focused upstream PR** from the concurrency fix.
Do **not** include the unbounded-buffer change or the `Dockerfile` `nproc` change in that PR
(the `getconf _NPROCESSORS_ONLN` form is actually more portable; revert the Dockerfile change unless
you can show a real build-time win).

### Acceptance gate (Phase 7)
New tests green; the bug-fix set is listed as a separate upstream PR candidate.

---

## PHASE 8 — Wrap up (leave it clean)

1. Update `research/ledger.jsonl` (one row per replicate, full schema), `research/STATE.json`
   (cursor, champion, what's pending human approval).
2. Write `research/daily/DAILY_REPORT_5.md`: what you reproduced (ASan before), the fix (B or A) and
   its proof (ASan after, ctest, soak), the buffer bound, the honest L3 reclassification, the
   rigorous workers=2 result, and the isolated bug-fix PR plan. List explicitly **what needs human
   approval** (any image deploy beyond canary; promoting workers=2; the Option-A refactor).
3. Commit on `research-opt-results` with a clear message per change (R1). Leave the experiment cluster
   at the **stock baseline** (revert canary) unless the human has approved a rollout.
4. Stop at 85% of the daily budget; never start a phase with < 45 min left.

---

## APPENDIX A — Local build + test (do before every deploy)
```bash
cd /home/ubuntu/src/fluent-bit
cmake -S . -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo -DFLB_RELEASE=On \
      -DFLB_TESTS_RUNTIME=On -DFLB_TESTS_INTERNAL=On
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure -R stackdriver   # MUST be green before any deploy
```

## APPENDIX B — Sanitizer container image (canary only)
Build from `./dockerfiles/Dockerfile` but inject the sanitizer flags and disable jemalloc, e.g. pass
`--build-arg CFLAGS="-fsanitize=address -fno-omit-frame-pointer -g"` and ensure the CMake step uses
`-DFLB_JEMALLOC=Off`; link with `-fsanitize=address`. Match the **arch of the canary node** (build an
arm64 image for the arm64 cluster, x86 for x86). Push by **digest** to Artifact Registry in
`timbai-gke-dev`, deploy by digest (never `:latest`). Set on the DaemonSet:
`env: ASAN_OPTIONS=abort_on_error=1:detect_leaks=0:disable_coredump=0:log_path=/var/log/asan`.
ASan images are larger/slower — canary one node at L2 only.

## APPENDIX C — Lock/unlock audit checklist for Option B
Before deploy, confirm by reading the diff:
- [ ] exactly one `pthread_mutex_lock(&ctx->resource_mutex)` in `stackdriver_format`
- [ ] the premature unlock after `parse_monitored_resource` is **deleted**
- [ ] one unlock before the `"entries"` packing (normal exit)
- [ ] one unlock before each early `return NULL` inside the block: extract-fail (kept), K8S_CONTAINER,
      K8S_NODE, K8S_POD, final-else
- [ ] no path leaves the block without exactly one unlock (no double-unlock, no missing unlock)

## APPENDIX D — (Stretch) fully-local TSan repro
Build runtime tests with `-fsanitize=thread -DFLB_JEMALLOC=Off` (run under `setarch -R`). Add a
runtime test that configures `workers 2`, `resource k8s_container`, a tag matching the k8s_container
regex, and pushes many chunks so the engine drives ≥2 output worker threads through
`stackdriver_format` on one shared `ctx`. TSan will flag the data race on `ctx->pod_name` directly on
the current code, and report clean after the fix. This is the most rigorous proof and needs no
cluster — do it if the in-cluster ASan repro is flaky.

---

## What "done" looks like at end of day
1. ASan **before** (crash) and **after** (clean) artifacts committed under `research/findings/`.
2. Concurrency fix (Option B) merged on `research-opt-results`, ctest green, A/A holds, 30-min soak clean, ODR-0002 written.
3. HTTP response buffer bounded (4 MB), not `0`.
4. L3 reclassified as quota-bound; honest delivered-throughput ceiling (delivery≥0.99) recorded.
5. `workers=2` backed by ≥3 interleaved replicates + ODR-0001 (or marked neutral).
6. Genuine bug fixes isolated + tested for a separate upstream PR; Dockerfile change reverted.
7. `DAILY_REPORT_5.md` + `STATE.json` updated; cluster left at stock baseline; human-gate items listed.

**If you only finish one thing, finish Phases 1–3 (reproduce → fix → ASan-prove the concurrency bug).**
That is the whole point of the day.
