from pathlib import Path
from symbiosis_brain.storage import Storage
from symbiosis_brain.graph import GraphTraverser


def _seed_graph(storage: Storage):
    """Create a small test graph: beta → Dapper → SQL, beta → WPF"""
    storage.upsert_entity("beta", "project")
    storage.upsert_entity("Dapper", "technology")
    storage.upsert_entity("SQL", "concept")
    storage.upsert_entity("WPF", "technology")
    storage.upsert_entity("MVVM", "pattern")
    storage.upsert_relation("beta", "Dapper", "uses")
    storage.upsert_relation("beta", "WPF", "uses")
    storage.upsert_relation("Dapper", "SQL", "requires")
    storage.upsert_relation("WPF", "MVVM", "implements")


class TestGraphTraverser:
    def test_depth_1(self, db_path: Path):
        storage = Storage(db_path)
        _seed_graph(storage)
        graph = GraphTraverser(storage)
        result = graph.traverse("beta", max_depth=1)
        neighbor_names = [n["name"] for n in result["neighbors"]]
        assert "Dapper" in neighbor_names
        assert "WPF" in neighbor_names
        assert "SQL" not in neighbor_names

    def test_depth_2(self, db_path: Path):
        storage = Storage(db_path)
        _seed_graph(storage)
        graph = GraphTraverser(storage)
        result = graph.traverse("beta", max_depth=2)
        all_names = [n["name"] for n in result["neighbors"]]
        assert "SQL" in all_names
        assert "MVVM" in all_names

    def test_nonexistent_entity(self, db_path: Path):
        storage = Storage(db_path)
        graph = GraphTraverser(storage)
        result = graph.traverse("nonexistent", max_depth=1)
        assert result["neighbors"] == []


def _seed_hub_graph(storage: Storage):
    """Graph: Start connects to Hub; Hub has many back-references (high in-degree).
    Also: Start → Normal → Leaf (normal chain, no hub interference).
    """
    for name in ("Start", "Hub", "Normal", "Leaf", "A", "B", "C", "D", "E"):
        storage.upsert_entity(name, "concept")
    # Start → Hub (direct edge)
    storage.upsert_relation("Start", "Hub", "uses")
    # Normal chain: Start → Normal → Leaf
    storage.upsert_relation("Start", "Normal", "uses")
    storage.upsert_relation("Normal", "Leaf", "uses")
    # Hub has 5 incoming edges total (Start + A..D) → in-degree 5
    for src in ("A", "B", "C", "D"):
        storage.upsert_relation(src, "Hub", "uses")
    # Hub fans out to many nodes — would leak at depth 2 without filter
    storage.upsert_relation("Hub", "E", "contains")


