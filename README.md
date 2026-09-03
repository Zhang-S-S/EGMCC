# EGMCC Live Demo

This local front end performs live EGMCC graph computation. It supports four
paper datasets plus temporary user uploads and computes exactly one selected
function per run.

## Coverage

- Datasets: `soc-sign-bitcoin-otc`, `email-Enron`, `musae-github`, and `web-NotreDame`.
- Centrality: Betweenness, Closeness, PageRank, Eigenvector, and Katz.
- Structural hole: Constraint, Effective Size, Hierarchy, and Efficiency.
- Path: Eccentricity and Average Shortest Path Length.

Path functions use the largest strongly connected component for directed graphs
and the largest connected component for undirected graphs.

## Project layout

- `server.py` — HTTP server, job scheduling, and resource sampling.
- `catalog.py` — dataset, algorithm, and benchmark backend definitions.
- `uploaded_datasets.py` — upload storage, validation, expiry, and cleanup.
- `scripts/run_algorithm.py` — runs one dataset/algorithm/backend combination in an isolated subprocess.
- `app.js`, `index.html`, `styles.css` — browser front end.
- `data/` — the four built-in paper datasets.
- `EGMCC.pdf` — the paper, linked from the navigation bar.

## Run

Python 3.12 is recommended. EGMCC is included in the official
`python-easygraph` 1.6.2 release, so a standard pip install is sufficient.

```bash
python server.py
```

Then open <http://127.0.0.1:4173>.

The thread count defaults to 56 (matching the paper's Enron case study) and
can be adjusted in the front end; the server accepts values from 1 to 256.

### Interface tabs

After a run, results are organized into five tabs:

- **Results** — node-level score table with search, sorting, pagination, and CSV/JSON download.
- **Statistics** — score distribution (mean, median, standard deviation, percentiles) with a histogram.
- **Resource Usage** — live CPU and memory sampling curve captured from the worker process.
- **Benchmark** — per-backend runtime comparison on a logarithmic scale (Benchmark mode only).
- **Run Log** — timestamped lifecycle log of the current run; can be cleared.

Analysis mode launches one isolated EGMCC worker. Browser polling keeps long
calculations from blocking HTTP requests, and CPU/RSS data is sampled from the
real worker.

## Upload a dataset

Use **Upload** next to the Dataset selector to add a local edge-list file. The
demo accepts UTF-8 `.csv` and `.txt` files up to 100 MB:

```text
source target
alice bob
bob carol
```

CSV files normally use commas, while TXT files normally use spaces or tabs;
the server falls back to the other delimiter when needed. The first two
columns are source and target node IDs. Enable the weight option to read a
finite numeric edge weight from column three. Node IDs may be numbers or
arbitrary strings, and the upload form also lets you select directed or
undirected behavior and indicate whether the first data row is a header.

Blank lines and lines beginning with `#` are ignored. A malformed data row
rejects the upload and reports its line number. Successful uploads are stored
locally under `uploads/`, remain available after a server restart, work in both
Analysis and Benchmark modes, and expire automatically after 24 hours.

## Resource monitoring

On Linux, CPU is sampled every 0.05 seconds from `/proc/<pid>/stat` using the
CPU-time delta between adjacent samples. Peak CPU is reported relative to the
configured thread count, while each metric point also retains the raw process
CPU and whole-host CPU percentages. On non-Linux platforms (macOS, BSD) the
sampler falls back to `ps`, so CPU and memory monitoring remain available.

## Benchmark mode

Benchmark mode lets you choose any subset of `EGMCC`, `graph-tool`, NetworKit,
igraph, and NetworkX, then runs the selected libraries strictly in that order.
At least one library must be selected, and only one backend subprocess exists
at a time. Missing libraries and unsupported functions are reported instead of
being replaced with fabricated data. Optional benchmark libraries must be in
the same Python environment. The interactive default is three timed runs per
backend; set it to 30 to reproduce the paper's experimental protocol, which
uses NetworKit 11.2.1.

To install the paper-matched optional baseline:

```bash
pip install python-easygraph==1.6.2
pip install networkx==3.6.1
pip install igraph==1.0.0
conda install -c conda-forge graph-tool=3.0
pip install networkit==11.2.1
```
