class ScopeResolver:
    """Resolves scope chain: project scope includes global."""

    def __init__(self, scope: str | None):
        self.scope = scope

    def matches(self, other_scope: str) -> bool:
        if self.scope is None:
            return True
        if self.scope == "global":
            return other_scope == "global"
        return other_scope in (self.scope, "global")

    def sql_filter(self, column: str) -> tuple[str, list[str]]:
        if self.scope is None:
            return "1=1", []
        if self.scope == "global":
            return f"{column} = ?", ["global"]
        return f"{column} IN (?, ?)", [self.scope, "global"]

    @property
    def chain(self) -> list[str]:
        if self.scope is None:
            return []
        if self.scope == "global":
            return ["global"]
        return [self.scope, "global"]
