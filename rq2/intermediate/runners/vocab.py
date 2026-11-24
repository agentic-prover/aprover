import re, sys, os, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from norm import records

pat = re.compile(sys.argv[1])
c = collections.Counter()
for f in sys.argv[2:]:
    for ts, lev, m in records(f):
        if pat.search(m):
            t = re.sub(r"\d+", "N", re.sub(r"'[^']*'", "Q", m))[:100]
            c[t] += 1
for t, n in c.most_common(45):
    print("%5d  %s" % (n, t))
