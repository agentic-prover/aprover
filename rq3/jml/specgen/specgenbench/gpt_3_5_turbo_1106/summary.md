# SpecGen Full-Mutation Reproduction Run

- Started: `2026-06-22T12:08:27`
- Updated: `2026-06-22T15:57:12`
- Completed cases: `120/120`
- Trial passes: `834/1200`
- Mean success probability: `0.6950`
- Model: `gpt-3.5-turbo-1106`
- Mutation phase: `enabled` (original `specgen.py` default)

## Status Counts

| Status | Count |
|---|---:|
| fail | 10 |
| pass | 109 |
| timeout | 1 |

## By Suite

- `SpecGenBench/common`: fail=10, pass=109, timeout=1

## Recent Results

- `fail` `benchmark/SpecGenBench/common/SelectionSort/SelectionSort.java` trials=0/10 runtime=5897.42s
- `fail` `benchmark/SpecGenBench/common/SelectionSortDesc/SelectionSortDesc.java` trials=0/10 runtime=7408.419s
- `pass` `benchmark/SpecGenBench/common/SetZero/SetZero.java` trials=5/10 runtime=592.302s
- `pass` `benchmark/SpecGenBench/common/Smallest/Smallest.java` trials=8/10 runtime=599.822s
- `pass` `benchmark/SpecGenBench/common/SmallestEvenMul/SmallestEvenMul.java` trials=10/10 runtime=420.587s
- `pass` `benchmark/SpecGenBench/common/SmallestEvenMulBranch/SmallestEvenMulBranch.java` trials=10/10 runtime=243.285s
- `pass` `benchmark/SpecGenBench/common/SolveQuadraticEquation/SolveQuadraticEquation.java` trials=1/10 runtime=7581.792s
- `pass` `benchmark/SpecGenBench/common/SortLibrary/SortLibrary.java` trials=9/10 runtime=1879.348s
- `fail` `benchmark/SpecGenBench/common/StrPalindrome/StrPalindrome.java` trials=0/10 runtime=9671.655s
- `pass` `benchmark/SpecGenBench/common/SubLoop/SubLoop.java` trials=10/10 runtime=734.279s
- `pass` `benchmark/SpecGenBench/common/Swap/Swap.java` trials=10/10 runtime=117.306s
- `pass` `benchmark/SpecGenBench/common/Temperatures/Temperatures.java` trials=10/10 runtime=121.208s
- `pass` `benchmark/SpecGenBench/common/ThreeConsecutiveOdds/ThreeConsecutiveOdds.java` trials=8/10 runtime=5151.146s
- `fail` `benchmark/SpecGenBench/common/TransposeMatrix/TransposeMatrix.java` trials=0/10 runtime=2670.877s
- `pass` `benchmark/SpecGenBench/common/TwoSum/TwoSum.java` trials=5/10 runtime=8955.824s
- `pass` `benchmark/SpecGenBench/common/UglyNum/UglyNum.java` trials=1/10 runtime=5785.216s
- `pass` `benchmark/SpecGenBench/common/UglyNumLoop/UglyNumLoop.java` trials=8/10 runtime=3089.984s
- `pass` `benchmark/SpecGenBench/common/UniqueCharNested/UniqueCharNested.java` trials=6/10 runtime=9605.57s
- `pass` `benchmark/SpecGenBench/common/UniqueNumNested/UniqueNumNested.java` trials=1/10 runtime=12603.392s
- `pass` `benchmark/SpecGenBench/common/XOR/XOR.java` trials=10/10 runtime=97.954s
