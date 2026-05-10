"""Tests for Provider ABC + ProviderResponse dataclass (F7)."""

from __future__ import annotations

import pytest

from bijotel.adapters.base import Provider, ProviderResponse


def test_provider_response_frozen() -> None:
    """ProviderResponse is frozen — cannot mutate fields."""
    r = ProviderResponse(
        text="hi",
        model="claude-haiku-4-5-20251001",
        input_tokens=10,
        output_tokens=5,
    )
    # FrozenInstanceError is a subclass of AttributeError
    with pytest.raises(AttributeError):
        r.text = "mutated"  # type: ignore[misc]


def test_provider_response_extra_attrs_default_independent_dict() -> None:
    """Each ProviderResponse gets its own extra_attrs dict (no shared mutable state)."""
    a = ProviderResponse(text="a", model="m", input_tokens=0, output_tokens=0)
    b = ProviderResponse(text="b", model="m", input_tokens=0, output_tokens=0)
    # field(default_factory=dict) ensures fresh dict per instance
    assert a.extra_attrs is not b.extra_attrs


def test_provider_abc_cannot_instantiate() -> None:
    """Provider() directly raises TypeError (4 abstract methods)."""
    with pytest.raises(TypeError, match="abstract"):
        Provider()  # type: ignore[abstract]


def test_subclass_must_implement_all_methods() -> None:
    """Partial implementation must fail TypeError on instantiate."""

    class IncompleteProvider(Provider):
        @property
        def name(self) -> str:
            return "incomplete"

        # Missing extract_request_attrs, extract_response_attrs, complete

    with pytest.raises(TypeError, match="abstract"):
        IncompleteProvider()  # type: ignore[abstract]


def test_subclass_full_implementation_works() -> None:
    """All 4 abstract members provided → instantiation works."""

    class MinimalProvider(Provider):
        @property
        def name(self) -> str:
            return "minimal"

        def extract_request_attrs(self, kwargs: dict) -> dict:
            return {}

        def extract_response_attrs(self, response: object) -> dict:
            return {}

        async def complete(
            self, *, messages: list, model: str, max_tokens: int, **kwargs: object
        ) -> ProviderResponse:
            return ProviderResponse(
                text="ok", model=model, input_tokens=0, output_tokens=0
            )

    p = MinimalProvider()
    assert p.name == "minimal"
    assert isinstance(p, Provider)


def test_provider_response_optional_fields_defaults() -> None:
    """response_id, finish_reason default to None; raw_response None; extra_attrs {}."""
    r = ProviderResponse(text="x", model="m", input_tokens=1, output_tokens=2)
    assert r.response_id is None
    assert r.finish_reason is None
    assert r.raw_response is None
    assert r.extra_attrs == {}
