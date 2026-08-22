Boto3 SQS Extended Client Library for Python
===========================================

[![Build Status](https://travis-ci.org/timothymugayi/boto3-sqs-extended-client-lib.svg?branch=master)](https://travis-ci.org/timothymugayi/boto3-sqs-extended-client-lib)
[![codecov](https://codecov.io/gh/timothymugayi/boto3-sqs-extended-client-lib/branch/master/graph/badge.svg)](https://codecov.io/gh/timothymugayi/boto3-sqs-extended-client-lib)

The **Amazon SQS Extended Client Library for Python** (version **0.2.0**) is modelled after the original [Amazon SQS Extended Client for Java](https://github.com/awslabs/amazon-sqs-java-extended-client-lib). It stores large SQS payloads in Amazon S3 (over the 256 KB SQS limit, up to 2 GB) and puts a pointer on the queue.

You can:

* Use **one** `SQSClientExtended` as your SQS client — you do not need a separate `boto3.client("sqs")` for queue operations.
* Store payloads in S3 only when they exceed 256 KB, or always (`always_through_s3`).
* Send and receive messages whose body is a Java-compatible `PayloadS3Pointer`.
* Delete the S3 object when the SQS message is deleted (optional).
* Change visibility and batch-send/delete using S3-embedded receipt handles.

## Getting Started

* **Sign up for AWS** -- Before you begin, you need an AWS account. For more information about creating an AWS account and retrieving your AWS credentials, see [AWS Account and Credentials](https://docs.aws.amazon.com/general/latest/gr/aws-sec-cred-types.html).
* **Sign up for Amazon SQS** -- Go to the Amazon [SQS console](https://console.aws.amazon.com/sqs/home?region=us-east-1) to sign up for the service.
* **Minimum requirements** -- Python 3 (Python 2.7 is no longer recommended)
* **Further information** - Read the [API documentation](http://aws.amazon.com/documentation/sqs/).

## Installation

```
pip install pysqs-extended-client
```

## Usage

The four-argument constructor stays compatible. Messages under 256 KB stay in SQS unless you set `always_through_s3=True` on `ExtendedClientConfiguration` **before** constructing the client. After `__init__`, config is frozen (`FrozenConfigError` if you mutate it, including `set_always_through_s3`).

Hold this wrapper as your only SQS client. Send, receive, and delete through it so large payloads hydrate and S3 objects are cleaned up. Do not mix a raw boto3 SQS client on the same messages.

Call style differs from boto3: methods take positional `queue_url` / `message` instead of `QueueUrl=` / `MessageBody=`. Extra boto3 fields still work as kwargs (`DelaySeconds`, `VisibilityTimeout`). `receive_message` returns a **list of messages**, or `None` if the queue is empty — not boto3's `{"Messages": [...]}` dict.

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

messages = sqs.receive_message(queue_url)  # list or None
for message in messages or []:
    sqs.change_message_visibility(queue_url, message["ReceiptHandle"], 60)
    sqs.delete_message(queue_url, message["ReceiptHandle"])
```

### FIFO queues

```python
import uuid

sqs.send_message(
    queue_url,
    message,
    message_group_id=str(uuid.uuid4()),
    message_deduplication_id=str(uuid.uuid4()),
)
```

### Large payloads and files

The public send API is a **string** (`send_message(queue_url, message)`). If the UTF-8 size of the body (plus attributes) is over 256 KB, or you set `always_through_s3=True`, the library uploads the payload to S3 (multipart above ~8 MB) and puts a Java `PayloadS3Pointer` on the queue. Receive hydrates the original string.

```python
from pysqs_extended_client import SQSClientExtended, ExtendedClientConfiguration

sqs = SQSClientExtended(
    aws_access_key_id,
    aws_secret_access_key,
    aws_region_name,
    config=ExtendedClientConfiguration(
        s3_bucket_name="my-payload-bucket",
        s3_key_prefix="payloads/",
        # optional: always_through_s3=True,
        # optional: message_size_threshold=262144,  # default 256 KiB
    ),
)

with open("large-report.json", "r", encoding="utf-8") as handle:
    payload = handle.read()

sqs.send_message(queue_url, payload)

messages = sqs.receive_message(queue_url, wait_time_seconds=10)
for message in messages or []:
    with open("received-report.json", "w", encoding="utf-8") as handle:
        handle.write(message["Body"])
    sqs.delete_message(queue_url, message["ReceiptHandle"])
```

SQS still has a 256 KB **message** limit; only the payload bytes go to S3. Binary files are not a first-class `fileobj` API — encode them first (for example `base64.b64encode(data).decode("ascii")`) and decode after receive. The hydrated body is held in memory, same as the Java client.

### Configuration and injected clients

```python
import boto3
from pysqs_extended_client import SQSClientExtended, ExtendedClientConfiguration

config = ExtendedClientConfiguration(
    s3_bucket_name="my-payload-bucket",
    always_through_s3=False,
    cleanup_s3_payload=True,
    use_legacy_attribute=True,
    ignore_payload_not_found=False,
    s3_key_prefix="payloads/",
)
sqs = SQSClientExtended(
    sqs_client=boto3.client("sqs"),
    s3_client=boto3.client("s3"),
    config=config,
)
```

`use_legacy_attribute=True` (default, same as Java) writes `SQSLargePayloadSize`. Set it `False` to write `ExtendedPayloadSize`. Receive accepts both.

Configure the client **before** constructing it. After `SQSClientExtended` is created the config is frozen (unlike the Java client, which is documented as not thread-safe). Setters such as `set_always_through_s3` raise `FrozenConfigError` after init.

### Sharing a client across worker threads

Create **one** `SQSClientExtended` per process and share it across Celery / gunicorn / thread-pool workers. boto3 clients are thread-safe when created once.

Set `max_pool_connections` at least as high as your worker thread count (default 50). `s3_max_concurrency` (default 10) bounds parallel S3 uploads, hydrates, and deletes on batch/receive. Built clients use connect/read timeouts (3s / 60s) and standard retries. Injected `sqs_client` / `s3_client` are not wrapped.

Large S3 bodies use multipart upload via boto3 `TransferConfig` (8 MB threshold). The public send API is still a string; the Java pointer format is unchanged.

If a batch `send_message_batch` S3 offload fails, the whole batch is not sent.

```python
config = ExtendedClientConfiguration(
    s3_bucket_name="my-payload-bucket",
    max_pool_connections=50,
    s3_max_concurrency=10,
    on_event=lambda name, **fields: None,  # optional metrics: s3_offload / s3_hydrate / s3_delete
)
```

### Batch and pass-through APIs

S3-aware methods: `send_message`, `send_message_batch`, `receive_message`, `delete_message`, `delete_message_batch`, `change_message_visibility`, `change_message_visibility_batch`, `purge_queue`.

`purge_queue` clears SQS only; S3 objects are left behind (same warning as Java).

Other SQS calls (`create_queue`, `get_queue_url`, `list_queues`, `tag_queue`, …) are forwarded to the underlying boto3 SQS client, so this wrapper can be the only SQS object you hold. Extra boto3 fields such as `DelaySeconds` and `VisibilityTimeout` are accepted on send/receive.

Call `sqs.close()` when the process is shutting down to stop the bounded S3 thread pool. This is not a `boto3.resource("sqs")` replacement.

### LocalStack

Use Docker Compose to run SQS and S3 locally. The init hook creates bucket `sqs-extended-payloads`, queue `sqs-extended-demo`, and FIFO queue `sqs-extended-demo.fifo`.

```bash
docker compose up -d --wait
```

Point the library at LocalStack with `endpoint_url` (dummy credentials are enough):

```python
from pysqs_extended_client import SQSClientExtended, ExtendedClientConfiguration

sqs = SQSClientExtended(
    "test",
    "test",
    "us-east-1",
    config=ExtendedClientConfiguration(
        s3_bucket_name="sqs-extended-payloads",
        endpoint_url="http://127.0.0.1:4566",
    ),
)
queue_url = sqs.get_queue_url(QueueName="sqs-extended-demo")["QueueUrl"]
sqs.send_message(queue_url, "hello from localstack")
```

You can also inject boto3 clients created with `endpoint_url="http://127.0.0.1:4566"`. Injected clients are not wrapped.

```bash
pytest -m localstack          # skipped unless LocalStack is up
docker compose down
```

## Feedback

We use GitHub issues for tracking bugs and feature requests.

* Give feedback [here](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/issues).
* If you'd like to contribute a new feature or bug fix, go ahead submit a pull request.
