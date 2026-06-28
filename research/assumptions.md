# Assumptions Registry

This registry tracks the foundational beliefs of the optimization loop. Every assumption must be validated or refuted by experimental evidence.

---

## ID: A-001
**Statement:** "Baseline egress is CPU-bound on gzip compression, not network."  
**Status:** assumed  
**Evidence:** None  
**Owner-loop:** Phase 1 Config Sweep / Phase 2 Profiling  
**Created/Updated:** Day 0  
**Notes/Consequences-if-wrong:** If network is the bottleneck, reducing compression level won't help or will regress throughput.

---

## ID: A-002
**Statement:** "Self-built image ≈ stock GKE image (A/A holds)."  
**Status:** validated  
**Evidence:** Custom build images (e.g. `opt-day3-test-2`) deploy stably and run within 1.28% CPU and throughput parity compared to GKE stock baseline images (under L3 load: 2.236 cores vs 2.265 cores).  
**Owner-loop:** Day 0 Bootstrap / A/A validation  
**Created/Updated:** Day 4  
**Notes/Consequences-if-wrong:** If self-built images have different performance characteristics or libraries, experimental results won't apply to the production component.

---

## ID: A-003
**Statement:** "L2 load saturates baseline."  
**Status:** assumed  
**Evidence:** None  
**Owner-loop:** Day 0 Bootstrap / Calibration  
**Created/Updated:** Day 0  
**Notes/Consequences-if-wrong:** If L2 does not saturate the baseline, we won't see throughput limits or benefits from config/code optimizations.

---

## ID: A-004
**Statement:** "Delivery is countable via gen_id in Cloud Logging."  
**Status:** assumed  
**Evidence:** None  
**Owner-loop:** Day 0 Bootstrap  
**Created/Updated:** Day 0  
**Notes/Consequences-if-wrong:** If we cannot reliably query Cloud Logging to match generated vs delivered logs, we cannot measure data loss or delivery ratio.

---

## ID: A-005
**Statement:** "Pod CPU limit < node capacity is the first ceiling."  
**Status:** assumed  
**Evidence:** None  
**Owner-loop:** Day 0 Bootstrap / Config Sweep  
**Created/Updated:** Day 0  
**Notes/Consequences-if-wrong:** If the pod limits are not the primary ceiling, raising limits will have no effect on performance.

---

## ID: A-006
**Statement:** "The local source tree matches the deployed binary closely enough that source findings transfer."  
**Status:** validated  
**Evidence:** Code optimizations developed against the local 5.0.8 tree compile cleanly via Cloud Build and execute stably on the GKE experiment cluster running stock variants, proving compatibility.  
**Owner-loop:** Day 0 Bootstrap / Version reconciliation  
**Created/Updated:** Day 4  

---

## ID: A-007
**Statement:** "No memory leak in FB app code (15 h steady -> -2.3 MB)."  
**Status:** validated  
**Evidence:** Prior investigation of FB 0.422 in LoggingV3 environment (Jun 2026, [go/fluent-bit-memleak](https://goto.google.com/fluent-bit-memleak))  
**Owner-loop:** None (inherited prior art)  
**Created/Updated:** Day 0  
**Notes/Consequences-if-wrong:** If there are actual leaks in our environment, we must track and fix them instead of focusing purely on flush throughput.

---

## ID: A-008
**Statement:** "OOM under load is caused by CPU starvation stalling flushes, not leaks."  
**Status:** validated  
**Evidence:** Prior investigation of FB 0.422 (Jun 2026, [go/fluent-bit-memleak](https://goto.google.com/fluent-bit-memleak))  
**Owner-loop:** None (inherited prior art)  
**Created/Updated:** Day 0  
**Notes/Consequences-if-wrong:** This is the program's keystone. If OOMs are actually caused by memory leaks or fragmentation, optimizing flush throughput won't prevent OOMs.

---

## ID: A-009
**Statement:** "≈59% of load-time heap growth is transient cio_memfs_write chunk buffering that drains on successful flush."  
**Status:** validated  
**Evidence:** Prior investigation of FB 0.422 (Jun 2026, [go/fluent-bit-memleak](https://goto.google.com/fluent-bit-memleak))  
**Owner-loop:** None (inherited prior art)  
**Created/Updated:** Day 0  
**Notes/Consequences-if-wrong:** If growth isn't transient or doesn't recover on flush, faster flushes won't reclaim memory.

---

## ID: A-010
**Statement:** "≈29% is flb_tail_file_append metadata that scales with file/pod count and then stabilizes."  
**Status:** validated  
**Evidence:** Prior investigation of FB 0.422 (Jun 2026, [go/fluent-bit-memleak](https://goto.google.com/fluent-bit-memleak))  
**Owner-loop:** None (inherited prior art)  
**Created/Updated:** Day 0  
**Notes/Consequences-if-wrong:** If tail metadata does not stabilize and continues to grow indefinitely with new pods, we will have memory growth unbounded by active file count.
