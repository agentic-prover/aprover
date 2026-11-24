# SpecGen Full-Mutation Reproduction Run

- Started: `2026-06-21T19:02:37`
- Updated: `2026-06-21T21:35:38`
- Completed cases: `120/120`
- Trial passes: `1019/1200`
- Mean success probability: `0.8492`
- Model: `anthropic/claude-sonnet-4.6`
- Mutation phase: `enabled` (original `specgen.py` default)

## Status Counts

| Status | Count |
|---|---:|
| fail | 3 |
| pass | 116 |
| timeout | 1 |

## By Suite

- `SpecGenBench/common`: fail=3, pass=116, timeout=1

## Recent Results

- `pass` `benchmark/SpecGenBench/common/SelectionSort/SelectionSort.java` trials=7/10 runtime=5647.408s
- `pass` `benchmark/SpecGenBench/common/SelectionSortDesc/SelectionSortDesc.java` trials=7/10 runtime=5806.497s
- `pass` `benchmark/SpecGenBench/common/SetZero/SetZero.java` trials=10/10 runtime=153.241s
- `pass` `benchmark/SpecGenBench/common/Smallest/Smallest.java` trials=10/10 runtime=129.708s
- `pass` `benchmark/SpecGenBench/common/SmallestEvenMul/SmallestEvenMul.java` trials=10/10 runtime=280.95s
- `pass` `benchmark/SpecGenBench/common/SmallestEvenMulBranch/SmallestEvenMulBranch.java` trials=10/10 runtime=157.724s
- `pass` `benchmark/SpecGenBench/common/SolveQuadraticEquation/SolveQuadraticEquation.java` trials=1/10 runtime=2421.511s
- `pass` `benchmark/SpecGenBench/common/SortLibrary/SortLibrary.java` trials=10/10 runtime=243.533s
- `pass` `benchmark/SpecGenBench/common/StrPalindrome/StrPalindrome.java` trials=10/10 runtime=5094.281s
- `pass` `benchmark/SpecGenBench/common/SubLoop/SubLoop.java` trials=10/10 runtime=118.008s
- `pass` `benchmark/SpecGenBench/common/Swap/Swap.java` trials=10/10 runtime=247.669s
- `pass` `benchmark/SpecGenBench/common/Temperatures/Temperatures.java` trials=10/10 runtime=104.211s
- `pass` `benchmark/SpecGenBench/common/ThreeConsecutiveOdds/ThreeConsecutiveOdds.java` trials=8/10 runtime=6924.85s
- `fail` `benchmark/SpecGenBench/common/TransposeMatrix/TransposeMatrix.java` trials=0/10 runtime=3125.033s
- `pass` `benchmark/SpecGenBench/common/TwoSum/TwoSum.java` trials=9/10 runtime=1198.282s
- `pass` `benchmark/SpecGenBench/common/UglyNum/UglyNum.java` trials=6/10 runtime=3709.124s
- `pass` `benchmark/SpecGenBench/common/UglyNumLoop/UglyNumLoop.java` trials=6/10 runtime=5393.506s
- `pass` `benchmark/SpecGenBench/common/UniqueCharNested/UniqueCharNested.java` trials=4/10 runtime=10977.884s
- `pass` `benchmark/SpecGenBench/common/UniqueNumNested/UniqueNumNested.java` trials=7/10 runtime=7469.826s
- `pass` `benchmark/SpecGenBench/common/XOR/XOR.java` trials=10/10 runtime=120.612s
