import argparse
import copy
import itertools
import pickle as pkl
import time
from typing import Callable, Iterable, List, Tuple
import torch
import torch.utils.benchmark as TBenchmark
from torch.utils.benchmark import Measurement as TMeasurement
from utils import make_rand_sparse_tensors
from weight_shapes import WEIGHT_SHAPES
from vllm import _custom_ops as ops
from vllm.utils import FlexibleArgumentParser
DEFAULT_MODELS = list(WEIGHT_SHAPES.keys())
DEFAULT_BATCH_SIZES = [1, 16, 32, 64, 128, 256, 512]
DEFAULT_TP_SIZES = [1]
# bench
def bench_fn(label: str, sub_label: str, description: str, fn: Callable, *args,
             **kwargs) -> TMeasurement:
    min_run_time = 1
    globals = {
        "args": args,
        "kwargs": kwargs,
        "fn": fn,
    }
    return TBenchmark.Timer(
        stmt="fn(*args, **kwargs)",
        globals=globals,
        label=label,
        sub_label=sub_label,
        description=description,
    ).blocked_autorange(min_run_time=min_run_time)
def bench_int8(dtype: torch.dtype, m: int, k: int, n: int, label: str,
               sub_label: str) -> Iterable[TMeasurement]:
    assert dtype == torch.int8
    b_compressed, e, a, b = make_rand_sparse_tensors(torch.int8, m, n, k)
    scale_a = torch.tensor(1.0, device="cuda", dtype=torch.float32)
    scale_b = torch.tensor(1.0, device="cuda", dtype=torch.float32)
    bias = torch.zeros((n, ), device="cuda", dtype=torch.bfloat16)
    out = ops.cutlass_scaled_sparse_mm(a, b_compressed, e, scale_a, scale_b,
                                       torch.bfloat16)
    out_ref = ops.cutlass_scaled_mm(a, b, scale_a, scale_b, torch.bfloat16)
    if not torch.allclose(out, out_ref):
        print("Incorrect results")
        print(out)
        print(out_ref)
    else:
        print("Correct results")
    timers = []
    # pytorch impl - bfloat16
    timers.append(
        bench_fn(label, sub_label, "pytorch_bf16_bf16_bf16_matmul-no-scales",
                 torch.mm, a.to(dtype=torch.bfloat16),
                 b.to(dtype=torch.bfloat16)))
    # pytorch impl - float16
    timers.append(
        bench_fn(label, sub_label,
                 "pytorch_fp16_fp16_fp16_matmul-no-scales", torch.mm,
                 a.to(dtype=torch.float16), b.to(dtype=torch.float16)))
    # cutlass impl
    timers.append(
        bench_fn(label, sub_label, "cutlass_i8_i8_bf16_scaled_mm",
                 ops.cutlass_scaled_mm, a, b, scale_a, scale_b,
                 torch.bfloat16))
    # cutlass with bias
    timers.append(
        bench_fn(label, sub_label, "cutlass_i8_i8_bf16_scaled_mm_bias",
                 ops.cutlass_scaled_mm, a, b, scale_a, scale_b, torch.bfloat16,
                 bias))
    # cutlass sparse impl
    timers.append(
        bench_fn(label, sub_label, "cutlass_i8_i8_bf16_scaled_sparse_mm",
                 ops.cutlass_scaled_sparse_mm, a, b_compressed, e, scale_a,
                 scale_b, torch.bfloat16))
    # cutlass sparse with bias
    timers.append(
        bench_fn(label, sub_label, "cutlass_i8_i8_bf16_scaled_sparse_mm_bias",
                 ops.cutlass_scaled_sparse_mm, a, b_compressed, e, scale_a,
                 scale_b, torch.bfloat16, bias))
    return timers
