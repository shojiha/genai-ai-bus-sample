#!/usr/bin/env node
import 'source-map-support/register';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { BusRouteStack } from '../lib/bus-route-stack';
import { type BusRouteParams, params } from '../parameter';

// Optional, git-ignored overrides (e.g. real source IPs): parameter.local.ts
function loadLocalParams(): Record<string, Partial<BusRouteParams>> {
  const file = path.join(__dirname, '..', 'parameter.local.ts');
  if (!fs.existsSync(file)) return {};
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return require(file).localParams ?? {};
}

const app = new cdk.App();
const envName: string = app.node.tryGetContext('env') ?? 'dev';
const base = params[envName];
if (!base) {
  throw new Error(`Unknown env "${envName}". Define it in parameter.ts`);
}
const p: BusRouteParams = { ...base, ...loadLocalParams()[envName] };

new BusRouteStack(app, `GenaiAiBus-${p.envName}`, {
  params: p,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: 'ap-northeast-1',
  },
});
