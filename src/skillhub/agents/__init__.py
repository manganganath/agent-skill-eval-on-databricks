"""External agents: harnesses that run each eval case themselves and send an OpenTelemetry trace to Databricks.

`contract` is what the eval Job hands an agent and what it gets back. `traces` finds the traces that agents sent in a
department's trace table and reads them. `cases` runs a child run's cases on an agent and scores the traces. Each
harness has its own module: `claude_code`, a CLI that the Job runs, and `agentcore`, an agent on Amazon Bedrock
AgentCore that the Job invokes.
"""