def bench_fp8(dtype: torch.dtype, m: int, k: int, n: int, label: str,
              sub_label: str) -> Iterable[TMeasurement]:
    assert dtype == torch.float8_e4m3fn
    b_compressed, e, a, b = make_rand_sparse_tensors(torch.float8_e4m3fn, m, n,
                                                     k)
    scale_a = torch.tensor(1.0, device="cuda", dtype=torch.float32)
    scale_b = torch.tensor(1.0, device="cuda", dtype=torch.float32)
    bias = torch.zeros((n, ), device="cuda", dtype=torch.bfloat16)
    out = ops.cutlass_scaled_sparse_mm(a, b_compressed, e, scale_a, scale_b,
                                       torch.bfloat16)
    out_ref = ops.cutlass_scaled_mm(a, b, scale_a, scale_b, torch.bfloat16)
    if not torch.allclose(out, out_ref):
        print("Incorrect results")
        print(out)
        print(out_ref)
    else:
        print("Correct results")
    timers = []
    # pytorch impl w. bf16
    timers.append(
        bench_fn(label, sub_label, "pytorch_bf16_bf16_bf16_matmul-no-scales",
                 torch.mm, a.to(dtype=torch.bfloat16, device="cuda"),
                 b.to(dtype=torch.bfloat16, device="cuda")))
    # pytorch impl: bf16 output, without fp8 fast accum
    timers.append(
        bench_fn(label,
                 sub_label,
                 "pytorch_fp8_fp8_bf16_scaled_mm",
                 torch._scaled_mm,
                 a,
                 b,
                 scale_a=scale_a,
                 scale_b=scale_b,
                 out_dtype=torch.bfloat16))
    # pytorch impl: bf16 output, with fp8 fast accum
    timers.append(
        bench_fn(label,
                 sub_label,
                 "pytorch_fp8_fp8_bf16_scaled_mm_fast_accum",
                 torch._scaled_mm,
                 a,
                 b,
                 scale_a=scale_a,
                 scale_b=scale_b,
                 out_dtype=torch.bfloat16,
                 use_fast_accum=True))
    # pytorch impl: fp16 output, without fp8 fast accum
    timers.append(
        bench_fn(label,
                 sub_label,
                 "pytorch_fp8_fp8_fp16_scaled_mm",
                 torch._scaled_mm,
                 a,
                 b,
                 scale_a=scale_a,
                 scale_b=scale_b,
                 out_dtype=torch.float16))
    # pytorch impl: fp16 output, with fp8 fast accum
    timers.append(
        bench_fn(label,
                 sub_label,
                 "pytorch_fp8_fp8_fp16_scaled_mm_fast_accum",
                 torch._scaled_mm,
                 a,
                 b,
                 scale_a=scale_a,
                 scale_b=scale_b,
                 out_dtype=torch.float16,
                 use_fast_accum=True))
    # cutlass impl: bf16 output
    timers.append(
        bench_fn(label, sub_label, "cutlass_fp8_fp8_bf16_scaled_mm",
                 ops.cutlass_scaled_mm, a, b, scale_a, scale_b,
                 torch.bfloat16))
    # cutlass impl: bf16 output
    timers.append(
        bench_fn(label, sub_label, "cutlass_fp8_fp8_bf16_scaled_sparse_mm",
                 ops.cutlass_scaled_sparse_mm, a, b_compressed, e, scale_a,
                 scale_b, torch.bfloat16))
    # cutlass impl: fp16 output
    timers.append(
        bench_fn(label, sub_label, "cutlass_fp8_fp8_fp16_scaled_sparse_mm",
                 ops.cutlass_scaled_sparse_mm, a, b_compressed, e, scale_a,
                 scale_b, torch.float16))
    # cutlass impl: bf16 output, with bias
    timers.append(
        bench_fn(label, sub_label,
                 "cutlass_fp8_fp8_bf16_scaled_sparse_mm_bias",
                 ops.cutlass_scaled_sparse_mm, a, b_compressed, e, scale_a,
                 scale_b, torch.bfloat16, bias))
    # cutlass impl: fp16 output, with bias
    timers.append(
        bench_fn(label, sub_label,
                 "cutlass_fp8_fp8_fp16_scaled_sparse_mm_bias",
                 ops.cutlass_scaled_sparse_mm, a, b_compressed, e, scale_a,
                 scale_b, torch.float16, bias.to(dtype=torch.float16)))
    return timers
