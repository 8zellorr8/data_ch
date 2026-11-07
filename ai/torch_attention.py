"""Keep Qwen GQA on memory-efficient CUDA SDPA with PyTorch 2.7/Windows."""

from functools import lru_cache
from importlib import import_module
from typing import Any


@lru_cache(maxsize=1)
def _libraries() -> tuple[Any, Any, Any]:
    return (
        import_module("torch"),
        import_module("torch.nn.attention"),
        import_module("transformers.integrations.sdpa_attention"),
    )


def efficient_sdpa(
    module: Any,
    query: Any,
    key: Any,
    value: Any,
    attention_mask: Any,
    dropout: float = 0.0,
    scaling: float | None = None,
    is_causal: bool | None = None,
    **kwargs: Any,
) -> tuple[Any, None]:
    _, attention, sdpa = _libraries()
    arguments = dict(dropout=dropout, scaling=scaling, is_causal=is_causal, **kwargs)
    if query.device.type == "cuda":
        # Native enable_gqa falls back to quadratic math attention on this stack.
        with attention.sdpa_kernel(
            [attention.SDPBackend.EFFICIENT_ATTENTION, attention.SDPBackend.FLASH_ATTENTION]
        ):
            result: tuple[Any, None] = sdpa.sdpa_attention_forward(
                module, query, key, value, attention_mask, **arguments
            )
    else:
        result = sdpa.sdpa_attention_forward(module, query, key, value, attention_mask, **arguments)
    return result


def _disable_native_gqa(*args: Any, **kwargs: Any) -> bool:
    return False


def enable_efficient_attention() -> None:
    _libraries()[2].use_gqa_in_sdpa = _disable_native_gqa
    import_module("transformers").AttentionInterface.register("sdpa", efficient_sdpa)
