# Configuration

Client options, fail-fast startup checks, injected boto3 clients, worker-thread settings, and pass-through SQS calls.

[README](../README.md) · Configuration · [IAM](iam.md) · [FIFO and redrive](fifo-and-redrive.md) · [Events](events.md) · [Binary and files](binary-and-files.md) · [Java compatibility](java-compatibility.md) · [Migration](migration.md) · [Testing](testing.md)

<a id="usage"></a>

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

<a id="configuration-and-injected-clients"></a>

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

<a id="fail-fast-checks"></a>

### Fail-fast checks (0.4.1)

These run when `ExtendedClientConfiguration` or `SQSClientExtended` is created.

* **Bucket.** With `payload_support_enabled=True` (the default), a send can offload: every message if `always_through_s3=True`, otherwise any message larger than `message_size_threshold`. Construction raises `ValueError` when the effective bucket is missing. That error used to wait until the first offload (`S3 bucket name cannot be null`). A constructor name that differs from `config.s3_bucket_name` raises `ValueError` instead of ignoring the constructor. The same name in both places is fine; a constructor name fills an empty config. Whitespace-only is `ValueError`. A non-string is `TypeError`.

  Small-only processes that never offload, hydrate, or delete S3 payloads omit the bucket by setting `payload_support_enabled=False`. That is the supported no-bucket mode. `always_through_s3` does nothing there. Receive-only workers that hydrate pointers should still set `s3_bucket_name`. Receive reads the bucket from the pointer; the init check exists so a later send cannot fail on a missing bucket.

* **`message_size_threshold`.** An int from 1 through 262144 (the SQS 256 KiB limit), default 262144. `None` selects the default. Outside that range is `ValueError`. Booleans and other types are `TypeError`. A larger threshold would try to send a body SQS rejects.

* **Credentials.** Set `aws_access_key_id` and `aws_secret_access_key` together, or omit both. One without the other raises `ValueError`. A half-specified pair used to be dropped, and boto3 used the default chain. Region alone still uses that chain. Both keys are passed even when region is omitted; they used to be dropped unless region was set. Whitespace-only values count as omitted. Non-string key or region values raise `TypeError`.

* **`server_side_encryption`.** `None` or a `ServerSideEncryptionStrategy` (`aws_managed_cmk()`, `customer_key(...)`, `sse_s3()`). Anything else is `TypeError` at config construction. A wrong object used to fail later, when the upload called `extra_args`.

* **`delete_s3_before_sqs=True` (0.5.0).** `SQSClientExtended` raises `ValueError` at construction. The 0.4.1 construction warning is gone, and delete methods are never reached. There is no S3-then-SQS path and no dead-pointer callback. Leave the flag unset so the SQS message is deleted first.

* **`ignore_payload_not_found` (0.5.0).** The flag does not delete the SQS message. A confirmed S3 not-found leaves the message in the queue and, if `on_event` is set, calls it as `message_left` with `cause=s3_not_found`. The 0.4.1 `payload_missing_deleted` callback is gone. The same `message_left` event, with `cause=broken_pointer`, fires when `s3BucketName` or `s3Key` is missing or empty, and when the body has the size attribute but is not valid JSON. A timeout or an HTTP 5xx raises, does not delete, and emits nothing for itself. Every `message_left` in that batch is still emitted, and payload files for messages that will not be returned are removed, before that error is raised. An exception inside `on_event` is logged and ignored.

<a id="server-side-encryption"></a>

### Server-side encryption

`server_side_encryption` is the analog of Java `ServerSideEncryptionStrategy`. It must be one of the strategies below (or omitted). Other objects raise `TypeError` when the config is created. It is merged into S3 `ExtraArgs` next to `s3_canned_acl`:

* `aws_managed_cmk()` — SSE-KMS with the AWS-managed `aws/s3` key (`ServerSideEncryption=aws:kms`), same as Java `AwsManagedCmk`.
* `customer_key("alias/my-key")` or a key id — SSE-KMS with that CMK, same as Java `CustomerKey`.
* `sse_s3()` — SSE-S3 (`AES256`). The Java factory does not expose this; it is here because boto3 callers use it.

Downloads of SSE-KMS objects use the caller's IAM `kms:Decrypt`. The client does not implement customer-provided keys (SSE-C).

<a id="sharing-a-client-across-worker-threads"></a>

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

<a id="batch-and-pass-through-apis"></a>

### Batch and pass-through APIs

S3-aware methods: `send_message`, `send_file`, `send_message_batch`, `receive_message`, `delete_message`, `delete_message_batch`, `change_message_visibility`, `change_message_visibility_batch`, `purge_queue`.

`purge_queue` clears SQS only; S3 objects are left behind (same warning as Java). See the [lifecycle note](events.md#deletes-and-orphaned-objects).

Other SQS calls (`create_queue`, `get_queue_url`, `list_queues`, `tag_queue`, …) are forwarded to the underlying boto3 SQS client, so this wrapper can be the only SQS object you hold. Extra boto3 fields such as `DelaySeconds` and `VisibilityTimeout` are accepted on send/receive.

Call `sqs.close()` when the process is shutting down to stop the bounded S3 thread pool. A send after `close()` starts a new S3 thread pool. `close()` does not turn off send-safety. It does not unregister the before-send handler and does not clear the attempt counter, so a send started after `close()`, and a send already in flight when `close()` runs, still records `before-send` and still leaves the S3 object in place when the outcome is uncertain. One handler is registered per SQS client and stays registered for that SQS client's lifetime. This is not a `boto3.resource("sqs")` replacement.

Send and receive of large bodies, FIFO deduplication, and redrive checks are in [Binary and files](binary-and-files.md), [FIFO and redrive](fifo-and-redrive.md), and [Events](events.md).
