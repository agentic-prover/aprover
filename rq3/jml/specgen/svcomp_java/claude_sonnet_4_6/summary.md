# SpecGen Full-Mutation Reproduction Run

- Started: `2026-06-22T23:02:04`
- Updated: `2026-06-22T23:57:31`
- Completed cases: `265/265`
- Trial passes: `2068/2650`
- Mean success probability: `0.7804`
- Model: `anthropic/claude-sonnet-4.6`
- Mutation phase: `enabled` (original `specgen.py` default)

## Status Counts

| Status | Count |
|---|---:|
| fail | 19 |
| pass | 245 |
| timeout | 1 |

## By Suite

- `SVCOMP`: fail=19, pass=245, timeout=1

## Recent Results

- `pass` `benchmark/SVCOMP/long1/long1.java` trials=6/10 runtime=2462.595s
- `pass` `benchmark/SVCOMP/lookupswitch1/lookupswitch1.java` trials=10/10 runtime=136.153s
- `fail` `benchmark/SVCOMP/multinewarray/multinewarray.java` trials=0/10 runtime=2931.594s
- `pass` `benchmark/SVCOMP/overloading1/overloading1.java` trials=10/10 runtime=574.236s
- `pass` `benchmark/SVCOMP/package1/package1.java` trials=10/10 runtime=159.356s
- `pass` `benchmark/SVCOMP/putfield_getfield1/putfield_getfield1.java` trials=10/10 runtime=188.607s
- `pass` `benchmark/SVCOMP/putstatic_getstatic1/putstatic_getstatic1.java` trials=10/10 runtime=278.296s
- `pass` `benchmark/SVCOMP/radians/radians.java` trials=10/10 runtime=478.719s
- `pass` `benchmark/SVCOMP/recursion2/recursion2.java` trials=10/10 runtime=325.822s
- `pass` `benchmark/SVCOMP/return1/return1.java` trials=10/10 runtime=194.469s
- `pass` `benchmark/SVCOMP/return2/return2.java` trials=10/10 runtime=119.937s
- `pass` `benchmark/SVCOMP/shifting/shifting.java` trials=10/10 runtime=1449.277s
- `pass` `benchmark/SVCOMP/shifting2/shifting2.java` trials=10/10 runtime=689.32s
- `pass` `benchmark/SVCOMP/shifting3/shifting3.java` trials=9/10 runtime=790.39s
- `pass` `benchmark/SVCOMP/startswith/startswith.java` trials=10/10 runtime=137.887s
- `pass` `benchmark/SVCOMP/store_load1/store_load1.java` trials=10/10 runtime=182.214s
- `pass` `benchmark/SVCOMP/swap1/swap1.java` trials=10/10 runtime=1373.706s
- `pass` `benchmark/SVCOMP/tableswitch1/tableswitch1.java` trials=10/10 runtime=183.245s
- `pass` `benchmark/SVCOMP/uninitialised1/uninitialised1.java` trials=10/10 runtime=396.147s
- `fail` `benchmark/SVCOMP/virtual_function_unwinding/virtual_function_unwinding.java` trials=0/10 runtime=820.868s
