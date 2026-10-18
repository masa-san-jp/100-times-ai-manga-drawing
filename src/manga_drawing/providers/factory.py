from .gemini import GeminiProvider
from .mock import MockProvider


def create_provider(cfg, clock=None):
    if cfg.provider == "gemini":
        return GeminiProvider(cfg)
    return MockProvider(cfg) if clock is None else MockProvider(cfg, clock)
