#!/bin/sh
# Create or update the AWS Lambda compute node for fast-compute.
#
# Needs: aws CLI v2 (signed in as an AWS admin), docker with buildx. Run from anywhere:
#   REGION=eu-west-2 sh lambda-node/deploy.sh
# Settings (environment variables, all optional except REGION):
#   REGION        AWS region (required)
#   FUNCTION      function name              (default fast-compute-job)
#   BUCKET        S3 bucket for job files    (default fast-compute-jobs-<account>-<region>)
#   MEMORY_MB     memory per job, 128..10240 (default 2048 = ~1.16 vCPU; 1769 MB = 1 vCPU)
#   TIMEOUT       function timeout s, <=900  (default 900; longest job = TIMEOUT - 30)
#   NETWORK       none | internet            (default none = VPC without internet access)
#   CONCURRENCY   reserved concurrency       (default 10; also the node's max_concurrency)
#   ARCH          x86_64 | arm64             (default x86_64)
#   SERVER_USER   IAM user the server uses   (default fast-compute-server)
# It is safe to run again: existing resources are reused and the code/config updated.
# Nothing here writes credentials anywhere; create the server's access key yourself (printed
# at the end) and put it only in server/.env.
set -eu
: "${REGION:?set REGION, e.g. REGION=eu-west-2}"
FUNCTION=${FUNCTION:-fast-compute-job}
MEMORY_MB=${MEMORY_MB:-2048}
TIMEOUT=${TIMEOUT:-900}
NETWORK=${NETWORK:-none}
CONCURRENCY=${CONCURRENCY:-10}
ARCH=${ARCH:-x86_64}
SERVER_USER=${SERVER_USER:-fast-compute-server}
HERE=$(cd "$(dirname "$0")" && pwd)
export AWS_REGION="$REGION" AWS_PAGER=""
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET=${BUCKET:-fast-compute-jobs-$ACCOUNT-$REGION}
REPO=$FUNCTION
ROLE=$FUNCTION-exec
case $NETWORK in none|internet) ;; *) echo "NETWORK must be none or internet" >&2; exit 2;; esac
case $ARCH in x86_64) PLATFORM=linux/amd64;; arm64) PLATFORM=linux/arm64;; *) echo "bad ARCH" >&2; exit 2;; esac
say() { printf '\n== %s\n' "$*"; }

say "S3 bucket $BUCKET (private, objects expire after 2 days)"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [ "$REGION" = us-east-1 ]; then aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  else aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null; fi
fi
aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
  '{"Rules":[{"ID":"expire-job-files","Status":"Enabled","Filter":{"Prefix":"fast-compute/"},"Expiration":{"Days":2},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":1}}]}'

say "Image -> ECR repository $REPO"
aws ecr describe-repositories --repository-names "$REPO" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$REPO" --image-scanning-configuration scanOnPush=true >/dev/null
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
TAG=$(date -u +%Y%m%d%H%M%S)
docker buildx build --platform "$PLATFORM" --provenance=false -t "$REGISTRY/$REPO:$TAG" --push "$HERE"
IMAGE_URI=$REGISTRY/$REPO:$TAG

say "Execution role $ROLE (no permissions in 'internet' mode; only VPC networking in 'none' mode)"
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" >/dev/null
  sleep 10   # IAM propagation
