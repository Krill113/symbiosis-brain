from symbiosis_brain.scopes import ScopeResolver


class TestScopeResolver:
    def test_global_scope_includes_everything(self):
        resolver = ScopeResolver("global")
        assert resolver.matches("global")
        assert not resolver.matches("beta")

    def test_project_scope_includes_global(self):
        resolver = ScopeResolver("beta")
        assert resolver.matches("beta")
        assert resolver.matches("global")
        assert not resolver.matches("widgetcompare")

    def test_scope_filter_sql(self):
        resolver = ScopeResolver("beta")
        clause, params = resolver.sql_filter("scope")
        assert "scope" in clause
        assert "beta" in params
        assert "global" in params

    def test_none_scope_returns_all(self):
        resolver = ScopeResolver(None)
        clause, params = resolver.sql_filter("scope")
        assert clause == "1=1"
        assert params == []