class TestHubFilter:
    def test_blocklist_stops_expansion_through_hub(self, db_path: Path):
        storage = Storage(db_path)
        _seed_hub_graph(storage)
        graph = GraphTraverser(storage)
        result = graph.traverse(
            "Start", max_depth=2,
            hub_blocklist={"hub"},  # case-insensitive
            hub_threshold=999,  # effectively disable threshold
        )
        names = [n["name"] for n in result["neighbors"]]
        # Hub reachable as a terminal
        assert "Hub" in names
        # But E (reached only via Hub) is filtered out
        assert "E" not in names
        # Normal chain untouched
        assert "Normal" in names
        assert "Leaf" in names

    def test_threshold_stops_expansion_through_high_in_degree(self, db_path: Path):
        storage = Storage(db_path)
        _seed_hub_graph(storage)
        graph = GraphTraverser(storage)
        result = graph.traverse(
            "Start", max_depth=2,
            hub_blocklist=set(),
            hub_threshold=3,  # Hub has in-degree 5, exceeds threshold
        )
        names = [n["name"] for n in result["neighbors"]]
        assert "Hub" in names
        assert "E" not in names  # blocked by threshold
        assert "Leaf" in names   # reached via Normal (in-degree 1)

    def test_include_hubs_bypasses_filter(self, db_path: Path):
        storage = Storage(db_path)
        _seed_hub_graph(storage)
        graph = GraphTraverser(storage)
        result = graph.traverse(
            "Start", max_depth=2,
            hub_blocklist={"hub"},
            hub_threshold=3,
            include_hubs=True,
        )
        names = [n["name"] for n in result["neighbors"]]
        assert "E" in names  # traversal went through Hub

    def test_hub_neighbor_marked_is_hub(self, db_path: Path):
        storage = Storage(db_path)
        _seed_hub_graph(storage)
        graph = GraphTraverser(storage)
        result = graph.traverse(
            "Start", max_depth=2,
            hub_blocklist={"hub"},
            hub_threshold=999,
        )
        hub_entry = next(n for n in result["neighbors"] if n["name"] == "Hub")
        assert hub_entry["is_hub"] is True
        normal_entry = next(n for n in result["neighbors"] if n["name"] == "Normal")
        assert normal_entry["is_hub"] is False

    def test_edge_to_hub_still_present(self, db_path: Path):
        """Even filtered hubs keep their incoming edge in the output, so the caller
        can see that a connection exists — just not walk through it."""
        storage = Storage(db_path)
        _seed_hub_graph(storage)
        graph = GraphTraverser(storage)
        result = graph.traverse(
            "Start", max_depth=2,
            hub_blocklist={"hub"},
            hub_threshold=999,
        )
        edge_pairs = [(e["from"], e["to"]) for e in result["edges"]]
        assert ("Start", "Hub") in edge_pairs

    def test_default_arguments_apply_blocklist_and_threshold(self, db_path: Path):
        """Calling traverse() without explicit hub_* args must still filter known hubs."""
        storage = Storage(db_path)
        for name in ("X", "Claude Code", "Y"):
            storage.upsert_entity(name, "concept")
        storage.upsert_relation("X", "Claude Code", "uses")
        storage.upsert_relation("Claude Code", "Y", "contains")
        # Create incoming edges to exceed default threshold (20)
        for i in range(25):
            src_name = f"Src{i}"
            storage.upsert_entity(src_name, "concept")
            storage.upsert_relation(src_name, "Claude Code", "uses")

        graph = GraphTraverser(storage)
        result = graph.traverse("X", max_depth=2)  # no hub args
        names = [n["name"] for n in result["neighbors"]]
        assert "Claude Code" in names
        assert "Y" not in names  # blocked by default filter


class TestBrokenAndLabelPropagation:
    def test_broken_edge_marked(self, db_path: Path):
        from symbiosis_brain.graph import GraphTraverser
        from symbiosis_brain.storage import Storage

        storage = Storage(db_path)
        storage.upsert_entity(name="projects/src")
        storage.upsert_entity(name="broken:nonexistent")
        storage.upsert_relation(
            from_name="projects/src",
            to_name="broken:nonexistent",
            relation_type="references",
            source_note="projects/src.md",
            raw_target="nonexistent",
            broken=True,
        )

        result = GraphTraverser(storage).traverse("projects/src", max_depth=1)
        edges = result["edges"]
        assert len(edges) == 1
        assert edges[0]["broken"] is True

    def test_label_propagated(self, db_path: Path):
        from symbiosis_brain.graph import GraphTraverser
        from symbiosis_brain.storage import Storage

        storage = Storage(db_path)
        storage.upsert_entity(name="projects/src")
        storage.upsert_entity(name="projects/foo")
        storage.upsert_relation(
            from_name="projects/src",
            to_name="projects/foo",
            relation_type="references",
            source_note="projects/src.md",
            label="Foo Alias",
            raw_target="projects/foo|Foo Alias",
        )

        result = GraphTraverser(storage).traverse("projects/src", max_depth=1)
        assert result["edges"][0]["label"] == "Foo Alias"