def bench(dtype: torch.dtype, m: int, k: int, n: int, label: str,
          sub_label: str) -> Iterable[TMeasurement]:
    if dtype == torch.int8:
        return bench_int8(dtype, m, k, n, label, sub_label)
    if dtype == torch.float8_e4m3fn:
        return bench_fp8(dtype, m, k, n, label, sub_label)
    raise ValueError("unsupported type")
# runner
def print_timers(timers: Iterable[TMeasurement]):
    compare = TBenchmark.Compare(timers)
    compare.print()
def run(dtype: torch.dtype,
        MKNs: Iterable[Tuple[int, int, int]]) -> Iterable[TMeasurement]:
    results = []
    for m, k, n in MKNs:
        timers = bench(dtype, m, k, n, f"scaled-{dtype}-gemm",
                       f"MKN=({m}x{k}x{n})")
        print_timers(timers)
        results.extend(timers)
    return results
# output makers
def make_output(data: Iterable[TMeasurement],
                MKNs: Iterable[Tuple[int, int, int]],
                base_description: str,
                timestamp=None):
    print(f"== All Results {base_description} ====")
    print_timers(data)
    # pickle all the results
    timestamp = int(time.time()) if timestamp is None else timestamp
    with open(f"{base_description}-{timestamp}.pkl", "wb") as f:
        pkl.dump(data, f)
# argparse runners
def run_square_bench(args):
    dim_sizes = list(
        range(args.dim_start, args.dim_end + 1, args.dim_increment))
    MKNs = list(zip(dim_sizes, dim_sizes, dim_sizes))
    data = run(args.dtype, MKNs)
    make_output(data, MKNs, f"square_bench-{args.dtype}")
def run_range_bench(args):
    dim_sizes = list(range(args.dim_start, args.dim_end, args.dim_increment))
    n = len(dim_sizes)
    Ms = [args.m_constant] * n if args.m_constant is not None else dim_sizes
    Ks = [args.k_constant] * n if args.k_constant is not None else dim_sizes
    Ns = [args.n_constant] * n if args.n_constant is not None else dim_sizes
    MKNs = list(zip(Ms, Ks, Ns))
    data = run(args.dtype, MKNs)
    make_output(data, MKNs, f"range_bench-{args.dtype}")
def run_model_bench(args):
    print("Benchmarking models:")
    for i, model in enumerate(args.models):
        print(f"[{i}]  {model}")
    def model_shapes(model_name: str, tp_size: int) -> List[Tuple[int, int]]:
        KNs = []
        for KN, tp_split_dim in copy.deepcopy(WEIGHT_SHAPES[model_name]):
            KN[tp_split_dim] = KN[tp_split_dim] // tp_size
            KNs.append(KN)
        return KNs
    model_bench_data = []
    models_tps = list(itertools.product(args.models, args.tp_sizes))
    for model, tp_size in models_tps:
        Ms = args.batch_sizes
        KNs = model_shapes(model, tp_size)
        MKNs = []
        for m in Ms:
            for k, n in KNs:
                MKNs.append((m, k, n))
        data = run(args.dtype, MKNs)
        model_bench_data.append(data)
    # Print all results
    for data, model_tp in zip(model_bench_data, models_tps):
        model, tp_size = model_tp
        print(f"== Results {args.dtype} {model}-TP{tp_size} ====")
        print_timers(data)
    timestamp = int(time.time())
    all_data = []
    for d in model_bench_data:
        all_data.extend(d)
    # pickle all data
    with open(f"model_bench-{args.dtype}-{timestamp}.pkl", "wb") as f:
        pkl.dump(all_data, f)
matrix_sizes = [
    (16384, 53248, 16384)
    # (256, 256, 256),
    # (512, 512, 512),
    # (1024, 1024, 1024),
    # (2048, 2048, 2048),
    # (4096, 4096, 4096)
]
for m, k, n in matrix_sizes:
    print(f"\nBenchmarking matrix size: M={m}, K={k}, N={n}")
    timers = bench_fp8(dtype=torch.float8_e4m3fn, m=m, k=k, n=n, label="fp8_bench", sub_label=f"MKN=({m}x{k}x{n})")
    for timer in timers:
        print(f"{timer.description}: {timer.median:.6f} seconds (median over {timer.times} runs)")