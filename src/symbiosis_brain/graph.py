from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from symbiosis_brain.storage import Storage


DEFAULT_HUB_THRESHOLD = 20

# Known hub entity names (case-insensitive). Traversal terminates at these nodes
# unless `include_hubs=True` is passed. Add new hubs here when they emerge in
# field reports — keep this list curated, not auto-generated.
# Post-W3: entity names are canonical paths (no .md) or root-level basenames.
DEFAULT_HUB_BLOCKLIST: frozenset[str] = frozenset({
    "user/profile",
    "CRITICAL_FACTS",
    "wiki/claude-code",
    "ExampleProject",
    "Symbiosis Brain",
})


class GraphTraverser:
    def __init__(self, storage: Storage):
        self.storage = storage

    def traverse(
        self,
        start: str,
        max_depth: int = 1,
        *,
        hub_threshold: int = DEFAULT_HUB_THRESHOLD,
        hub_blocklist: frozenset[str] | set[str] | None = None,
        include_hubs: bool = False,
    ) -> dict:
        """BFS over relations starting at `start`, capped at `max_depth`.

        Hub filter (active when `include_hubs=False`): a neighbor classified as a hub
        (name in blocklist OR in-degree ≥ threshold) is included in `neighbors` with
        `is_hub=True` and its edge is kept, but the node is NOT expanded further.
        This prevents depth-2 fan-out through graph hubs like `Claude Code`.
        """
        if hub_blocklist is None:
            hub_blocklist = DEFAULT_HUB_BLOCKLIST
        # Normalize blocklist to lowercase for case-insensitive matching.
        blocklist_lc = {name.lower() for name in hub_blocklist}

        in_degree: dict[str, int] = (
            {} if include_hubs else self.storage.get_in_degree_map()
        )

        def is_hub(name: str) -> bool:
            if include_hubs:
                return False
            if name.lower() in blocklist_lc:
                return True
            return in_degree.get(name, 0) >= hub_threshold

        visited: set[str] = set()
        neighbors: list[dict] = []
        edges: list[dict] = []

        queue: list[tuple[str, int]] = [(start, 0)]
        visited.add(start)

        while queue:
            current, depth = queue.pop(0)
            if depth >= max_depth:
                continue

            for rel in self.storage.get_relations(current, direction="all"):
                other = rel["to_name"] if rel["from_name"] == current else rel["from_name"]
                edges.append({
                    "from": rel["from_name"],
                    "to": rel["to_name"],
                    "type": rel["relation_type"],
                    "depth": depth + 1,
                    "label": rel["label"] if "label" in rel.keys() else None,
                    "broken": bool(rel["broken"]) if "broken" in rel.keys() else False,
                })
                if other in visited:
                    continue
                visited.add(other)
                hub = is_hub(other)
                neighbors.append({
                    "name": other,
                    "depth": depth + 1,
                    "is_hub": hub,
                })
                # Expand only non-hub neighbors.
                if not hub:
                    queue.append((other, depth + 1))

        return {"start": start, "neighbors": neighbors, "edges": edges}
