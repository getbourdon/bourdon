"""
Bourdon core — reference implementation of the L0-L6 memory stack.

Public API:
    Bourdon: the main memory orchestrator class

Example:
    from core import Bourdon

    memory = Bourdon()
    system_prompt = await memory.prepare(user_message, base_instructions)
"""

__all__ = ["Bourdon"]
__version__ = "0.0.1"


def __getattr__(name):  # PEP 562 — lazy, so `import core.<submodule>` stays light
    """Defer the orchestrator import until someone actually asks for it.

    An eager ``from core.orchestrator import Bourdon`` here made importing ANY
    core submodule pull yaml + asyncio (~145 modules). The presence hook
    entrypoint (``python -m core.presence``) runs on every user prompt and must
    import only its own module — and must not be crashable by an unrelated
    orchestrator import failure before its always-exit-0 guard is in place.
    """
    if name == "Bourdon":
        from core.orchestrator import Bourdon

        return Bourdon
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
