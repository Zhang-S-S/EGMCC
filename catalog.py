"""Datasets, algorithms, parameters, and benchmark backends for the demo."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent


PAPER_CASE_STUDY = {
    "dataset": "enron",
    "algorithm": "betweenness",
}


DATASETS = {
    "bitcoinotc": {
        "id": "bitcoinotc",
        "label": "soc-sign-bitcoin-otc",
        "short_label": "OTC",
        "path": ROOT / "data" / "bitcoinotc.csv",
        "directed": True,
        "weighted": True,
        "nodes": 5_881,
        "edges": 35_592,
        "format": "csv",
        "header": False,
        "weight_transform": "bitcoin_positive",
    },
    "enron": {
        "id": "enron",
        "label": "email-Enron",
        "short_label": "Enron",
        "path": ROOT / "data" / "email-Enron.txt",
        "directed": False,
        "weighted": False,
        "nodes": 36_692,
        "edges": 183_831,
        "format": "whitespace",
        "header": False,
    },
    "github": {
        "id": "github",
        "label": "musae-github",
        "short_label": "GitHub",
        "path": ROOT / "data" / "musae_git.csv",
        "directed": False,
        "weighted": False,
        "nodes": 37_700,
        "edges": 289_003,
        "format": "csv",
        "header": True,
    },
    "notredame": {
        "id": "notredame",
        "label": "web-NotreDame",
        "short_label": "NotreDame",
        "path": ROOT / "data" / "web-NotreDame.txt",
        "directed": True,
        "weighted": False,
        "nodes": 325_729,
        "edges": 1_497_134,
        "format": "whitespace",
        "header": False,
    },
}


COMMON_WEIGHT = {
    "id": "weight_mode",
    "label": "Edge weight",
    "type": "select",
    "default": "unweighted",
    "options": [
        {"value": "unweighted", "label": "Unweighted"},
        {"value": "weighted", "label": "Dataset weight (when available)"},
    ],
}

ITERATION_PARAMS = [
    {"id": "tolerance", "label": "Tolerance", "type": "number", "default": 1e-6, "min": 1e-12, "step": 1e-6},
    {"id": "max_iter", "label": "Max iterations", "type": "integer", "default": 1000, "min": 1, "step": 1},
]


ALGORITHMS = {
    "betweenness": {
        "id": "betweenness",
        "label": "Betweenness",
        "family": "Centrality",
        "result_kind": "node_scores",
        "result_label": "Betweenness",
        "parameters": [COMMON_WEIGHT],
    },
    "closeness": {
        "id": "closeness",
        "label": "Closeness",
        "family": "Centrality",
        "result_kind": "node_scores",
        "result_label": "Closeness",
        "parameters": [COMMON_WEIGHT],
    },
    "pagerank": {
        "id": "pagerank",
        "label": "PageRank",
        "family": "Centrality",
        "result_kind": "node_scores",
        "result_label": "PageRank",
        "parameters": [
            {"id": "alpha", "label": "Damping factor", "type": "number", "default": 0.85, "min": 0.0, "max": 1.0, "step": 0.01},
        ],
    },
    "eigenvector": {
        "id": "eigenvector",
        "label": "Eigenvector",
        "family": "Centrality",
        "result_kind": "node_scores",
        "result_label": "Eigenvector",
        "parameters": [COMMON_WEIGHT],
    },
    "katz": {
        "id": "katz",
        "label": "Katz",
        "family": "Centrality",
        "result_kind": "node_scores",
        "result_label": "Katz",
        "parameters": [
            {"id": "alpha", "label": "Alpha", "type": "number", "default": 0.01, "min": 0.0, "step": 0.001},
            {"id": "beta", "label": "Beta", "type": "number", "default": 1.0, "step": 0.1},
            *ITERATION_PARAMS,
        ],
    },
    "constraint": {
        "id": "constraint",
        "label": "Constraint",
        "family": "Structural Hole",
        "result_kind": "node_scores",
        "result_label": "Constraint",
        "parameters": [COMMON_WEIGHT],
    },
    "effective_size": {
        "id": "effective_size",
        "label": "Effective Size",
        "family": "Structural Hole",
        "result_kind": "node_scores",
        "result_label": "Effective Size",
        "parameters": [COMMON_WEIGHT],
    },
    "hierarchy": {
        "id": "hierarchy",
        "label": "Hierarchy",
        "family": "Structural Hole",
        "result_kind": "node_scores",
        "result_label": "Hierarchy",
        "parameters": [COMMON_WEIGHT],
    },
    "efficiency": {
        "id": "efficiency",
        "label": "Efficiency",
        "family": "Structural Hole",
        "result_kind": "node_scores",
        "result_label": "Efficiency",
        "parameters": [COMMON_WEIGHT],
    },
    "eccentricity": {
        "id": "eccentricity",
        "label": "Eccentricity",
        "family": "Path",
        "result_kind": "node_scores",
        "result_label": "Eccentricity",
        "largest_component": True,
        "parameters": [],
    },
    "average_shortest_path_length": {
        "id": "average_shortest_path_length",
        "label": "Average Shortest Path Length",
        "family": "Path",
        "result_kind": "scalar",
        "result_label": "Average path length",
        "largest_component": True,
        "parameters": [COMMON_WEIGHT],
    },
}


BACKENDS = [
    {"id": "easygraph", "label": "EGMCC"},
    {"id": "graph-tool", "label": "graph-tool"},
    {"id": "networkit", "label": "NetworKit"},
    {"id": "igraph", "label": "igraph"},
    {"id": "networkx", "label": "NetworkX"},
]


def public_catalog() -> dict:
    datasets = []
    for dataset in DATASETS.values():
        datasets.append({key: value for key, value in dataset.items() if key not in {"path", "format", "header", "weight_transform"}})
    return {
        "datasets": datasets,
        "algorithms": list(ALGORITHMS.values()),
        "backends": BACKENDS,
        "defaults": PAPER_CASE_STUDY,
        "single_active_job": True,
        "benchmark_serial": True,
    }
