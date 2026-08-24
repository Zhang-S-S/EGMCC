# EGMCC Live Demo

This local front end performs live EGMCC graph computation. It supports four
paper datasets and computes exactly one selected function per run.

## Coverage

- Datasets: `soc-sign-bitcoin-otc`, `email-Enron`, `musae-github`, and `web-NotreDame`.
- Centrality: Betweenness, Closeness, PageRank, Eigenvector, and Katz.
- Structural hole: Constraint, Effective Size, Hierarchy, and Efficiency.
- Path: Eccentricity and Average Shortest Path Length.

Path functions use the largest strongly connected component for directed graphs
and the largest connected component for undirected graphs.

## Run

Use the same Python environment that can import the EGMCC-enabled `easygraph`
package:

```bash
conda activate env_312
python server.py
```

Then open <http://127.0.0.1:4173>.

Analysis mode launches one isolated EGMCC worker. Browser polling keeps long
calculations from blocking HTTP requests, and CPU/RSS data is sampled from the
real worker. Completed results support search, sorting, pagination, and CSV/JSON
downloads.

On Linux, CPU is sampled every 0.05 seconds from `/proc/<pid>/stat` using the
CPU-time delta between adjacent samples. Peak CPU is reported relative to the
configured thread count (56 by default, matching the paper's Enron case study),
while each metric point also retains the raw process CPU and whole-host CPU
percentages.

Benchmark mode runs `EGMCC → graph-tool → NetworKit → igraph → NetworkX` strictly in that
order. Only one backend subprocess exists at a time. Missing libraries and
unsupported functions are reported instead of being replaced with fabricated
data. Optional benchmark libraries must be in the same Python environment. The
interactive default is three timed runs per backend; set it to 30 to reproduce
the paper's experimental protocol, which uses NetworKit 11.2.1.

To install the paper-matched optional baseline:

```bash
python -m pip install networkit==11.2.1
```
