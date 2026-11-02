import { AgentCoreApplication, type AgentCoreApplicationProps } from '@aws/agentcore-cdk';
import { Stack, Tags, aws_bedrockagentcore as bedrockagentcore, aws_secretsmanager as secretsmanager, type StackProps } from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface AgentCoreStackProps extends StackProps {
  /** Props for the AgentCore application, produced by transformAgentCoreJson from agentcore/agentcore.json. */
  application: AgentCoreApplicationProps;
}

/**
 * The deployment's own settings, which agentcore.json does not hold: deploy.sh checks them and passes them in the
 * environment of `agentcore deploy`.
 */
interface Settings {
  databricksHost: string;
  clientId: string;
  secretArn: string;
  subnets: string[];
  securityGroups: string[];
  tags: Record<string, string>;
}

function required(env: NodeJS.ProcessEnv, name: string): string {
  const value = (env[name] ?? '').trim();
  if (!value) {
    throw new Error(`set ${name}: deploy.sh passes it to agentcore deploy`);
  }
  return value;
}

function list(env: NodeJS.ProcessEnv, name: string): string[] {
  return (env[name] ?? '').split(',').map((s) => s.trim()).filter((s) => s.length > 0);
}

function settings(env: NodeJS.ProcessEnv): Settings {
  const tags: Record<string, string> = {};
  for (const item of list(env, 'AWS_TAGS')) {
    const at = item.indexOf('=');
    tags[item.slice(0, at).trim()] = item.slice(at + 1).trim();
  }
  return {
    databricksHost: required(env, 'DATABRICKS_HOST').replace(/\/+$/, ''),
    clientId: required(env, 'DATABRICKS_CLIENT_ID'),
    secretArn: required(env, 'SKILLHUB_AGENT_SECRET_ARN'),
    subnets: list(env, 'AGENT_SUBNETS'),
    securityGroups: list(env, 'AGENT_SECURITY_GROUPS'),
    tags,
  };
}

/**
 * The stack the AgentCore CLI deploys: the runtime that agentcore/agentcore.json declares, with
 * - the Databricks settings of the agent, as its environment variables;
 * - read access to the one secret that holds its Databricks OAuth secret, and to nothing else in Secrets Manager;
 * - VPC mode in the given subnets, when AGENT_SUBNETS is set, for a workspace that allows only known IP addresses;
 * - project=skillhub and the tags in AWS_TAGS, on every resource of the stack.
 */
export class AgentCoreStack extends Stack {
  public readonly application: AgentCoreApplication;

  constructor(scope: Construct, id: string, props: AgentCoreStackProps) {
    super(scope, id, props);
    const s = settings(process.env);
    this.application = new AgentCoreApplication(this, 'Application', props.application);

    const agent = this.application.runtime('skill_agent');
    agent
      .addEnvironmentVariable('DATABRICKS_HOST', s.databricksHost)
      .addEnvironmentVariable('DATABRICKS_CLIENT_ID', s.clientId)
      .addEnvironmentVariable('SKILLHUB_AGENT_SECRET_ARN', s.secretArn);
    secretsmanager.Secret.fromSecretCompleteArn(this, 'AgentSecret', s.secretArn).grantRead(agent);

    if (s.subnets.length > 0) {
      const runtime = agent.resource.node.defaultChild as bedrockagentcore.CfnRuntime;
      runtime.networkConfiguration = {
        networkMode: 'VPC',
        networkModeConfig: { subnets: s.subnets, securityGroups: s.securityGroups },
      };
    }
    for (const [key, value] of Object.entries({ project: 'skillhub', ...s.tags })) {
      Tags.of(this).add(key, value);
    }
  }
}
