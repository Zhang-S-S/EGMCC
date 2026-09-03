#!/usr/bin/env python3
"""Run one dataset/algorithm/backend combination in an isolated process."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog import ALGORITHMS, DATASETS
from uploaded_datasets import read_edge_list


class UnsupportedAlgorithm(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["easygraph", "graph-tool", "networkit", "igraph", "networkx"], required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--dataset-config")
    parser.add_argument("--algorithm", choices=ALGORITHMS, required=True)
    parser.add_argument("--threads", type=int, default=56)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--params", default="{}")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-only", action="store_true")
    return parser.parse_args(argv)


def dataset_configuration(args: argparse.Namespace) -> dict:
    if not args.dataset_config:
        if args.dataset not in DATASETS:
            raise ValueError("Unknown dataset.")
        return DATASETS[args.dataset]
    try:
        config = json.loads(args.dataset_config)
    except json.JSONDecodeError as error:
        raise ValueError("Invalid dataset configuration.") from error
    if not isinstance(config, dict) or config.get("id") != args.dataset:
        raise ValueError("Dataset configuration does not match the requested dataset.")
    required = {"path", "format", "header", "directed", "weighted"}
    if not required.issubset(config):
        raise ValueError("Dataset configuration is incomplete.")
    config["path"] = Path(config["path"])
    return config


def parse_node(value: str) -> int:
    return int(float(value.strip()))


def read_dataset(config: dict, weighted: bool) -> tuple[list[int | str], list[tuple[int | str, int | str, float | None]]]:
    path = Path(config["path"])
    if not path.exists():
        raise FileNotFoundError(path)
    if config.get("uploaded"):
        return read_edge_list(path, config["format"], bool(config.get("header")), weighted)
    edges: list[tuple[int, int, float | None]] = []
    nodes: set[int] = set()

    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        if config["format"] == "csv":
            reader = csv.reader(stream)
            if config.get("header"):
                next(reader, None)
            rows = reader
        else:
            rows = (line.split() for line in stream if line.strip() and not line.lstrip().startswith("#"))

        for row in rows:
            if len(row) < 2:
                continue
            try:
                source, target = parse_node(row[0]), parse_node(row[1])
            except ValueError:
                continue
            weight = None
            if weighted and len(row) >= 3:
                weight = float(row[2])
                if config.get("weight_transform") == "bitcoin_positive":
                    weight += 11.0
            edges.append((source, target, weight))
            nodes.add(source)
            nodes.add(target)
    return sorted(nodes), edges


def remap_edges(original_ids: list[int], edges: list[tuple[int, int, float | None]]):
    mapping = {node_id: index for index, node_id in enumerate(original_ids)}
    return [(mapping[source], mapping[target], weight) for source, target, weight in edges]


def largest_weak_component(node_count: int, edges: list[tuple[int, int, float | None]]) -> set[int]:
    adjacency = [[] for _ in range(node_count)]
    for source, target, _ in edges:
        adjacency[source].append(target)
        adjacency[target].append(source)
    visited = bytearray(node_count)
    largest: list[int] = []
    for start in range(node_count):
        if visited[start]:
            continue
        queue = [start]
        visited[start] = 1
        component = []
        for node in queue:
            component.append(node)
            for neighbor in adjacency[node]:
                if not visited[neighbor]:
                    visited[neighbor] = 1
                    queue.append(neighbor)
        if len(component) > len(largest):
            largest = component
    return set(largest)


def largest_strong_component(node_count: int, edges: list[tuple[int, int, float | None]]) -> set[int]:
    adjacency = [[] for _ in range(node_count)]
    reverse = [[] for _ in range(node_count)]
    for source, target, _ in edges:
        adjacency[source].append(target)
        reverse[target].append(source)

    visited = bytearray(node_count)
    order: list[int] = []
    for start in range(node_count):
        if visited[start]:
            continue
        visited[start] = 1
        stack = [(start, 0)]
        while stack:
            node, index = stack[-1]
            if index < len(adjacency[node]):
                neighbor = adjacency[node][index]
                stack[-1] = (node, index + 1)
                if not visited[neighbor]:
                    visited[neighbor] = 1
                    stack.append((neighbor, 0))
            else:
                order.append(node)
                stack.pop()

    visited = bytearray(node_count)
    largest: list[int] = []
    for start in reversed(order):
        if visited[start]:
            continue
        visited[start] = 1
        stack = [start]
        component = []
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in reverse[node]:
                if not visited[neighbor]:
                    visited[neighbor] = 1
                    stack.append(neighbor)
        if len(component) > len(largest):
            largest = component
    return set(largest)


def select_largest_component(
    original_ids: list[int],
    mapped_edges: list[tuple[int, int, float | None]],
    directed: bool,
) -> tuple[list[int], list[tuple[int, int, float | None]]]:
    component = (
        largest_strong_component(len(original_ids), mapped_edges)
        if directed
        else largest_weak_component(len(original_ids), mapped_edges)
    )
    selected = sorted(component)
    new_index = {old: new for new, old in enumerate(selected)}
    new_ids = [original_ids[old] for old in selected]
    new_edges = [
        (new_index[source], new_index[target], weight)
        for source, target, weight in mapped_edges
        if source in component and target in component
    ]
    if len(new_ids) <= 1:
        raise ValueError("The largest connected component contains fewer than two nodes.")
    return new_ids, new_edges


def normalized_params(raw: dict, dataset: dict) -> dict:
    weighted = raw.get("weight_mode") == "weighted"
    if weighted and not dataset.get("weighted"):
        raise ValueError(f"{dataset['label']} does not provide an edge-weight column.")
    return {
        "weighted": weighted,
        "weight": "weight" if weighted else None,
        "alpha": float(raw.get("alpha", 0.85)),
        "beta": float(raw.get("beta", 1.0)),
        "tolerance": float(raw.get("tolerance", 1e-6)),
        "max_iter": int(raw.get("max_iter", 1000)),
    }


def build_easygraph(node_count, edges, directed, weighted):
    import easygraph as eg

    graph = eg.DiGraphC() if directed else eg.GraphC()
    graph.add_nodes_from(range(node_count))
    if weighted:
        graph.add_edges_from([(source, target, {"weight": float(weight)}) for source, target, weight in edges])
    else:
        graph.add_edges_from([(source, target) for source, target, _ in edges])
    return graph, eg


def build_networkx(node_count, edges, directed, weighted):
    import networkx as nx

    graph = nx.DiGraph() if directed else nx.Graph()
    graph.add_nodes_from(range(node_count))
    if weighted:
        graph.add_weighted_edges_from([(source, target, float(weight)) for source, target, weight in edges])
    else:
        graph.add_edges_from([(source, target) for source, target, _ in edges])
    return graph, nx


def build_igraph(node_count, edges, directed, weighted):
    import igraph as ig

    graph = ig.Graph(n=node_count, edges=[(source, target) for source, target, _ in edges], directed=directed)
    if weighted:
        graph.es["weight"] = [float(weight) for _, _, weight in edges]
    return graph, ig


def build_graph_tool(node_count, edges, directed, weighted):
    import graph_tool.all as gt

    graph = gt.Graph(directed=directed)
    graph.add_vertex(node_count)
    graph.add_edge_list([(source, target) for source, target, _ in edges])
    weight_property = None
    if weighted:
        weight_property = graph.new_edge_property("double")
        for edge, (_, _, weight) in zip(graph.edges(), edges):
            weight_property[edge] = float(weight)
    return graph, (gt, weight_property)


def build_networkit(node_count, edges, directed, weighted):
    import networkit as nk

    nk.setNumberOfThreads(max(1, int(os.environ.get("OMP_NUM_THREADS", "1"))))
    graph = nk.Graph(node_count, weighted=weighted, directed=directed)
    for source, target, weight in edges:
        graph.addEdge(source, target, float(weight) if weighted else 1.0)
    return graph, nk


def execute_easygraph(graph, module, algorithm: str, params: dict):
    weight = params["weight"]
    calls = {
        "betweenness": lambda: module.betweenness_centrality(graph, weight=weight),
        "closeness": lambda: module.closeness_centrality(graph, weight=weight),
        "pagerank": lambda: module.pagerank(graph, alpha=params["alpha"]),
        "eigenvector": lambda: module.eigenvector_centrality(graph, weight=weight),
        "katz": lambda: module.katz_centrality(graph, alpha=params["alpha"], beta=params["beta"], max_iter=params["max_iter"], tol=params["tolerance"]),
        "constraint": lambda: module.constraint(graph, weight=weight),
        "effective_size": lambda: module.effective_size(graph, weight=weight),
        "hierarchy": lambda: module.hierarchy(graph, weight=weight),
        "efficiency": lambda: module.efficiency(graph, weight=weight),
        "eccentricity": lambda: module.eccentricity(graph),
        "average_shortest_path_length": lambda: module.average_shortest_path_length(graph, weight=weight),
    }
    return calls[algorithm]()


def execute_networkx(graph, module, algorithm: str, params: dict):
    weight = params["weight"]
    calls = {
        "betweenness": lambda: module.betweenness_centrality(graph, weight=weight),
        "closeness": lambda: module.closeness_centrality(graph, distance=weight),
        "pagerank": lambda: module.pagerank(graph, alpha=params["alpha"], max_iter=params["max_iter"], tol=params["tolerance"], weight=weight),
        "eigenvector": lambda: module.eigenvector_centrality(graph, max_iter=params["max_iter"], tol=params["tolerance"], weight=weight),
        "katz": lambda: module.katz_centrality(graph, alpha=params["alpha"], beta=params["beta"], max_iter=params["max_iter"], tol=params["tolerance"], weight=weight),
        "constraint": lambda: module.constraint(graph, weight=weight),
        "effective_size": lambda: module.effective_size(graph, weight=weight),
        "eccentricity": lambda: module.eccentricity(graph),
        "average_shortest_path_length": lambda: module.average_shortest_path_length(graph, weight=weight),
    }
    if algorithm not in calls:
        raise UnsupportedAlgorithm(f"NetworkX does not provide a directly comparable {algorithm} implementation.")
    return calls[algorithm]()


def execute_igraph(graph, module, algorithm: str, params: dict):
    weights = graph.es["weight"] if params["weighted"] else None
    calls = {
        "betweenness": lambda: graph.betweenness(directed=graph.is_directed(), weights=weights),
        "closeness": lambda: graph.closeness(weights=weights, normalized=True),
        "pagerank": lambda: graph.pagerank(damping=params["alpha"], weights=weights),
        "eigenvector": lambda: graph.eigenvector_centrality(directed=graph.is_directed(), weights=weights),
        "constraint": lambda: graph.constraint(weights=weights),
        "eccentricity": lambda: graph.eccentricity(),
        "average_shortest_path_length": lambda: graph.average_path_length(directed=graph.is_directed(), unconn=False),
    }
    if algorithm not in calls:
        raise UnsupportedAlgorithm(f"igraph does not provide a directly comparable {algorithm} implementation.")
    return calls[algorithm]()


def execute_graph_tool(graph, module_bundle, algorithm: str, params: dict):
    gt, weight_property = module_bundle
    calls = {
        "betweenness": lambda: gt.betweenness(graph, weight=weight_property)[0],
        "closeness": lambda: gt.closeness(graph, weight=weight_property),
        "pagerank": lambda: gt.pagerank(graph, damping=params["alpha"], weight=weight_property),
        "eigenvector": lambda: gt.eigenvector(graph, weight=weight_property)[1],
    }
    if algorithm not in calls:
        raise UnsupportedAlgorithm(f"graph-tool benchmark adapter does not expose {algorithm}.")
    return calls[algorithm]()


def execute_networkit(graph, module, algorithm: str, params: dict):
    def scores(instance):
        instance.run()
        return instance.scores()

    def pagerank():
        instance = module.centrality.PageRank(
            graph,
            damp=params["alpha"],
            tol=params["tolerance"],
            normalized=False,
        )
        if hasattr(instance, "maxIterations"):
            instance.maxIterations = params["max_iter"]
        return scores(instance)

    calls = {
        "betweenness": lambda: scores(module.centrality.Betweenness(graph, normalized=True)),
        "closeness": lambda: scores(
            module.centrality.Closeness(
                graph,
                not params["weighted"],
                module.centrality.ClosenessVariant.GENERALIZED,
            )
        ),
        "pagerank": pagerank,
        "eigenvector": lambda: scores(
            module.centrality.EigenvectorCentrality(graph, tol=params["tolerance"])
        ),
        "katz": lambda: scores(
            module.centrality.KatzCentrality(
                graph,
                alpha=params["alpha"],
                beta=params["beta"],
                tol=params["tolerance"],
            )
        ),
        "eccentricity": lambda: [
            module.distance.Eccentricity.getValue(graph, node)[1]
            for node in range(graph.numberOfNodes())
        ],
    }
    if algorithm not in calls:
        raise UnsupportedAlgorithm(f"NetworKit does not provide a directly comparable {algorithm} implementation.")
    return calls[algorithm]()


def build_backend(backend, node_count, edges, directed, weighted):
    builders = {
        "easygraph": build_easygraph,
        "networkx": build_networkx,
        "igraph": build_igraph,
        "graph-tool": build_graph_tool,
        "networkit": build_networkit,
    }
    return builders[backend](node_count, edges, directed, weighted)


def execute_backend(backend, graph, module, algorithm, params):
    executors = {
        "easygraph": execute_easygraph,
        "networkx": execute_networkx,
        "igraph": execute_igraph,
        "graph-tool": execute_graph_tool,
        "networkit": execute_networkit,
    }
    return executors[backend](graph, module, algorithm, params)


def normalize_node_values(result, backend: str, graph, node_count: int) -> list[float]:
    if isinstance(result, dict):
        values = [result.get(index, math.nan) for index in range(node_count)]
    elif backend == "graph-tool" and hasattr(result, "__getitem__"):
        values = [result[graph.vertex(index)] for index in range(node_count)]
    elif hasattr(result, "tolist"):
        values = result.tolist()
    else:
        values = list(result)
    if len(values) != node_count:
        raise ValueError(f"Result length {len(values)} does not match graph size {node_count}.")
    return [float(value) if value is not None else math.nan for value in values]


def write_payload(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    os.environ["OMP_NUM_THREADS"] = str(max(1, args.threads))
    payload = {
        "status": "error",
        "backend": args.backend,
        "dataset": args.dataset,
        "algorithm": args.algorithm,
    }
    try:
        dataset = dataset_configuration(args)
        algorithm = ALGORITHMS[args.algorithm]
        raw_params = json.loads(args.params)
        params = normalized_params(raw_params, dataset)
        original_ids, original_edges = read_dataset(dataset, params["weighted"])
        mapped_edges = remap_edges(original_ids, original_edges)
        if algorithm.get("largest_component"):
            original_ids, mapped_edges = select_largest_component(original_ids, mapped_edges, dataset["directed"])

        graph, module = build_backend(args.backend, len(original_ids), mapped_edges, dataset["directed"], params["weighted"])
        for _ in range(max(0, args.warmup)):
            execute_backend(args.backend, graph, module, args.algorithm, params)

        run_times = []
        result = None
        for _ in range(max(1, args.runs)):
            started = time.perf_counter()
            result = execute_backend(args.backend, graph, module, args.algorithm, params)
            run_times.append(time.perf_counter() - started)

        payload.update(
            {
                "status": "completed",
                "runtime_seconds": statistics.mean(run_times),
                "std_seconds": statistics.pstdev(run_times) if len(run_times) > 1 else 0.0,
                "run_times": run_times,
                "nodes": len(original_ids),
                "edges": len(mapped_edges),
                "directed": dataset["directed"],
                "weighted": params["weighted"],
                "result_kind": algorithm["result_kind"],
                "result_label": algorithm["result_label"],
            }
        )

        if algorithm["result_kind"] == "scalar":
            payload["scalar"] = float(result)
        elif not args.summary_only:
            values = normalize_node_values(result, args.backend, graph, len(original_ids))
            payload["results"] = [
                {"node_id": original_ids[index], "mapped_id": index, "value": value}
                for index, value in enumerate(values)
                if math.isfinite(value)
            ]
        else:
            values = normalize_node_values(result, args.backend, graph, len(original_ids))
            payload["result_count"] = sum(math.isfinite(value) for value in values)

        write_payload(args.output, payload)
        return 0
    except UnsupportedAlgorithm as error:
        payload.update({"status": "unsupported", "error": str(error)})
        write_payload(args.output, payload)
        return 4
    except Exception as error:
        payload.update({"status": "error", "error": str(error), "traceback": traceback.format_exc()})
        write_payload(args.output, payload)
        print(traceback.format_exc(), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
