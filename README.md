Boto3 SQS Extended Client Library for Python
===========================================

[![Tests](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/actions/workflows/tests.yml/badge.svg?branch=master)](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/timothymugayi/boto3-sqs-extended-client-lib/branch/master/graph/badge.svg)](https://codecov.io/gh/timothymugayi/boto3-sqs-extended-client-lib)

The **Amazon SQS Extended Client Library for Python** (version **0.4.0**) is modelled after the original [Amazon SQS Extended Client for Java](https://github.com/awslabs/amazon-sqs-java-extended-client-lib). It stores large SQS payloads in Amazon S3 (over the 256 KB SQS limit) and puts a pointer on the queue.

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

After **0.4.0** is on PyPI:

```bash
pip install "pysqs-extended-client>=0.4.0"
```

PyPI project `pysqs-extended-client` still serves **0.0.1** only ([issue #19](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/issues/19)). Until the maintainer publishes 0.4.0, install from this repository:

```bash
pip install "pysqs-extended-client @ git+https://github.com/timothymugayi/boto3-sqs-extended-client-lib.git"
```

Do not `pip install pysqs-extended-client` without a version pin until that upload exists. `0.0.1` does not include the extended client in this tree.

## Usage

The four-argument constructor stays compatible. Messages under 256 KB stay in SQS unless you set `always_through_s3=True` on `ExtendedClientConfiguration` **before** constructing the client. After `__init__`, config is frozen (`FrozenConfigError` if you mutate it). There are no `set_always_through_s3` / `set_message_size_threshold` methods.

Hold this wrapper as your only SQS client. Send, receive, and delete through it so large payloads hydrate and S3 objects are cleaned up. Do not mix a raw boto3 SQS client on the same messages.

Call style differs from boto3: methods take positional `queue_url` / `message` instead of `QueueUrl=` / `MessageBody=`. Extra boto3 fields still work as kwargs (`DelaySeconds`, `VisibilityTimeout`). `receive_message` returns a **list of messages** (an empty queue returns `[]`), not boto3's `{"Messages": [...]}` dict and not `None`.

Access keys passed to the constructor are handed to boto3 and are not kept on the instance. Prefer the default credential chain or injected clients.

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

`send_message(queue_url, message)` treats every `str` as text, including a string that happens to be a path to a file on disk. Small messages stay in SQS. Messages over 256 KB (or `always_through_s3=True`) are stored in S3 as UTF-8. Receive of those messages, including ones sent by older versions of this library, returns a `str`. The Java `PayloadS3Pointer` format is unchanged.

`bytes` and binary file-like objects are binary. To upload a file, call `send_file(queue_url, path)` or pass a `pathlib.Path` (any `os.PathLike`). Those use `upload_file` / `upload_fileobj` (multipart above ~8 MB) and are not loaded into memory when they go to S3. Receive returns `bytes` for those payloads unless you pass `payload_dir`. Messages without `SQSExtendedContentType` stay text.

**0.4.0 migration:** code that passed an existing path string to `send_message` now sends that path as text. Switch those calls to `send_file` or `pathlib.Path`. `receive_message` on an empty queue returns `[]` instead of `None`.

```python
from pathlib import Path

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

sqs.send_message(queue_url, "small body")
sqs.send_message(queue_url, b"\x00\x01\x02")
sqs.send_file(queue_url, "/path/to/large-video.mp4")
sqs.send_message(queue_url, Path("/path/to/large-video.mp4"))
with open("huge.zip", "rb") as handle:
    sqs.send_message(queue_url, handle)

messages = sqs.receive_message(queue_url, wait_time_seconds=10)
for message in messages:
    body = message["Body"]  # str for text, bytes for binary
    sqs.delete_message(queue_url, message["ReceiptHandle"])

# Stream a large S3 payload to disk instead of holding it in memory.
# Body and _payload_path are the file path. Inline messages are unchanged.
large = sqs.receive_message(queue_url, payload_dir="/tmp/sqs-payloads")
```

SQS still has a 256 KB **message** limit; only the payload bytes go to S3. The default receive path loads the payload into memory, same as the Java client. That is a poor fit for multi-hundred-MB objects: pass `payload_dir` (or `get_payload_from_s3(..., filename=...)`) so boto3 streams the object to a file.

### Configuration and injected clients

```python
import boto3
from pysqs_extended_client import ExtendedClientConfiguration, SQSClientExtended, customer_key

config = ExtendedClientConfiguration(
    s3_bucket_name="my-payload-bucket",
    always_through_s3=False,
    payload_support_enabled=True,  # False: pass through, no S3 offload/hydrate/delete
    cleanup_s3_payload=True,
    delete_s3_before_sqs=False,  # True matches Java's S3-then-SQS delete order
    use_legacy_attribute=True,
    ignore_payload_not_found=False,
    s3_key_prefix="payloads/",
    s3_canned_acl=None,  # e.g. "bucket-owner-read"; omit unless Object Ownership allows ACLs
    server_side_encryption=customer_key("alias/my-payload-key"),  # or aws_managed_cmk() / sse_s3()
)
sqs = SQSClientExtended(
    sqs_client=boto3.client("sqs"),
    s3_client=boto3.client("s3"),
    config=config,
)
```

`use_legacy_attribute=True` (default, same as Java) writes `SQSLargePayloadSize`. Set it `False` to write `ExtendedPayloadSize`. Receive accepts both.

`payload_support_enabled=True` (default) is Java's payload-support switch. `is_large_payload_support_enabled()` returns that flag. When it is `False`, send does not offload, receive does not hydrate pointers, and delete does not remove S3 objects or strip receipt handles. Set it on the config before constructing the client.

Configure the client **before** constructing it. After `SQSClientExtended` is created the config is frozen (unlike the Java client, which is documented as not thread-safe). `set_always_through_s3` and `set_message_size_threshold` are not part of the API.

Inject boto3 clients when you need custom credentials, endpoints, or session configuration. Do not look for a separate `config.py` module. Omit access keys to use the default credential chain.

### Server-side encryption

`server_side_encryption` is the analog of Java `ServerSideEncryptionStrategy`. It is merged into S3 `ExtraArgs` next to `s3_canned_acl`:

* `aws_managed_cmk()` — SSE-KMS with the AWS-managed `aws/s3` key (`ServerSideEncryption=aws:kms`), same as Java `AwsManagedCmk`.
* `customer_key("alias/my-key")` or a key id — SSE-KMS with that CMK, same as Java `CustomerKey`.
* `sse_s3()` — SSE-S3 (`AES256`). The Java factory does not expose this; it is here because boto3 callers use it.

Downloads of SSE-KMS objects use the caller's IAM `kms:Decrypt`. The client does not implement customer-provided keys (SSE-C).

### Deletes, dead pointers, and orphaned objects

Default delete order is **SQS first**, then a best-effort S3 delete.

* If `DeleteMessage` fails, the S3 object is still there. Retry the delete after the visibility timeout.
* If the SQS delete succeeds and the S3 delete fails, the queue message is gone and the object is orphaned. The error is logged and not raised.

The Java extended client deletes S3 **first**. If SQS delete then fails, the message stays on the queue and points at a missing object (a dead pointer). Receipt-handle markers are the same either way, so Java consumers can still read messages this client sent. Set `delete_s3_before_sqs=True` only if you need that Java failure mode. This client logs an error when that path creates a dead pointer.

`purge_queue` clears SQS only. S3 objects are left behind.

If `send_message` uploads to S3 and the SQS send then fails, the client tries to delete that object. The same cleanup runs for batch entries that SQS reports as failed, and for S3 objects uploaded in a batch whose offload fails before any send. That cleanup is best-effort. An object is also orphaned if the process dies between the S3 put and the SQS send, or if `purge_queue` is used.

Put a lifecycle rule on the payload prefix so leftovers expire. Abort incomplete multipart uploads in the same rule. Example (expire payloads after 14 days):

```json
{
  "Rules": [{
    "ID": "expire-sqs-payloads",
    "Status": "Enabled",
    "Filter": {"Prefix": "payloads/"},
    "Expiration": {"Days": 14},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
  }]
}
```

### IAM

Prefer one role for producers and one for consumers. Scope S3 to the payload prefix. Add the KMS statements only when `server_side_encryption` is a KMS strategy.

Producer (send):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": "arn:aws:sqs:us-east-1:123456789012:my-queue"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::my-payload-bucket/payloads/*"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:GenerateDataKey", "kms:Encrypt"],
      "Resource": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    }
  ]
}
```

Consumer (receive, delete, visibility). `s3:DeleteObject` is required only when `cleanup_s3_payload` is true.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility"],
      "Resource": "arn:aws:sqs:us-east-1:123456789012:my-queue"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::my-payload-bucket/payloads/*"
    },
    {
      "Effect": "Allow",
      "Action": ["kms:Decrypt"],
      "Resource": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    }
  ]
}
```

### Cross-account S3 payloads

Uploads default to the bucket's Object Ownership settings (no canned ACL). Prefer a **bucket policy** that grants the consuming account `s3:GetObject` (and `s3:DeleteObject` if `cleanup_s3_payload` is on).

If you still need a canned ACL, set `s3_canned_acl` (for example `"bucket-owner-read"` or `"bucket-owner-full-control"`). That is passed as `ExtraArgs={"ACL": ...}` on `upload_fileobj`. Buckets with Object Ownership **BucketOwnerEnforced** reject ACLs; leave `s3_canned_acl` unset in that case.

### Sharing a client across worker threads

Create **one** `SQSClientExtended` per process and share it across Celery / gunicorn / thread-pool workers. boto3 clients are thread-safe when created once.

Set `max_pool_connections` at least as high as your worker thread count (default 50). `s3_max_concurrency` (default 10) bounds parallel S3 uploads, hydrates, and deletes on batch/receive. Built clients use connect/read timeouts (3s / 60s) and standard retries. Injected `sqs_client` / `s3_client` are not wrapped.

Large S3 bodies use multipart upload via boto3 `TransferConfig` (8 MB threshold). Text messages keep the Java pointer format. `send_file`, `pathlib.Path`, and binary file objects stream through that same transfer config. A plain string is never treated as a path.

If a batch `send_message_batch` S3 offload fails, the whole batch is not sent. Objects uploaded before that failure are deleted on a best-effort basis.

```python
config = ExtendedClientConfiguration(
    s3_bucket_name="my-payload-bucket",
    max_pool_connections=50,
    s3_max_concurrency=10,
    on_event=lambda name, **fields: None,  # optional metrics: s3_offload / s3_hydrate / s3_delete
)
```

### Batch and pass-through APIs

S3-aware methods: `send_message`, `send_file`, `send_message_batch`, `receive_message`, `delete_message`, `delete_message_batch`, `change_message_visibility`, `change_message_visibility_batch`, `purge_queue`.

`purge_queue` clears SQS only; S3 objects are left behind (same warning as Java). See the lifecycle note above.

Other SQS calls (`create_queue`, `get_queue_url`, `list_queues`, `tag_queue`, …) are forwarded to the underlying boto3 SQS client, so this wrapper can be the only SQS object you hold. Extra boto3 fields such as `DelaySeconds` and `VisibilityTimeout` are accepted on send/receive.

Call `sqs.close()` when the process is shutting down to stop the bounded S3 thread pool. This is not a `boto3.resource("sqs")` replacement.

### LocalStack

Use Docker Compose to run SQS and S3 locally. The init hook creates bucket `sqs-extended-payloads`, queue `sqs-extended-demo`, and FIFO queue `sqs-extended-demo.fifo`.

The compose file pins `localstack/localstack:4.14.0` so it starts **without an account**. LocalStack 2026.03 and later require `LOCALSTACK_AUTH_TOKEN` (see [app.localstack.cloud](https://app.localstack.cloud)); you can override with `LOCALSTACK_IMAGE` and that env var.

```bash
docker compose up -d
```

Wait until `http://127.0.0.1:4566/_localstack/health` responds, then run either the pytest marker or the smoke script. Both talk to `http://127.0.0.1:4566` with dummy credentials. They are skipped or fail if LocalStack is not up.

```bash
pytest -m localstack
python scripts/localstack_smoke.py
docker compose down
```

`pytest -m localstack` runs `tests/test_localstack.py`. `scripts/localstack_smoke.py` is the same checks as a standalone script. Each one checks:

* a small string stays in SQS and comes back as `str`
* a large string is stored in S3 and still comes back as `str`
* small `bytes` come back as `bytes`
* `send_file` / a `pathlib.Path` and a binary file object come back as `bytes`
* a 16 KiB payload with `multipart_threshold=1024` calls S3 `CreateMultipartUpload` and round-trips

Point your own code at the same endpoint:

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

You can also inject boto3 clients created with `endpoint_url="http://127.0.0.1:4566"`. Injected clients are not wrapped. The default multipart threshold is 8 MB. Lower `multipart_threshold` on `ExtendedClientConfiguration` when you want a small local file to take the multipart path.

## Feedback

We use GitHub issues for tracking bugs and feature requests.

* Give feedback [here](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/issues).
* If you'd like to contribute a new feature or bug fix, go ahead submit a pull request.
