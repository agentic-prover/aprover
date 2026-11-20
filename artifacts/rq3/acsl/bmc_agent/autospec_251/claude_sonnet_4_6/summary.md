# BMC-Agent AutoSpec 251 Trials

- Progress: `251/251`
- Model: `anthropic/claude-sonnet-4.6`
- Prompt examples: `4`
- Trials per case: `5`
- Workers: `32`
- Case status: `{'pass': 209, 'fail': 42}`
- Trial passes: `990/1255`
- Mean success probability: `0.7888446215139442`

## By Benchmark

| Benchmark | Cases | Passed | Failed | Trial pass ratio |
|---|---:|---:|---:|---:|
| `code2inv_133` | 133 | 121 | 12 | 587/665 |
| `fib_46` | 46 | 38 | 8 | 173/230 |
| `frama-c-problems` | 51 | 33 | 18 | 157/255 |
| `svcomp` | 21 | 17 | 4 | 73/105 |

## Cases

| Benchmark | Case | Status | Trial passes | Best WP | Runtime | Failure | Artifacts |
|---|---|---:|---:|---:|---:|---|---|
| `code2inv_133` | `1.c` | pass | 5/5 | 10/10 | 278.55s |  | json c |
| `code2inv_133` | `10.c` | pass | 5/5 | 5/5 | 150.49s |  | json c |
| `code2inv_133` | `100.c` | pass | 5/5 | 8/8 | 94.13s |  | json c |
| `code2inv_133` | `101.c` | pass | 5/5 | 6/6 | 36.93s |  | json c |
| `code2inv_133` | `102.c` | pass | 5/5 | 6/6 | 31.97s |  | json c |
| `code2inv_133` | `103.c` | pass | 5/5 | 6/6 | 37.35s |  | json c |
| `code2inv_133` | `104.c` | pass | 5/5 | 6/6 | 44.58s |  | json c |
| `code2inv_133` | `105.c` | pass | 5/5 | 6/6 | 29.72s |  | json c |
| `code2inv_133` | `106.c` | fail | 0/5 | 5/6 | 836.23s | fail | json c |
| `code2inv_133` | `107.c` | pass | 5/5 | 8/8 | 117.33s |  | json c |
| `code2inv_133` | `108.c` | pass | 5/5 | 6/6 | 159.21s |  | json c |
| `code2inv_133` | `109.c` | pass | 5/5 | 8/8 | 110.63s |  | json c |
| `code2inv_133` | `11.c` | pass | 5/5 | 9/9 | 154.08s |  | json c |
| `code2inv_133` | `110.c` | pass | 5/5 | 12/12 | 255.24s |  | json c |
| `code2inv_133` | `111.c` | pass | 5/5 | 8/8 | 142.22s |  | json c |
| `code2inv_133` | `112.c` | pass | 5/5 | 14/14 | 168.88s |  | json c |
| `code2inv_133` | `113.c` | pass | 5/5 | 8/8 | 141.51s |  | json c |
| `code2inv_133` | `114.c` | pass | 5/5 | 7/7 | 92.17s |  | json c |
| `code2inv_133` | `115.c` | pass | 5/5 | 7/7 | 84.3s |  | json c |
| `code2inv_133` | `116.c` | pass | 5/5 | 7/7 | 92.78s |  | json c |
| `code2inv_133` | `117.c` | pass | 5/5 | 7/7 | 83.95s |  | json c |
| `code2inv_133` | `118.c` | pass | 5/5 | 10/10 | 193.88s |  | json c |
| `code2inv_133` | `119.c` | pass | 5/5 | 10/10 | 142.45s |  | json c |
| `code2inv_133` | `12.c` | pass | 5/5 | 5/5 | 150.6s |  | json c |
| `code2inv_133` | `120.c` | pass | 5/5 | 6/6 | 84.66s |  | json c |
| `code2inv_133` | `121.c` | pass | 5/5 | 10/10 | 88.06s |  | json c |
| `code2inv_133` | `122.c` | pass | 5/5 | 12/12 | 282.89s |  | json c |
| `code2inv_133` | `123.c` | pass | 5/5 | 8/8 | 140.64s |  | json c |
| `code2inv_133` | `124.c` | pass | 5/5 | 6/6 | 196.86s |  | json c |
| `code2inv_133` | `125.c` | pass | 5/5 | 6/6 | 165.21s |  | json c |
| `code2inv_133` | `126.c` | pass | 5/5 | 6/6 | 185.96s |  | json c |
| `code2inv_133` | `127.c` | pass | 5/5 | 10/10 | 195.61s |  | json c |
| `code2inv_133` | `128.c` | pass | 5/5 | 4/4 | 78.68s |  | json c |
| `code2inv_133` | `129.c` | pass | 5/5 | 4/4 | 78.72s |  | json c |
| `code2inv_133` | `13.c` | pass | 5/5 | 5/5 | 152.2s |  | json c |
| `code2inv_133` | `130.c` | pass | 1/5 | 10/10 | 1434.62s |  | json c |
| `code2inv_133` | `131.c` | pass | 1/5 | 16/16 | 1153.53s |  | json c |
| `code2inv_133` | `132.c` | pass | 5/5 | 5/5 | 310.81s |  | json c |
| `code2inv_133` | `133.c` | pass | 5/5 | 6/6 | 29.35s |  | json c |
| `code2inv_133` | `14.c` | pass | 5/5 | 5/5 | 153.44s |  | json c |
| `code2inv_133` | `15.c` | pass | 5/5 | 11/11 | 139.82s |  | json c |
| `code2inv_133` | `16.c` | pass | 5/5 | 11/11 | 126.18s |  | json c |
| `code2inv_133` | `17.c` | pass | 5/5 | 26/26 | 113.52s |  | json c |
| `code2inv_133` | `18.c` | pass | 5/5 | 11/11 | 104.71s |  | json c |
| `code2inv_133` | `19.c` | pass | 5/5 | 11/11 | 112.48s |  | json c |
| `code2inv_133` | `2.c` | pass | 5/5 | 10/10 | 307.33s |  | json c |
| `code2inv_133` | `20.c` | pass | 5/5 | 11/11 | 146.67s |  | json c |
| `code2inv_133` | `21.c` | pass | 5/5 | 11/11 | 420.37s |  | json c |
| `code2inv_133` | `22.c` | pass | 5/5 | 7/7 | 147.92s |  | json c |
| `code2inv_133` | `23.c` | pass | 4/5 | 24/24 | 586.78s |  | json c |
| `code2inv_133` | `24.c` | fail | 0/5 | 19/20 | 1362.89s | fail | json c |
| `code2inv_133` | `25.c` | pass | 5/5 | 4/4 | 33.3s |  | json c |
| `code2inv_133` | `26.c` | fail | 0/5 | 3/4 | 981.97s | fail | json c |
| `code2inv_133` | `27.c` | fail | 0/5 | 3/4 | 531.42s | fail | json c |
| `code2inv_133` | `28.c` | pass | 2/5 | 4/4 | 648.48s |  | json c |
| `code2inv_133` | `29.c` | pass | 5/5 | 4/4 | 26.57s |  | json c |
| `code2inv_133` | `3.c` | pass | 5/5 | 18/18 | 188.44s |  | json c |
| `code2inv_133` | `30.c` | pass | 5/5 | 4/4 | 26.14s |  | json c |
| `code2inv_133` | `31.c` | fail | 0/5 | 5/6 | 1067.07s | fail | json c |
| `code2inv_133` | `32.c` | fail | 0/5 | 7/8 | 996.54s | fail | json c |
| `code2inv_133` | `33.c` | pass | 5/5 | 6/6 | 110.49s |  | json c |
| `code2inv_133` | `34.c` | pass | 5/5 | 6/6 | 36.84s |  | json c |
| `code2inv_133` | `35.c` | pass | 5/5 | 8/8 | 42.1s |  | json c |
| `code2inv_133` | `36.c` | pass | 5/5 | 6/6 | 35.87s |  | json c |
| `code2inv_133` | `37.c` | pass | 5/5 | 8/8 | 48.56s |  | json c |
| `code2inv_133` | `38.c` | pass | 5/5 | 7/7 | 40.28s |  | json c |
| `code2inv_133` | `39.c` | pass | 5/5 | 7/7 | 35.02s |  | json c |
| `code2inv_133` | `4.c` | pass | 5/5 | 12/12 | 375.63s |  | json c |
| `code2inv_133` | `40.c` | pass | 5/5 | 6/6 | 84.07s |  | json c |
| `code2inv_133` | `41.c` | pass | 5/5 | 6/6 | 33.88s |  | json c |
| `code2inv_133` | `42.c` | pass | 5/5 | 8/8 | 111.24s |  | json c |
| `code2inv_133` | `43.c` | pass | 5/5 | 6/6 | 84.14s |  | json c |
| `code2inv_133` | `44.c` | pass | 5/5 | 6/6 | 78.36s |  | json c |
| `code2inv_133` | `45.c` | pass | 5/5 | 8/8 | 53.23s |  | json c |
| `code2inv_133` | `46.c` | pass | 5/5 | 8/8 | 36.89s |  | json c |
| `code2inv_133` | `47.c` | pass | 5/5 | 8/8 | 65.78s |  | json c |
| `code2inv_133` | `48.c` | pass | 5/5 | 8/8 | 35.82s |  | json c |
| `code2inv_133` | `49.c` | pass | 5/5 | 8/8 | 208.04s |  | json c |
| `code2inv_133` | `5.c` | fail | 0/5 | 3/4 | 534.47s | fail | json c |
| `code2inv_133` | `50.c` | pass | 5/5 | 8/8 | 40.16s |  | json c |
| `code2inv_133` | `51.c` | pass | 5/5 | 8/8 | 35.25s |  | json c |
| `code2inv_133` | `52.c` | pass | 5/5 | 6/6 | 34.34s |  | json c |
| `code2inv_133` | `53.c` | pass | 5/5 | 6/6 | 85.8s |  | json c |
| `code2inv_133` | `54.c` | pass | 5/5 | 6/6 | 34.3s |  | json c |
| `code2inv_133` | `55.c` | pass | 5/5 | 8/8 | 118.66s |  | json c |
| `code2inv_133` | `56.c` | pass | 5/5 | 12/12 | 151.53s |  | json c |
| `code2inv_133` | `57.c` | pass | 5/5 | 8/8 | 131.79s |  | json c |
| `code2inv_133` | `58.c` | pass | 5/5 | 8/8 | 48.96s |  | json c |
| `code2inv_133` | `59.c` | pass | 5/5 | 8/8 | 35.78s |  | json c |
| `code2inv_133` | `6.c` | fail | 0/5 | 3/4 | 621.16s | fail | json c |
| `code2inv_133` | `60.c` | pass | 5/5 | 10/10 | 65.89s |  | json c |
| `code2inv_133` | `61.c` | fail | 0/5 | 15/16 | 1468.0s | fail | json c |
| `code2inv_133` | `62.c` | fail | 0/5 | 23/24 | 1387.31s | fail | json c |
| `code2inv_133` | `63.c` | pass | 5/5 | 10/10 | 132.76s |  | json c |
| `code2inv_133` | `64.c` | pass | 3/5 | 10/10 | 537.97s |  | json c |
| `code2inv_133` | `65.c` | pass | 5/5 | 8/8 | 89.78s |  | json c |
| `code2inv_133` | `66.c` | pass | 1/5 | 10/10 | 980.0s |  | json c |
| `code2inv_133` | `67.c` | pass | 5/5 | 12/12 | 180.04s |  | json c |
| `code2inv_133` | `68.c` | pass | 5/5 | 12/12 | 166.66s |  | json c |
| `code2inv_133` | `69.c` | pass | 5/5 | 8/8 | 164.16s |  | json c |
| `code2inv_133` | `7.c` | pass | 5/5 | 7/7 | 154.42s |  | json c |
| `code2inv_133` | `70.c` | pass | 5/5 | 20/20 | 298.54s |  | json c |
| `code2inv_133` | `71.c` | pass | 5/5 | 11/11 | 163.37s |  | json c |
| `code2inv_133` | `72.c` | fail | 0/5 | 16/17 | 1070.53s | fail | json c |
| `code2inv_133` | `73.c` | pass | 5/5 | 9/9 | 163.66s |  | json c |
| `code2inv_133` | `74.c` | pass | 5/5 | 11/11 | 157.33s |  | json c |
| `code2inv_133` | `75.c` | fail | 0/5 | 16/17 | 1158.02s | fail | json c |
| `code2inv_133` | `76.c` | pass | 5/5 | 9/9 | 174.83s |  | json c |
| `code2inv_133` | `77.c` | pass | 5/5 | 9/9 | 47.03s |  | json c |
| `code2inv_133` | `78.c` | pass | 5/5 | 5/5 | 39.88s |  | json c |
| `code2inv_133` | `79.c` | pass | 5/5 | 7/7 | 41.1s |  | json c |
| `code2inv_133` | `8.c` | pass | 5/5 | 5/5 | 153.39s |  | json c |
| `code2inv_133` | `80.c` | pass | 5/5 | 9/9 | 50.19s |  | json c |
| `code2inv_133` | `81.c` | pass | 5/5 | 5/5 | 34.66s |  | json c |
| `code2inv_133` | `82.c` | pass | 5/5 | 7/7 | 40.9s |  | json c |
| `code2inv_133` | `83.c` | pass | 5/5 | 6/6 | 501.67s |  | json c |
| `code2inv_133` | `84.c` | pass | 5/5 | 6/6 | 440.89s |  | json c |
| `code2inv_133` | `85.c` | pass | 5/5 | 10/10 | 407.9s |  | json c |
| `code2inv_133` | `86.c` | pass | 5/5 | 10/10 | 479.8s |  | json c |
| `code2inv_133` | `87.c` | pass | 5/5 | 7/7 | 337.62s |  | json c |
| `code2inv_133` | `88.c` | pass | 5/5 | 7/7 | 465.23s |  | json c |
| `code2inv_133` | `89.c` | pass | 5/5 | 7/7 | 278.55s |  | json c |
| `code2inv_133` | `9.c` | pass | 5/5 | 5/5 | 151.72s |  | json c |
| `code2inv_133` | `90.c` | pass | 5/5 | 13/13 | 408.22s |  | json c |
| `code2inv_133` | `91.c` | pass | 5/5 | 4/4 | 26.5s |  | json c |
| `code2inv_133` | `92.c` | pass | 5/5 | 4/4 | 25.99s |  | json c |
| `code2inv_133` | `93.c` | pass | 5/5 | 9/9 | 331.27s |  | json c |
| `code2inv_133` | `94.c` | pass | 5/5 | 16/16 | 219.27s |  | json c |
| `code2inv_133` | `95.c` | pass | 5/5 | 4/4 | 199.88s |  | json c |
| `code2inv_133` | `96.c` | pass | 5/5 | 4/4 | 162.1s |  | json c |
| `code2inv_133` | `97.c` | pass | 5/5 | 6/6 | 147.28s |  | json c |
| `code2inv_133` | `98.c` | pass | 5/5 | 4/4 | 148.66s |  | json c |
| `code2inv_133` | `99.c` | pass | 5/5 | 8/8 | 94.16s |  | json c |
| `fib_46` | `01.c` | pass | 5/5 | 7/7 | 96.15s |  | json c |
| `fib_46` | `02.c` | pass | 5/5 | 15/15 | 677.94s |  | json c |
| `fib_46` | `03.c` | pass | 5/5 | 16/16 | 66.82s |  | json c |
| `fib_46` | `04.c` | pass | 5/5 | 4/4 | 171.14s |  | json c |
| `fib_46` | `05.c` | pass | 5/5 | 11/11 | 585.99s |  | json c |
| `fib_46` | `06.c` | fail | 0/5 | None/None | 541.69s | fail | json c |
| `fib_46` | `07.c` | pass | 5/5 | 9/9 | 334.21s |  | json c |
| `fib_46` | `08.c` | pass | 4/5 | 11/11 | 796.13s |  | json c |
| `fib_46` | `09.c` | fail | 0/5 | 24/26 | 1099.54s | fail | json c |
| `fib_46` | `10.c` | pass | 5/5 | 17/17 | 659.12s |  | json c |
| `fib_46` | `11.c` | pass | 5/5 | 8/8 | 32.85s |  | json c |
| `fib_46` | `12.c` | pass | 3/5 | 45/45 | 1121.63s |  | json c |
| `fib_46` | `13.c` | pass | 1/5 | 9/9 | 1068.57s |  | json c |
| `fib_46` | `14.c` | pass | 5/5 | 12/12 | 98.57s |  | json c |
| `fib_46` | `15.c` | pass | 4/5 | 12/12 | 325.63s |  | json c |
| `fib_46` | `16.c` | pass | 5/5 | 6/6 | 185.57s |  | json c |
| `fib_46` | `17.c` | pass | 4/5 | 17/17 | 918.15s |  | json c |
| `fib_46` | `18.c` | pass | 5/5 | 8/8 | 47.24s |  | json c |
| `fib_46` | `19.c` | pass | 5/5 | 8/8 | 97.48s |  | json c |
| `fib_46` | `20.c` | pass | 5/5 | 21/21 | 801.8s |  | json c |
| `fib_46` | `21.c` | pass | 5/5 | 9/9 | 304.0s |  | json c |
| `fib_46` | `22.c` | pass | 5/5 | 10/10 | 383.31s |  | json c |
| `fib_46` | `23.c` | pass | 5/5 | 10/10 | 181.53s |  | json c |
| `fib_46` | `24.c` | fail | 0/5 | 7/8 | 1023.37s | fail | json c |
| `fib_46` | `25.c` | pass | 5/5 | 14/14 | 468.92s |  | json c |
| `fib_46` | `26.c` | pass | 2/5 | 51/51 | 1146.83s |  | json c |
| `fib_46` | `27.c` | pass | 5/5 | 18/18 | 190.2s |  | json c |
| `fib_46` | `28.c` | pass | 5/5 | 14/14 | 106.28s |  | json c |
| `fib_46` | `29.c` | pass | 5/5 | 18/18 | 1102.68s |  | json c |
| `fib_46` | `30.c` | pass | 5/5 | 8/8 | 100.48s |  | json c |
| `fib_46` | `31.c` | pass | 5/5 | 20/20 | 460.09s |  | json c |
| `fib_46` | `32.c` | pass | 1/5 | 14/14 | 1351.46s |  | json c |
| `fib_46` | `33.c` | fail | 0/5 | 19/20 | 1270.71s | fail | json c |
| `fib_46` | `34.c` | pass | 5/5 | 12/12 | 216.48s |  | json c |
| `fib_46` | `35.c` | pass | 5/5 | 6/6 | 30.4s |  | json c |
| `fib_46` | `36.c` | fail | 0/5 | 27/29 | 1296.87s | fail | json c |
| `fib_46` | `37.c` | pass | 4/5 | 15/15 | 843.7s |  | json c |
| `fib_46` | `38.c` | pass | 5/5 | 10/10 | 214.05s |  | json c |
| `fib_46` | `39.c` | fail | 0/5 | None/None | 65.62s | fail | json c |
| `fib_46` | `40.c` | pass | 5/5 | 27/27 | 607.31s |  | json c |
| `fib_46` | `41.c` | pass | 5/5 | 10/10 | 214.7s |  | json c |
| `fib_46` | `42.c` | fail | 0/5 | 12/13 | 1469.28s | fail | json c |
| `fib_46` | `43.c` | pass | 5/5 | 5/5 | 81.87s |  | json c |
| `fib_46` | `44.c` | pass | 5/5 | 8/8 | 161.44s |  | json c |
| `fib_46` | `45.c` | fail | 0/5 | None/None | 577.42s | fail | json c |
| `fib_46` | `46.c` | pass | 5/5 | 11/11 | 413.21s |  | json c |
| `frama-c-problems` | `arrays_and_loops/1.c` | pass | 5/5 | 3/3 | 171.1s |  | json c |
| `frama-c-problems` | `arrays_and_loops/2.c` | pass | 5/5 | 14/14 | 118.62s |  | json c |
| `frama-c-problems` | `arrays_and_loops/3.c` | fail | 0/5 | 7/8 | 331.24s | fail | json c |
| `frama-c-problems` | `arrays_and_loops/4.c` | pass | 5/5 | 9/9 | 90.45s |  | json c |
| `frama-c-problems` | `arrays_and_loops/5.c` | fail | 0/5 | 6/10 | 504.08s | fail | json c |
| `frama-c-problems` | `general_wp_problems/absolute_value.c` | pass | 3/5 | 8/8 | 318.48s |  | json c |
| `frama-c-problems` | `general_wp_problems/add.c` | pass | 5/5 | 3/3 | 138.31s |  | json c |
| `frama-c-problems` | `general_wp_problems/ani.c` | fail | 0/5 | 47/49 | 1134.33s | fail | json c |
| `frama-c-problems` | `general_wp_problems/diff.c` | pass | 5/5 | 4/4 | 89.98s |  | json c |
| `frama-c-problems` | `general_wp_problems/gcd.c` | fail | 0/5 | 13/14 | 2218.2s | fail | json c |
| `frama-c-problems` | `general_wp_problems/max_of_2.c` | pass | 5/5 | 5/5 | 88.64s |  | json c |
| `frama-c-problems` | `general_wp_problems/power.c` | fail | 0/5 | 15/19 | 1316.06s | fail | json c |
| `frama-c-problems` | `general_wp_problems/simple_interest.c` | pass | 5/5 | 2/2 | 231.87s |  | json c |
| `frama-c-problems` | `general_wp_problems/swap.c` | pass | 3/5 | 7/7 | 273.6s |  | json c |
| `frama-c-problems` | `general_wp_problems/triangle_angles.c` | pass | 5/5 | 14/14 | 135.95s |  | json c |
| `frama-c-problems` | `general_wp_problems/triangle_sides.c` | pass | 3/5 | 10/10 | 122.77s |  | json c |
| `frama-c-problems` | `general_wp_problems/wp1.c` | fail | 0/5 | 0/4 | 671.28s | fail | json c |
| `frama-c-problems` | `immutable_arrays/array_sum.c` | pass | 5/5 | 15/15 | 46.91s |  | json c |
| `frama-c-problems` | `immutable_arrays/binary_search.c` | fail | 0/5 | 16/20 | 802.19s | fail | json c |
| `frama-c-problems` | `immutable_arrays/check_evens_in_array.c` | pass | 5/5 | 13/13 | 40.59s |  | json c |
| `frama-c-problems` | `immutable_arrays/max.c` | pass | 5/5 | 14/14 | 120.6s |  | json c |
| `frama-c-problems` | `immutable_arrays/occurences_of_x.c` | fail | 0/5 | 4/8 | 1056.16s | fail | json c |
| `frama-c-problems` | `immutable_arrays/sample.c` | pass | 5/5 | 6/6 | 501.36s |  | json c |
| `frama-c-problems` | `immutable_arrays/search.c` | pass | 5/5 | 13/13 | 40.66s |  | json c |
| `frama-c-problems` | `immutable_arrays/search_2.c` | pass | 5/5 | 13/13 | 40.93s |  | json c |
| `frama-c-problems` | `loops/1.c` | pass | 5/5 | 6/6 | 38.66s |  | json c |
| `frama-c-problems` | `loops/2.c` | fail | 0/5 | 4/8 | 229.06s | fail | json c |
| `frama-c-problems` | `loops/3.c` | fail | 0/5 | 6/8 | 154.22s | fail | json c |
| `frama-c-problems` | `loops/4.c` | fail | 0/5 | 3/7 | 1761.4s | fail | json c |
| `frama-c-problems` | `loops/fact.c` | fail | 0/5 | 6/8 | 168.13s | fail | json c |
| `frama-c-problems` | `loops/mult.c` | fail | 0/5 | 6/8 | 662.04s | fail | json c |
| `frama-c-problems` | `loops/sum_digits.c` | fail | 0/5 | 8/10 | 1121.71s | fail | json c |
| `frama-c-problems` | `loops/sum_even.c` | fail | 0/5 | 6/8 | 176.41s | fail | json c |
| `frama-c-problems` | `miscellaneous/array_find.c` | pass | 5/5 | 16/16 | 42.38s |  | json c |
| `frama-c-problems` | `miscellaneous/array_max_advanced.c` | pass | 5/5 | 14/14 | 44.82s |  | json c |
| `frama-c-problems` | `miscellaneous/array_swap.c` | pass | 5/5 | 15/15 | 254.78s |  | json c |
| `frama-c-problems` | `miscellaneous/increment_arr.c` | pass | 5/5 | 17/17 | 110.22s |  | json c |
| `frama-c-problems` | `miscellaneous/max_of_2.c` | pass | 5/5 | 5/5 | 89.97s |  | json c |
| `frama-c-problems` | `more_arrays/equal_arrays.c` | pass | 5/5 | 14/14 | 41.1s |  | json c |
| `frama-c-problems` | `more_arrays/replace_evens.c` | pass | 5/5 | 13/13 | 42.73s |  | json c |
| `frama-c-problems` | `more_arrays/reverse_array.c` | fail | 0/5 | None/None | 250.95s | fail | json c |
| `frama-c-problems` | `mutable_arrays/array_double.c` | pass | 5/5 | 18/18 | 154.7s |  | json c |
| `frama-c-problems` | `mutable_arrays/bubble_sort.c` | fail | 0/5 | None/None | 262.22s | fail | json c |
| `frama-c-problems` | `pointers/add_pointers.c` | pass | 5/5 | 10/10 | 96.85s |  | json c |
| `frama-c-problems` | `pointers/add_pointers_3_vars.c` | pass | 5/5 | 12/12 | 194.86s |  | json c |
| `frama-c-problems` | `pointers/div_rem.c` | pass | 5/5 | 10/10 | 738.95s |  | json c |
| `frama-c-problems` | `pointers/incr_a_by_b.c` | pass | 5/5 | 9/9 | 168.34s |  | json c |
| `frama-c-problems` | `pointers/max_pointers.c` | pass | 5/5 | 9/9 | 97.8s |  | json c |
| `frama-c-problems` | `pointers/order_3.c` | fail | 0/5 | 17/21 | 1502.74s | fail | json c |
| `frama-c-problems` | `pointers/reset_1st.c` | pass | 5/5 | 12/12 | 204.66s |  | json c |
| `frama-c-problems` | `pointers/swap.c` | pass | 3/5 | 8/8 | 229.98s |  | json c |
| `svcomp` | `quantifier-free/afnp2014_true-unreach-call/afnp2014_true-unreach-call.c` | pass | 4/5 | 13/13 | 290.12s |  | json c |
| `svcomp` | `quantifier-free/bhmr2007/bhmr2007_true-unreach-call.c` | pass | 5/5 | 9/9 | 39.31s |  | json c |
| `svcomp` | `quantifier-free/cggmp2005_true-unreach-call/cggmp2005_true-unreach-call.c` | pass | 5/5 | 12/12 | 149.77s |  | json c |
| `svcomp` | `quantifier-free/count_up_down/count_up_down_true-unreach-call_true-termination.c` | pass | 5/5 | 24/24 | 479.57s |  | json c |
| `svcomp` | `quantifier-free/css2003_true-unreach-call/css2003_true-unreach-call.c` | pass | 5/5 | 10/10 | 292.28s |  | json c |
| `svcomp` | `quantifier-free/ddlm2013_true-unreach-call/ddlm2013_true-unreach-call.c` | pass | 2/5 | 15/15 | 1065.0s |  | json c |
| `svcomp` | `quantifier-free/down_true-unreach-call/down_true-unreach-call.c` | pass | 5/5 | 15/15 | 68.08s |  | json c |
| `svcomp` | `quantifier-free/half_2_true-unreach-call/half_2_true-unreach-call.c` | fail | 0/5 | 35/37 | 2182.16s | fail | json c |
| `svcomp` | `quantifier-free/hhk2008/hhk2008_true-unreach-call.c` | pass | 5/5 | 6/6 | 119.8s |  | json c |
| `svcomp` | `quantifier-free/jm2006/jm2006_variant_true-unreach-call.c` | pass | 5/5 | 26/26 | 403.64s |  | json c |
| `svcomp` | `quantifier-free/jm2006_true_unreach_call/jm2006_true-unreach-call.c` | pass | 5/5 | 26/26 | 283.54s |  | json c |
| `svcomp` | `quantifier-free/large_const_true-unreach-call/large_const_true-unreach-call.c` | pass | 5/5 | 35/35 | 569.85s |  | json c |
| `svcomp` | `quantifier-free/nest-if3_true-unreach-call/nest-if3_true-unreach-call.c` | fail | 0/5 | 15/16 | 1909.91s | fail | json c |
| `svcomp` | `quantifier-free/nested6_true-unreach-call/nested6_true-unreach-call.c` | pass | 1/5 | 23/23 | 1580.36s |  | json c |
| `svcomp` | `quantifier-free/nested9_true-unreach-call/nested9_true-unreach-call.c` | fail | 0/5 | 5/6 | 1532.12s | fail | json c |
| `svcomp` | `quantifier-free/seq_true-unreach-call/seq_true-unreach-call.c` | fail | 0/5 | 27/30 | 1469.07s | fail | json c |
| `svcomp` | `quantifier-free/sum01/sum01_true-unreach-call_true-termination.c` | pass | 5/5 | 32/32 | 147.25s |  | json c |
| `svcomp` | `quantifier-free/terminator03/terminator_03_true-unreach-call_true-termination.c` | pass | 1/5 | 6/6 | 628.41s |  | json c |
| `svcomp` | `quantifier-free/up_true-unreach-call/up_true-unreach-call.c` | pass | 5/5 | 17/17 | 107.67s |  | json c |
| `svcomp` | `quantifier/array_true-unreach-call1/array_true-unreach-call1.c` | pass | 5/5 | 8/8 | 39.95s |  | json c |
| `svcomp` | `quantifier/array_true-unreach-call2/array_true-unreach-call2.c` | pass | 5/5 | 8/8 | 38.12s |  | json c |
