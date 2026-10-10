# Migration

Notes for 0.4.0, 0.4.1, and 0.5.0 taken from this tree. The release history is [CHANGELOG.rst](../CHANGELOG.rst).

[README](../README.md) · [Configuration](configuration.md) · [IAM](iam.md) · [FIFO and redrive](fifo-and-redrive.md) · [Events](events.md) · [Binary and files](binary-and-files.md) · [Java compatibility](java-compatibility.md) · Migration · [Testing](testing.md)

<a id="v040"></a>

## 0.4.0

**0.4.0 migration:** code that passed an existing path string to `send_message` now sends that path as text. Switch those calls to `send_file` or `pathlib.Path`. `receive_message` on an empty queue returns `[]` instead of `None`.

0.4.0 also removed `set_always_through_s3` and `set_message_size_threshold` ([CHANGELOG.rst](../CHANGELOG.rst)). The API that remains:

The four-argument constructor stays compatible. Messages under 256 KB stay in SQS unless you set `always_through_s3=True` on `ExtendedClientConfiguration` **before** constructing the client. After `__init__`, config is frozen (`FrozenConfigError` if you mutate it). There are no `set_always_through_s3` / `set_message_size_threshold` methods.

Configure the client **before** constructing it. After `SQSClientExtended` is created the config is frozen (unlike the Java client, which is documented as not thread-safe). `set_always_through_s3` and `set_message_size_threshold` are not part of the API.

<a id="v041"></a>

## 0.4.1

Fail-fast checks run when `ExtendedClientConfiguration` or `SQSClientExtended` is created. The 0.5.0 changes to `delete_s3_before_sqs` and `ignore_payload_not_found` are under [0.5.0](#v050).

* **Bucket.** With `payload_support_enabled=True` (the default), a send can offload: every message if `always_through_s3=True`, otherwise any message larger than `message_size_threshold`. Construction raises `ValueError` when the effective bucket is missing. That error used to wait until the first offload (`S3 bucket name cannot be null`). A constructor name that differs from `config.s3_bucket_name` raises `ValueError` instead of ignoring the constructor. The same name in both places is fine; a constructor name fills an empty config. Whitespace-only is `ValueError`. A non-string is `TypeError`.

  Small-only processes that never offload, hydrate, or delete S3 payloads omit the bucket by setting `payload_support_enabled=False`. That is the supported no-bucket mode. `always_through_s3` does nothing there. Receive-only workers that hydrate pointers should still set `s3_bucket_name`. Receive reads the bucket from the pointer; the init check exists so a later send cannot fail on a missing bucket.

* **`message_size_threshold`.** An int from 1 through 262144 (the SQS 256 KiB limit), default 262144. `None` selects the default. Outside that range is `ValueError`. Booleans and other types are `TypeError`. A larger threshold would try to send a body SQS rejects.

* **Credentials.** Set `aws_access_key_id` and `aws_secret_access_key` together, or omit both. One without the other raises `ValueError`. A half-specified pair used to be dropped, and boto3 used the default chain. Region alone still uses that chain. Both keys are passed even when region is omitted; they used to be dropped unless region was set. Whitespace-only values count as omitted. Non-string key or region values raise `TypeError`.

* **`server_side_encryption`.** `None` or a `ServerSideEncryptionStrategy` (`aws_managed_cmk()`, `customer_key(...)`, `sse_s3()`). Anything else is `TypeError` at config construction. A wrong object used to fail later, when the upload called `extra_args`.

The full check list, including the 0.5.0 entries, is in [Fail-fast checks (0.4.1)](configuration.md#fail-fast-checks).

<a id="v050"></a>

## 0.5.0

* **`delete_s3_before_sqs=True` (0.5.0).** `SQSClientExtended` raises `ValueError` at construction. The 0.4.1 construction warning is gone, and delete methods are never reached. There is no S3-then-SQS path and no dead-pointer callback. Leave the flag unset so the SQS message is deleted first.

* **`ignore_payload_not_found` (0.5.0).** The flag does not delete the SQS message. A confirmed S3 not-found leaves the message in the queue and, if `on_event` is set, calls it as `message_left` with `cause=s3_not_found`. The 0.4.1 `payload_missing_deleted` callback is gone. The same `message_left` event, with `cause=broken_pointer`, fires when `s3BucketName` or `s3Key` is missing or empty, and when the body has the size attribute but is not valid JSON. A timeout or an HTTP 5xx raises, does not delete, and emits nothing for itself. Every `message_left` in that batch is still emitted, and payload files for messages that will not be returned are removed, before that error is raised. An exception inside `on_event` is logged and ignored.

* The FIFO queue has a redrive policy with a `deadLetterTargetArn` and a `maxReceiveCount` of at least 1. Send, batch send, and receive on a `.fifo` queue read `RedrivePolicy` and refuse to run when it is missing, empty, lacks either field, or has `maxReceiveCount` of 0. The check is not cached. Someone still has to create the policy. The dead-letter queue must also be FIFO, or SQS rejects the policy. This library checks only the source queue's `RedrivePolicy`. It creates neither queue. A dead-letter queue that nobody reads is not enough. The read needs `sqs:GetQueueAttributes`. That permission is new in 0.5.0 and is breaking for a least-privilege FIFO producer that previously only had `sqs:SendMessage`.

Prefer one role for producers and one for consumers. Scope S3 to the payload prefix. Add the KMS statements only when `server_side_encryption` is a KMS strategy. `sqs:GetQueueAttributes` is required in 0.5.0 for FIFO send, batch send, and receive. A least-privilege FIFO producer that previously only allowed `sqs:SendMessage` must add it or those calls fail closed.

An offloaded body contains a new S3 key, so content-based deduplication will not collapse a retry. `send_message` and `send_message_batch` raise before any S3 upload when the queue name ends in `.fifo` and `message_deduplication_id` was not passed. A batch raises before any entry is uploaded or sent. This library does not invent a deduplication id. A caller-supplied id stops a second copy only inside SQS's 5-minute dedup interval. After that, the same id is a new message.

Before send, batch send, and receive, a `.fifo` queue must have a `RedrivePolicy` with `deadLetterTargetArn` and `maxReceiveCount` of at least 1. The client reads it with `GetQueueAttributes` on every call (the result is not cached) and raises before any upload or receive when the call fails or the policy is missing, empty, incomplete, or has a count of 0. It emits nothing. It checks only that queue, and it creates neither the source queue nor the dead-letter queue. The dead-letter queue must be FIFO or SQS rejects the policy. A standard queue is not checked. `sqs:GetQueueAttributes` is required; a least-privilege FIFO producer that only had `sqs:SendMessage` fails closed. The demo queue `sqs-extended-demo.fifo` is created without a redrive policy; send and receive through this library raise until you set one.

Delete order, `message_left`, and `s3_object_orphaned` are in [Events](events.md). FIFO send and receive rules are in [FIFO and redrive](fifo-and-redrive.md).