fi
ROLE_ARN=$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)
VPC_ARGS=""
if [ "$NETWORK" = none ]; then
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
  say "Isolated VPC (no internet gateway, no NAT; S3 reachable through a gateway endpoint only)"
  VPC=$(aws ec2 describe-vpcs --filters Name=tag:Name,Values="$FUNCTION" --query 'Vpcs[0].VpcId' --output text)
  if [ "$VPC" = None ]; then
    VPC=$(aws ec2 create-vpc --cidr-block 10.77.0.0/16 --query Vpc.VpcId --output text)
    aws ec2 create-tags --resources "$VPC" --tags Key=Name,Value="$FUNCTION"
  fi
  SUBNET=$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC" --query 'Subnets[0].SubnetId' --output text)
  if [ "$SUBNET" = None ]; then
    AZ=$(aws ec2 describe-availability-zones --query 'AvailabilityZones[0].ZoneName' --output text)
    SUBNET=$(aws ec2 create-subnet --vpc-id "$VPC" --cidr-block 10.77.0.0/20 --availability-zone "$AZ" \
             --query Subnet.SubnetId --output text)
  fi
  RTB=$(aws ec2 describe-route-tables --filters Name=vpc-id,Values="$VPC" Name=association.main,Values=true \
        --query 'RouteTables[0].RouteTableId' --output text)
  EP=$(aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values="$VPC" Name=service-name,Values="com.amazonaws.$REGION.s3" \
       --query 'VpcEndpoints[0].VpcEndpointId' --output text)
  EP_POLICY=$(printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::%s/fast-compute/*"}]}' "$BUCKET")
  if [ "$EP" = None ]; then
    aws ec2 create-vpc-endpoint --vpc-id "$VPC" --service-name "com.amazonaws.$REGION.s3" \
      --route-table-ids "$RTB" --policy-document "$EP_POLICY" >/dev/null
  else
    aws ec2 modify-vpc-endpoint --vpc-endpoint-id "$EP" --policy-document "$EP_POLICY" >/dev/null
  fi
  SG=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values="$VPC" Name=group-name,Values=default \
       --query 'SecurityGroups[0].GroupId' --output text)
  VPC_ARGS="SubnetIds=$SUBNET,SecurityGroupIds=$SG"
else
  aws iam detach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole 2>/dev/null || true
fi

say "Function $FUNCTION ($MEMORY_MB MB, ${TIMEOUT}s, network=$NETWORK, $ARCH)"
if aws lambda get-function --function-name "$FUNCTION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNCTION" --image-uri "$IMAGE_URI" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FUNCTION"
  if [ -n "$VPC_ARGS" ]; then VPC_CFG="$VPC_ARGS"; else VPC_CFG="SubnetIds=[],SecurityGroupIds=[]"; fi
  aws lambda update-function-configuration --function-name "$FUNCTION" --role "$ROLE_ARN" \
    --memory-size "$MEMORY_MB" --timeout "$TIMEOUT" --ephemeral-storage Size=2048 \
    --vpc-config "$VPC_CFG" >/dev/null
else
  set -- --function-name "$FUNCTION" --package-type Image --code ImageUri="$IMAGE_URI" \
    --role "$ROLE_ARN" --memory-size "$MEMORY_MB" --timeout "$TIMEOUT" --architectures "$ARCH" \
    --ephemeral-storage Size=2048
  [ -n "$VPC_ARGS" ] && set -- "$@" --vpc-config "$VPC_ARGS"
  aws lambda create-function "$@" >/dev/null
fi
aws lambda wait function-updated-v2 --function-name "$FUNCTION" 2>/dev/null || \
  aws lambda wait function-active-v2 --function-name "$FUNCTION"
aws lambda put-function-concurrency --function-name "$FUNCTION" \
  --reserved-concurrent-executions "$CONCURRENCY" >/dev/null
# never retry: a job must run at most once, and the handler ends its process on purpose
aws lambda put-function-event-invoke-config --function-name "$FUNCTION" \
  --maximum-retry-attempts 0 --maximum-event-age-in-seconds 300 >/dev/null

say "IAM user $SERVER_USER for the server (invoke this function, read/write job files)"
FN_ARN=$(aws lambda get-function-configuration --function-name "$FUNCTION" --query FunctionArn --output text)
POLICY=$(printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["lambda:InvokeFunction","lambda:GetFunctionConfiguration"],"Resource":"%s"},{"Effect":"Allow","Action":["s3:PutObject","s3:GetObject","s3:DeleteObject"],"Resource":"arn:aws:s3:::%s/fast-compute/*"}]}' "$FN_ARN" "$BUCKET")
aws iam get-user --user-name "$SERVER_USER" >/dev/null 2>&1 || aws iam create-user --user-name "$SERVER_USER" >/dev/null
aws iam put-user-policy --user-name "$SERVER_USER" --policy-name "fast-compute-$FUNCTION" --policy-document "$POLICY"

cat <<DONE

Done. Next, on the server (aws-london), in fast-compute/server/.env:

1. Credentials (only if this user has no access key in .env yet). Create one with
     aws iam create-access-key --user-name $SERVER_USER
   and add (never commit these):
     AWS_ACCESS_KEY_ID=...
     AWS_SECRET_ACCESS_KEY=...

2. Add this node to DOCKERSERVER_NODES (keep the existing entries), e.g.:
     {"name":"aws-lambda","type":"lambda","function":"$FUNCTION","bucket":"$BUCKET","region":"$REGION","max_concurrency":$CONCURRENCY,"network":"$NETWORK"}

3. pip install boto3 in server/.venv (in requirements.txt), restart dockerserver, and check
   the node in /admin-ui -> Nodes.
DONE
