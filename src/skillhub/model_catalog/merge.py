"""The eval runner's model profiles: the hub.yaml `models` plus the model catalog rows a request names.

hub.yaml wins on a name clash, though registration already refuses one. Only enabled rows of a kind the runner runs
(models on Unity Gateway and external models) are merged. The catalog is read only when a request names a profile
that hub.yaml does not have, so a run on hub.yaml models, which every gate reads, never depends on the catalog.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from skillhub import SkillHubError
from skillhub.config import HubConfig, ModelProfile
from skillhub.model_catalog.entry import CatalogEntry
from skillhub.sql import SqlExecutor


class CatalogError(SkillHubError, ValueError):
    """A request names a model that is in neither hub.yaml nor the model catalog, or one that is disabled or cannot
    run, or the catalog cannot be read."""


def with_catalog(
    cfg: HubConfig,
    entries: Iterable[CatalogEntry],
    version: int | None,
    *,
    only: Iterable[str] | None = None,
    include_disabled: bool = False,
) -> HubConfig:
    """A copy of `cfg` whose `models` also hold the catalog rows: the enabled ones (every one with `include_disabled`,
    for a connection test), narrowed to `only` when it is given. The copy goes to the eval worker processes as it is."""
    wanted = set(only) if only is not None else None
    add: dict[str, ModelProfile] = {}
    for e in entries:
        if e.name in cfg.models or e.name in add or not e.runnable:
            continue
        if (wanted is not None and e.name not in wanted) or not (e.enabled or include_disabled):
            continue
        add[e.name] = e.to_profile(version)
    return cfg.model_copy(update={"models": {**cfg.models, **add}})


def resolve(
    cfg: HubConfig, entries: list[CatalogEntry], version: int | None, names: Iterable[str]
) -> HubConfig:
    """`cfg` with the catalog profiles that `names` asks for. CatalogError names each one that cannot run."""
    known = {e.name: e for e in entries}
    wanted = [n for n in dict.fromkeys(names) if n not in cfg.models]
    unknown = [n for n in wanted if n not in known]
    disabled = [n for n in wanted if n in known and known[n].runnable and not known[n].enabled]
    agents = [n for n in wanted if n in known and not known[n].runnable]
    reasons = []
    if unknown:
        reasons.append(f"unknown model profile(s): {', '.join(unknown)}")
    if disabled:
        reasons.append(f"disabled in the model catalog: {', '.join(disabled)}")
    if agents:
        reasons.append(f"not a model this release can run: {', '.join(agents)}")
    if reasons:
        raise CatalogError("; ".join(reasons))
    return with_catalog(cfg, entries, version, only=wanted)


def load_requested(cfg: HubConfig, connect: Callable[[], SqlExecutor], names: Iterable[str]) -> HubConfig:
    """`cfg` itself when every name is a hub.yaml profile. Else `connect()` gives the SQL client, and the result is
    `cfg` with the requested catalog profiles."""
    names = list(names)
    if all(n in cfg.models for n in names):
        return cfg
    from skillhub.model_catalog.store import read_entries

    try:
        entries, version = read_entries(connect(), cfg)
    except Exception as exc:  # the request names a catalog model, so the run cannot go on without the catalog
        raise CatalogError(f"the model catalog cannot be read ({type(exc).__name__})") from None
    return resolve(cfg, entries, version, names)
