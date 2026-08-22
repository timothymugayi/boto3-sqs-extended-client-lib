#!/usr/bin/env bash
set -euo pipefail

BUCKET="${SQS_EXTENDED_S3_BUCKET:-sqs-extended-payloads}"
QUEUE="${SQS_EXTENDED_QUEUE_NAME:-sqs-extended-demo}"
FIFO_QUEUE="${SQS_EXTENDED_FIFO_QUEUE_NAME:-sqs-extended-demo.fifo}"

awslocal s3api head-bucket --bucket "$BUCKET" 2>/dev/null || awslocal s3 mb "s3://${BUCKET}"
awslocal sqs create-queue --queue-name "$QUEUE"
awslocal sqs create-queue \
	--queue-name "$FIFO_QUEUE" \
	--attributes FifoQueue=true,ContentBasedDeduplication=false
