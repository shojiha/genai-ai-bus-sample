/**
 * Deployment parameters per environment.
 * Select one with `-c env=<name>` (default: dev).
 */
export interface BusRouteParams {
  /** Suffix for stack and resource names */
  envName: string;
  /** Region shown in prompts/answers, e.g. "〇〇市" */
  regionName: string;
  /** Bias for Amazon Location Service geocoding: [longitude, latitude] of the area centre */
  biasPosition: [number, number] | null;
  /** S3 key of the timetable index built by tools/build_index.py */
  indexKey: string;
  /** Bedrock model for inference 1 (reading search conditions). Fast model recommended. */
  parseModelId: string;
  /** Bedrock model for inference 2 (writing the answer) */
  answerModelId: string;
  /** Source IPs allowed through WAF (源内 Web's NAT gateways), CIDR */
  allowedIpV4AddressRanges: string[];
  logLevel: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';
}

export const params: Record<string, BusRouteParams> = {
  dev: {
    envName: 'dev',
    regionName: '宮崎県都農町（都農町コミュニティバス）',
    biasPosition: [131.56, 32.256], // 都農町役場・JR 都農駅付近 [経度, 緯度]
    indexKey: 'timetable/index.json.gz',
    parseModelId: 'jp.anthropic.claude-haiku-4-5-20251001-v1:0',
    answerModelId: 'jp.anthropic.claude-sonnet-4-6',
    // 源内 Web の送信元 IP（TeamAccessControl スタックの NAT ゲートウェイの EIP 2 つ）。
    // 下は例の値です。実際の値は parameter.local.ts（git 管理外）に書いてください。
    allowedIpV4AddressRanges: ['203.0.113.10/32', '203.0.113.11/32'],
    logLevel: 'INFO',
  },
};
