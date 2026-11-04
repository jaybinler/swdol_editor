"""Hashable resource roots shared by every model, icon and appearance reader."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResourcePaths:
    """Route native resource namespaces to explicit read-only directories, without changing readers."""

    base: Path
    act: Path
    model: Path
    tex: Path
    tsw: Path

    def __truediv__(self, name: str) -> Path:
        """Resolve a native directory name; all returned paths remain input locations only."""
        return {'Act': self.act, 'MODEL': self.model, 'TEX': self.tex, 'Tsw': self.tsw}.get(name, self.base / name)


def clear_resource_caches() -> None:
    """Discard immutable decoded resources when validating or activating a changed data source."""
    from .models import actor_data, read_mesh, texture_png
    from .sprites import sprite_index, sprite_png
    for function in (actor_data, read_mesh, texture_png, sprite_index, sprite_png):
        function.cache_clear()
