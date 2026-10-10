Boto3 SQS Extended Client Library for Python
===========================================

[![Tests](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/actions/workflows/tests.yml/badge.svg?branch=master)](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/timothymugayi/boto3-sqs-extended-client-lib/branch/master/graph/badge.svg)](https://codecov.io/gh/timothymugayi/boto3-sqs-extended-client-lib)

The **Amazon SQS Extended Client Library for Python** (version **0.5.0**) is modelled after the original [Amazon SQS Extended Client for Java](https://github.com/awslabs/amazon-sqs-java-extended-client-lib). It stores large SQS payloads in Amazon S3 (over the 256 KB SQS limit) and puts a pointer on the queue.

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

Install a specific commit. Pin the full commit SHA of the revision you deploy. A branch name moves.

```bash
pip install "pysqs-extended-client @ git+https://github.com/timothymugayi/boto3-sqs-extended-client-lib.git@<commit-sha>"
```

## Required before production

These are not optional, and this library does not do them. A green test run does not show that they are in place. The library is ready for a payment only when the code in this release is what you run **and** every condition below is true.

* The payload prefix has an S3 lifecycle rule whose expiration is longer than the **sum** of the source-queue retention and the dead-letter retention. The dead-letter clock starts when the message arrives there, after it has already sat in the source, so being longer than each retention on its own is not enough. A shorter rule deletes the object while the message can still be read. That is the missing-object loss. Purge leaves the objects. A failed delete after SQS has already succeeded leaves the object. A failed delete after a send that SQS did not accept can leave one too. The library cannot create the rule and cannot remove the object while S3 is down.
* The FIFO queue has a redrive policy with a `deadLetterTargetArn` and a `maxReceiveCount` of at least 1. Send, batch send, and receive on a `.fifo` queue read `RedrivePolicy` and refuse to run when it is missing, empty, lacks either field, or has `maxReceiveCount` of 0. The check is not cached. Someone still has to create the policy. The dead-letter queue must also be FIFO, or SQS rejects the policy. This library checks only the source queue's `RedrivePolicy`. It creates neither queue. A dead-letter queue that nobody reads is not enough. The read needs `sqs:GetQueueAttributes`. That permission is new in 0.5.0 and is breaking for a least-privilege FIFO producer that previously only had `sqs:SendMessage`.
* The handler is idempotent on the instruction id inside the payload, not the SQS message id. A `message_deduplication_id` expires after 5 minutes, and a move to the dead-letter queue is a new message id. This library cannot extend that interval, and it does not invent a deduplication id.
* This library is the only receiver and the only deleter on the queue. Java may send text. The Java extended client still deletes the SQS message when the object is missing, and it still deletes the object before the message. If that client receives or deletes, the payment can still disappear, and this library's `message_left` callback does not fire. There is no change inside this repo that stops the Java client.

A timeout during receive still counts as a receive. A message dead-lettered that way is recoverable until the dead-letter retention, as long as the lifecycle rule has not deleted the object. It is not the missing-object loss.

## Usage

The four-argument constructor stays compatible. Messages under 256 KB stay in SQS unless you set `always_through_s3=True` on `ExtendedClientConfiguration` **before** constructing the client. After `__init__`, config is frozen (`FrozenConfigError` if you mutate it). There are no `set_always_through_s3` / `set_message_size_threshold` methods.

Hold this wrapper as your only SQS client. Send, receive, and delete through it so large payloads hydrate and S3 objects are cleaned up. Do not mix a raw boto3 SQS client on the same messages.

Call style differs from boto3: methods take positional `queue_url` / `message` instead of `QueueUrl=` / `MessageBody=`. Extra boto3 fields still work as kwargs (`DelaySeconds`, `VisibilityTimeout`). `receive_message` returns a **list of messages** (an empty queue returns `[]`), not boto3's `{"Messages": [...]}` dict and not `None`.

Access keys passed to the constructor are handed to boto3 and are not kept on the instance. Pass both the access key and the secret, or omit both and use the default credential chain (region alone is enough for that). A key without its secret raises `ValueError` at construction. Prefer the default credential chain or injected clients.

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

A payment send uses a FIFO queue. A standard queue can deliver the same body twice, and this library cannot stop that.

Pass `message_group_id` on its own when the queue uses content-based deduplication and the body stays inline. That body does not change, so SQS can deduplicate it. If the queue does not use content-based deduplication, SQS rejects the send. That rejection is not a posted message, and this library emits nothing: nothing was uploaded.

An offloaded body contains a new S3 key, so content-based deduplication will not collapse a retry. `send_message` and `send_message_batch` raise before any S3 upload when the queue name ends in `.fifo` and `message_deduplication_id` was not passed. A batch raises before any entry is uploaded or sent. This library does not invent a deduplication id. A caller-supplied id stops a second copy only inside SQS's 5-minute dedup interval. After that, the same id is a new message.

Before send, batch send, and receive, a `.fifo` queue must have a `RedrivePolicy` with `deadLetterTargetArn` and `maxReceiveCount` of at least 1. The client reads it with `GetQueueAttributes` on every call (the result is not cached) and raises before any upload or receive when the call fails or the policy is missing, empty, incomplete, or has a count of 0. It emits nothing. It checks only that queue, and it creates neither the source queue nor the dead-letter queue. The dead-letter queue must be FIFO or SQS rejects the policy. A standard queue is not checked. `sqs:GetQueueAttributes` is required; a least-privilege FIFO producer that only had `sqs:SendMessage` fails closed. The demo queue `sqs-extended-demo.fifo` is created without a redrive policy; send and receive through this library raise until you set one.

```python
sqs.send_message(
    queue_url,
    message,
    message_group_id="instruction-group",
    # Required when this body will be offloaded. Omit it only for an inline
    # body on a queue that has content-based deduplication.
    message_deduplication_id="instruction-id",
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
        # optional: message_size_threshold=262144,  # int, 1..262144; default 256 KiB
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
    delete_s3_before_sqs=False,  # True raises ValueError when the client is constructed
    use_legacy_attribute=True,
    ignore_payload_not_found=False,  # does not delete; a missing object leaves the SQS message
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

Inject boto3 clients when you need custom credentials, endpoints, or session configuration. Do not look for a separate `config.py` module. Omit both access keys to use the default credential chain. Setting only one of them raises `ValueError`.

### Fail-fast checks (0.4.1)

These run when `ExtendedClientConfiguration` or `SQSClientExtended` is created.

* **Bucket.** With `payload_support_enabled=True` (the default), a send can offload: every message if `always_through_s3=True`, otherwise any message larger than `message_size_threshold`. Construction raises `ValueError` when the effective bucket is missing. That error used to wait until the first offload (`S3 bucket name cannot be null`). A constructor name that differs from `config.s3_bucket_name` raises `ValueError` instead of ignoring the constructor. The same name in both places is fine; a constructor name fills an empty config. Whitespace-only is `ValueError`. A non-string is `TypeError`.

  Small-only processes that never offload, hydrate, or delete S3 payloads omit the bucket by setting `payload_support_enabled=False`. That is the supported no-bucket mode. `always_through_s3` does nothing there. Receive-only workers that hydrate pointers should still set `s3_bucket_name`. Receive reads the bucket from the pointer; the init check exists so a later send cannot fail on a missing bucket.

* **`message_size_threshold`.** An int from 1 through 262144 (the SQS 256 KiB limit), default 262144. `None` selects the default. Outside that range is `ValueError`. Booleans and other types are `TypeError`. A larger threshold would try to send a body SQS rejects.

* **Credentials.** Set `aws_access_key_id` and `aws_secret_access_key` together, or omit both. One without the other raises `ValueError`. A half-specified pair used to be dropped, and boto3 used the default chain. Region alone still uses that chain. Both keys are passed even when region is omitted; they used to be dropped unless region was set. Whitespace-only values count as omitted. Non-string key or region values raise `TypeError`.

* **`server_side_encryption`.** `None` or a `ServerSideEncryptionStrategy` (`aws_managed_cmk()`, `customer_key(...)`, `sse_s3()`). Anything else is `TypeError` at config construction. A wrong object used to fail later, when the upload called `extra_args`.

* **`delete_s3_before_sqs=True` (0.5.0).** `SQSClientExtended` raises `ValueError` at construction. The 0.4.1 construction warning is gone, and delete methods are never reached. There is no S3-then-SQS path and no dead-pointer callback. Leave the flag unset so the SQS message is deleted first.

* **`ignore_payload_not_found` (0.5.0).** The flag does not delete the SQS message. A confirmed S3 not-found leaves the message in the queue and, if `on_event` is set, calls it as `message_left` with `cause=s3_not_found`. The 0.4.1 `payload_missing_deleted` callback is gone. The same `message_left` event, with `cause=broken_pointer`, fires when `s3BucketName` or `s3Key` is missing or empty, and when the body has the size attribute but is not valid JSON. A timeout or an HTTP 5xx raises, does not delete, and emits nothing for itself. Every `message_left` in that batch is still emitted, and payload files for messages that will not be returned are removed, before that error is raised. An exception inside `on_event` is logged and ignored.

### Server-side encryption

`server_side_encryption` is the analog of Java `ServerSideEncryptionStrategy`. It must be one of the strategies below (or omitted). Other objects raise `TypeError` when the config is created. It is merged into S3 `ExtraArgs` next to `s3_canned_acl`:

* `aws_managed_cmk()` — SSE-KMS with the AWS-managed `aws/s3` key (`ServerSideEncryption=aws:kms`), same as Java `AwsManagedCmk`.
* `customer_key("alias/my-key")` or a key id — SSE-KMS with that CMK, same as Java `CustomerKey`.
* `sse_s3()` — SSE-S3 (`AES256`). The Java factory does not expose this; it is here because boto3 callers use it.

Downloads of SSE-KMS objects use the caller's IAM `kms:Decrypt`. The client does not implement customer-provided keys (SSE-C).

### Deletes and orphaned objects

Delete order is **SQS first**, then a best-effort S3 delete. `delete_s3_before_sqs=True` raises `ValueError` when the client is constructed. There is no S3-then-SQS path. The caller is not told the object went first when it did not.

* If `DeleteMessage` fails, the S3 object is still there. Retry the delete after the visibility timeout.
* If the SQS delete succeeds and the S3 delete fails, the queue message is gone and the object is left in the bucket. `on_event` is called as `s3_object_orphaned` with `case=after_sqs_succeeded`. The SQS delete still stands. The event means the object is still in the bucket. It does not mean a lifecycle rule removed it.

Receipt-handle markers stay compatible with Java either way, so a Java producer can still send text this client reads. Do not let the Java extended client receive or delete on this queue. It still deletes the SQS message when the object is missing, and it still deletes the object before the message.

`purge_queue` clears SQS only. S3 objects are left behind.

If `send_message` uploads to S3 and SQS definitely rejects the send, the client tries to delete that object. A definite rejection is a botocore `ClientError` with an HTTP 4xx status and `RetryAttempts` of 0, a client-side validation error raised before the request is sent (`ParamValidationError` and the same class of check), or a batch `Failed` entry with `SenderFault` true. That cleanup is best-effort. If it fails, `on_event` is called as `s3_object_orphaned` with `case=after_upload_sqs_rejected`. The same delete runs for S3 objects uploaded in a batch whose offload fails before any send.

A timeout, a connection error, an HTTP 5xx, a 4xx whose `RetryAttempts` is greater than 0, or a batch `Failed` entry without `SenderFault` true is not a definite rejection. boto3 retries timeouts, and the error the caller sees can be a throttled 400 after SQS already stored the first attempt. The object is left for the lifecycle rule, `on_event` is called as `s3_object_orphaned` with `case=after_sqs_uncertain`, and a raised send error is re-raised. An object is also left behind if the process dies between the S3 put and the SQS send, or if `purge_queue` is used.

A confirmed missing object, a pointer missing `s3BucketName` or `s3Key`, or a size-attribute body that is not valid JSON does not delete the SQS message. `on_event` is `message_left`. A successful offload, hydrate, or delete does not use `message_left` or `s3_object_orphaned`. The FIFO deduplication raise and the redrive raise emit nothing: both happen before any upload. An exception raised inside `on_event` is logged and ignored.

Put a lifecycle rule on the payload prefix so leftovers expire. Abort incomplete multipart uploads in the same rule. The expiration must be longer than the sum of the source-queue retention and the dead-letter retention (see Required before production). Example (expire payloads after 14 days, which is only enough when that sum is shorter):

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

Prefer one role for producers and one for consumers. Scope S3 to the payload prefix. Add the KMS statements only when `server_side_encryption` is a KMS strategy. `sqs:GetQueueAttributes` is required in 0.5.0 for FIFO send, batch send, and receive. A least-privilege FIFO producer that previously only allowed `sqs:SendMessage` must add it or those calls fail closed.

Producer (send). `s3:DeleteObject` is what delete-on-rejection uses. Without it, a definite SQS rejection cannot remove the object that was just uploaded.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sqs:SendMessage", "sqs:GetQueueAttributes"],
      "Resource": "arn:aws:sqs:us-east-1:123456789012:my-queue"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:DeleteObject"],
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

Consumer (receive, delete, visibility). `s3:DeleteObject` is required only when `cleanup_s3_payload` is true. `s3:ListBucket` has to be on the bucket and limited to the payload prefix. Without it, S3 answers `GetObject` for a missing key with 403 instead of 404, so the not-found check never matches and `message_left` never fires.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"],
      "Resource": "arn:aws:sqs:us-east-1:123456789012:my-queue"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::my-payload-bucket/payloads/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::my-payload-bucket",
      "Condition": {"StringLike": {"s3:prefix": ["payloads/*"]}}
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
    on_event=lambda name, **fields: None,  # s3_offload / s3_hydrate / s3_delete are debug metrics; message_left and s3_object_orphaned are the failure signals. A raise inside the callback is logged and ignored.
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
