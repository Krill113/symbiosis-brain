import asyncio


def test_brain_write_accepts_reference_type():
    """`reference` is documented in SKILL.md brain-save and used in vault; enum must allow it."""
    from symbiosis_brain import server
    tools = asyncio.run(server.list_tools())
    brain_write = next(t for t in tools if t.name == "brain_write")
    enum_values = brain_write.inputSchema["properties"]["note_type"]["enum"]
    assert "reference" in enum_values, (
        f"`reference` missing from note_type enum; got {enum_values}"
    )
