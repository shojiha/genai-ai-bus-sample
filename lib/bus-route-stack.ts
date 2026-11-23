import * as path from 'node:path';
import {
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  type StackProps,
  aws_apigateway as apigw,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_s3 as s3,
  aws_wafv2 as wafv2,
} from 'aws-cdk-lib';
import type { Construct } from 'constructs';
import type { BusRouteParams } from '../parameter';

export interface BusRouteStackProps extends StackProps {
  params: BusRouteParams;
}

/**
 * Bus route search ExApp for 源内 Web (synchronous).
 * API Gateway (x-api-key, WAF IP allow-list) -> Lambda (Python) -> S3 timetable / Bedrock / Location Service
 */
export class BusRouteStack extends Stack {
  constructor(scope: Construct, id: string, props: BusRouteStackProps) {
    super(scope, id, props);
    const p = props.params;

    // ---- Timetable data -------------------------------------------------
    const dataBucket = new s3.Bucket(this, 'DataBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // ---- Lambda ---------------------------------------------------------
    const logGroup = new logs.LogGroup(this, 'FunctionLogs', {
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const fn = new lambda.Function(this, 'BusRouteFunction', {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.lambda_handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda', 'bus_route'), {
        exclude: ['__pycache__', '*.pyc'],
      }),
      memorySize: 1769, // 1 vCPU: CSA and index parsing are CPU bound
      timeout: Duration.seconds(28), // 源内 Web cuts synchronous calls at 29 s
      logGroup,
      environment: {
        DATA_BUCKET: dataBucket.bucketName,
        INDEX_KEY: p.indexKey,
        REGION_NAME: p.regionName,
        BIAS_POSITION: p.biasPosition ? p.biasPosition.join(',') : '',
        PARSE_MODEL_ID: p.parseModelId,
        ANSWER_MODEL_ID: p.answerModelId,
        MODEL_REGION: this.region,
        LOG_LEVEL: p.logLevel,
      },
    });
    dataBucket.grantRead(fn);

    // Bedrock: jp.* cross-region inference profiles route to ap-northeast-1 / ap-northeast-3
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/jp.anthropic.*`,
          'arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-*',
          'arn:aws:bedrock:ap-northeast-3::foundation-model/anthropic.claude-*',
        ],
      }),
    );
    // Amazon Location Service Places API v2 (no resource to create)
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['geo-places:Geocode'],
        resources: [`arn:aws:geo-places:${this.region}::provider/default`],
      }),
    );

    // ---- API Gateway ----------------------------------------------------
    const accessLogs = new logs.LogGroup(this, 'ApiAccessLogs', {
      retention: logs.RetentionDays.THREE_MONTHS,
      removalPolicy: RemovalPolicy.DESTROY,
    });
    const api = new apigw.RestApi(this, 'BusRouteApi', {
      restApiName: `genai-ai-bus-${p.envName}`,
      endpointTypes: [apigw.EndpointType.REGIONAL],
      deployOptions: {
        stageName: 'prod',
        throttlingRateLimit: 10,
        throttlingBurstLimit: 20,
        accessLogDestination: new apigw.LogGroupLogDestination(accessLogs),
        accessLogFormat: apigw.AccessLogFormat.jsonWithStandardFields(),
      },
    });
    api.root.addResource('invoke').addMethod(
      'POST',
      new apigw.LambdaIntegration(fn, { timeout: Duration.seconds(29) }),
      { apiKeyRequired: true },
    );

    const apiKey = api.addApiKey('ApiKey', { apiKeyName: `genai-ai-bus-${p.envName}` });
    const plan = api.addUsagePlan('UsagePlan', {
      name: `genai-ai-bus-${p.envName}`,
      throttle: { rateLimit: 10, burstLimit: 20 },
      quota: { limit: 10000, period: apigw.Period.DAY },
    });
    plan.addApiKey(apiKey);
    plan.addApiStage({ stage: api.deploymentStage });

    // ---- WAF: allow only 源内 Web's source IPs -----------------------------
    const ipSet = new wafv2.CfnIPSet(this, 'AllowedIps', {
      scope: 'REGIONAL',
      ipAddressVersion: 'IPV4',
      addresses: p.allowedIpV4AddressRanges,
    });
    const webAcl = new wafv2.CfnWebACL(this, 'WebAcl', {
      scope: 'REGIONAL',
      defaultAction: { block: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `genai-ai-bus-${p.envName}`,
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: 'AllowGenaiWeb',
          priority: 0,
          action: { allow: {} },
          statement: { ipSetReferenceStatement: { arn: ipSet.attrArn } },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'AllowGenaiWeb',
            sampledRequestsEnabled: true,
          },
        },
      ],
    });
    new wafv2.CfnWebACLAssociation(this, 'WebAclAssociation', {
      resourceArn: api.deploymentStage.stageArn,
      webAclArn: webAcl.attrArn,
    });

    // ---- Outputs ----------------------------------------------------------
    new CfnOutput(this, 'InvokeUrl', { value: `${api.url}invoke` });
    new CfnOutput(this, 'ApiHostname', { value: `${api.restApiId}.execute-api.${this.region}.amazonaws.com` });
    new CfnOutput(this, 'ApiKeyValueCommand', {
      value: `aws apigateway get-api-key --api-key ${apiKey.keyId} --include-value --query value --output text --region ${this.region}`,
    });
    new CfnOutput(this, 'DataBucketName', { value: dataBucket.bucketName });
    new CfnOutput(this, 'UploadIndexCommand', {
      value: `aws s3 cp index.json.gz s3://${dataBucket.bucketName}/${p.indexKey}`,
    });
  }
}
