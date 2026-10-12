"""Framework adapters: a missing optional extra raises a clear install hint.

The adapters are thin wiring over OpenInference instrumentors that ship as optional
extras (``husk-studio-shared[openai]`` etc.). Core husk-studio-shared never imports them, so the
adapter must fail with an actionable ImportError rather than a cryptic one. We do
not test the instrumentor's own span emission — that's the instrumentor's job.
"""

from __future__ import annotations

import importlib.util

import pytest

from husk_studio_shared import (
    instrument_anthropic,
    instrument_langgraph,
    instrument_llamaindex,
    instrument_openai,
)


def _extra_installed(*module_names: str) -> bool:
    # find_spec raises ModuleNotFoundError (not None) when a *parent* package is
    # missing — e.g. no `openinference` at all — so treat that as not installed.
    try:
        return all(importlib.util.find_spec(m) is not None for m in module_names)
    except ModuleNotFoundError:
        return False


@pytest.mark.parametrize(
    ("func", "instrumentor_module"),
    [
        (instrument_openai, "openinference.instrumentation.openai"),
        (instrument_anthropic, "openinference.instrumentation.anthropic"),
        (instrument_langgraph, "openinference.instrumentation.langchain"),
        (instrument_llamaindex, "openinference.instrumentation.llama_index"),
    ],
)
def test_adapter_without_extra_raises_install_hint(func, instrumentor_module) -> None:  # type: ignore[no-untyped-def]
    if _extra_installed("opentelemetry", instrumentor_module):
        pytest.skip("optional extra installed; the missing-dependency path isn't exercised")
    with pytest.raises(ImportError) as ei:
        func()
    # Either the OTel-SDK hint or the husk-studio-shared[...] hint — both tell the user to pip install.
    assert "pip install" in str(ei.value)
