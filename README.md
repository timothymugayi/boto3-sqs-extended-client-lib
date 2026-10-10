Boto3 SQS Extended Client Library for Python
===========================================

[![Tests](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/actions/workflows/tests.yml/badge.svg?branch=master)](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/timothymugayi/boto3-sqs-extended-client-lib/branch/master/graph/badge.svg)](https://codecov.io/gh/timothymugayi/boto3-sqs-extended-client-lib)

The **Amazon SQS Extended Client Library for Python** (version **0.5.1**) is modelled after the original [Amazon SQS Extended Client for Java](https://github.com/awslabs/amazon-sqs-java-extended-client-lib). It stores large SQS payloads in Amazon S3 (over the 256 KB SQS limit) and puts a pointer on the queue.

You can:

* Use **one** `SQSClientExtended` as your SQS client — you do not need a separate `boto3.client("sqs")` for queue operations.
* Store payloads in S3 only when they exceed 256 KB, or always (`always_through_s3`).
* Send and receive messages whose body is a Java-compatible `PayloadS3Pointer`.
* Delete the S3 object when the SQS message is deleted (optional).
* Change visibility and batch-send/delete using S3-embedded receipt handles.

## Getting Started

* **Sign up for AWS** -- Before you begin, you need an AWS account. For more information about creating an AWS account and retrieving your AWS credentials, see [AWS Account and Credentials](https://docs.aws.amazon.com/general/latest/gr/aws-sec-cred-types.html).
* **Sign up for Amazon SQS** -- Go to the Amazon [SQS console](https://console.aws.amazon.com/sqs/home?region=us-east-1) to sign up for the service.
* **Minimum requirements** -- Python 3.9+
* **Further information** - Read the [API documentation](http://aws.amazon.com/documentation/sqs/).

## Installation

Requires Python 3.9+.

PyPI project `pysqs-extended-client` is stuck at **0.0.1** until the account is recovered ([issue #19](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/issues/19)). `0.0.1` does not include the extended client in this tree. Do not `pip install pysqs-extended-client` without a pin.

PyPI publishing of this release is coming once that account is recovered. Until then, install 0.5.1 by pinning the commit SHA of the revision you deploy. A branch name moves, and this release is not tagged.

```bash
pip install "pysqs-extended-client @ git+https://github.com/timothymugayi/boto3-sqs-extended-client-lib.git@<commit-sha>"
```

## Quick start

Send, receive, and delete through one `SQSClientExtended`. Messages under 256 KB stay in SQS unless you set `always_through_s3=True` on `ExtendedClientConfiguration` before constructing the client. `receive_message` returns a list of messages (an empty queue returns `[]`). Call style, frozen config, and credentials are in [Configuration](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/configuration.md#usage).

```python
from pysqs_extended_client import SQSClientExtended, ExtendedClientConfiguration

sqs = SQSClientExtended(
    aws_access_key_id,
    aws_secret_access_key,
    aws_region_name,
    "my-payload-bucket",
)

# Or always offload:
# sqs = SQSClientExtended(..., config=ExtendedClientConfiguration(
#     s3_bucket_name="my-payload-bucket", always_through_s3=True,
# ))

sqs.send_message(queue_url, "small body")

# Overflow or always-through-S3 writes a Java-compatible pointer:
# ["software.amazon.payloadoffloading.PayloadS3Pointer", {"s3BucketName": "...", "s3Key": "..."}]
# and a reserved attribute SQSLargePayloadSize (or ExtendedPayloadSize if configured).

messages = sqs.receive_message(queue_url)  # list, empty when the queue is empty
for message in messages:
    sqs.change_message_visibility(queue_url, message["ReceiptHandle"], 60)
    sqs.delete_message(queue_url, message["ReceiptHandle"])
```

## Breaking changes

0.5.0 leaves a confirmed missing payload on the queue, raises `ValueError` at construction when `delete_s3_before_sqs=True`, and requires `sqs:GetQueueAttributes` plus a FIFO redrive policy. Notes for 0.4.0, 0.4.1, and 0.5.0 are in [Migration](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/migration.md#v050).

## Required before production

These are not optional, and this library does not do them. A green test run does not show that they are in place. The library is ready for a payment only when the code in this release is what you run **and** every condition below is true.

* **S3 lifecycle.** The payload prefix has an S3 lifecycle rule whose expiration is longer than the **sum** of the source-queue retention and the dead-letter retention. Longer than each retention on its own is not enough. A shorter rule deletes the object while the message can still be read. [Details](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/fifo-and-redrive.md#s3-lifecycle)
* **FIFO redrive.** The FIFO queue has a redrive policy with a `deadLetterTargetArn` and a `maxReceiveCount` of at least 1. The dead-letter queue must also be FIFO. Send, batch send, and receive on a `.fifo` queue refuse to run when the policy is missing, empty, incomplete, or has `maxReceiveCount` of 0. The read needs `sqs:GetQueueAttributes`, which is new in 0.5.0. [Details](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/fifo-and-redrive.md#redrive-policy)
* **Idempotent handler.** The handler is idempotent on the instruction id inside the payload, not the SQS message id. A `message_deduplication_id` expires after 5 minutes, and a move to the dead-letter queue is a new message id. [Details](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/fifo-and-redrive.md#idempotent-handlers)
* **Only this library receives and deletes.** This library is the only receiver and the only deleter on the queue. Java may send text. If the Java extended client receives or deletes, the payment can still disappear, and `message_left` does not fire. [Details](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/java-compatibility.md#only-this-library-receives-and-deletes)

A timeout during receive still counts as a receive. A message dead-lettered that way is recoverable until the dead-letter retention, as long as the lifecycle rule has not deleted the object. It is not the missing-object loss.

[How a receive timeout differs from the missing-object loss](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/fifo-and-redrive.md#receive-timeouts).

## Documentation

* [Configuration](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/configuration.md) — options, fail-fast startup checks, injected clients, worker threads, pass-through API
* [IAM](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/iam.md) — producer and consumer policies, including `s3:ListBucket` on the bucket ARN, `s3:DeleteObject`, and `sqs:GetQueueAttributes`
* [FIFO and redrive](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/fifo-and-redrive.md) — deduplication ids, `RedrivePolicy` / `maxReceiveCount`, FIFO dead-letter queue, S3 lifecycle, idempotent handlers
* [Events](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/events.md) — `message_left`, `s3_object_orphaned`, their causes, and what to alert on
* [Binary and files](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/binary-and-files.md) — `bytes`, files, and streaming payloads
* [Java compatibility](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/java-compatibility.md) — `PayloadS3Pointer`, legacy attribute, and not sharing receive or delete with the Java client
* [Migration](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/migration.md) — 0.4.0, 0.4.1, and 0.5.0 notes
* [Testing](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/blob/master/docs/testing.md) — LocalStack and local unit tests

## Feedback

We use GitHub issues for tracking bugs and feature requests.

* Give feedback [here](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/issues).
* If you'd like to contribute a new feature or bug fix, go ahead submit a pull request.
