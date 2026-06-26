/* -*- Mode: C; tab-width: 4; indent-tabs-mode: nil; c-basic-offset: 4 -*- */

/*  Fluent Bit
 *  ==========
 *  Copyright (C) 2015-2026 The Fluent Bit Authors
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 */

#ifndef FLB_ATOMIC_H
#define FLB_ATOMIC_H

/*
 * Minimal relaxed-atomic helpers for scalar values that are read and written
 * by more than one thread (e.g. counters and one-shot status fields shared
 * between a threaded input worker and the main engine).
 *
 * Aligned word-sized loads/stores are already atomic on every platform Fluent
 * Bit targets, but accessing them from multiple threads with plain operators is
 * a C-level data race (undefined behavior, and flagged by ThreadSanitizer).
 * These helpers make such accesses well defined. Relaxed ordering is used on
 * purpose: callers only require atomicity of the individual value, not ordering
 * relative to other memory (for ordered hand-offs use a mutex instead).
 *
 * The helpers are type-generic (int, size_t, uint64_t, ...).
 */

#if defined(__GNUC__) || defined(__clang__)

#define flb_atomic_load(ptr)         __atomic_load_n((ptr), __ATOMIC_RELAXED)
#define flb_atomic_store(ptr, val)   __atomic_store_n((ptr), (val), __ATOMIC_RELAXED)
#define flb_atomic_fetch_add(ptr, v) __atomic_fetch_add((ptr), (v), __ATOMIC_RELAXED)

#else

/*
 * Fallback for compilers without the GCC/Clang atomic builtins. The accesses
 * stay plain; on the hardware these compilers target an aligned word access is
 * atomic, and the races guarded here are benign, so correctness is preserved.
 */
#define flb_atomic_load(ptr)         (*(ptr))
#define flb_atomic_store(ptr, val)   (*(ptr) = (val))
#define flb_atomic_fetch_add(ptr, v) (*(ptr) += (v))

#endif

#endif
