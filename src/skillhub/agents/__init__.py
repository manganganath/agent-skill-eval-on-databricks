"""External agents: harnesses that run each eval case themselves and send an OpenTelemetry trace to Databricks.

`contract` is what the eval Job hands an agent and what it gets back. `traces` finds the traces that agents sent in a
department's trace table and reads them. `cases` runs a child run's cases on an agent and scores the traces. Each
harness has its own module: `claude_code`, a CLI that the Job runs, and `agentcore`, an agent on Amazon Bedrock
AgentCore that the Job invokes.
"""

from __future__ import annotations

import importlib

from skillhub.config import ConfigError, HubConfig

# harness -> "module:Class". Each class builds itself with `from_profile(cfg, name, profile)`.
HARNESSES: dict[str, str] = {
    "claude-code": "skillhub.agents.claude_code:ClaudeCodeAgent",
    "agentcore": "skillhub.agents.agentcore:AgentCoreAgent",
}


def agent_for(cfg: HubConfig, profile_name: str):
    """The external agent of a model catalog profile. A ConfigError when its harness cannot run here: the runner then
    records the profile as one it cannot serve, as it does for a model."""
    profile = cfg.models[profile_name]
    target = HARNESSES.get(profile.harness)
    if target is None:
        raise ConfigError(f"the {profile.harness} harness is not supported")
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name).from_profile(cfg, profile_name, profile)
