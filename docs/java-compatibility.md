# Java compatibility

The **Amazon SQS Extended Client Library for Python** (version **0.5.1**) is modelled after the original [Amazon SQS Extended Client for Java](https://github.com/awslabs/amazon-sqs-java-extended-client-lib). It stores large SQS payloads in Amazon S3 (over the 256 KB SQS limit) and puts a pointer on the queue.

[README](../README.md) · [Configuration](configuration.md) · [IAM](iam.md) · [FIFO and redrive](fifo-and-redrive.md) · [Events](events.md) · [Binary and files](binary-and-files.md) · Java compatibility · [Migration](migration.md) · [Testing](testing.md)

<a id="pointer-format"></a>

## Pointer format

Overflow or always-through-S3 writes a Java-compatible pointer:

```text
["software.amazon.payloadoffloading.PayloadS3Pointer", {"s3BucketName": "...", "s3Key": "..."}]
```

and a reserved attribute `SQSLargePayloadSize` (or `ExtendedPayloadSize` if configured).

`send_message(queue_url, message)` treats every `str` as text, including a string that happens to be a path to a file on disk. Small messages stay in SQS. Messages over 256 KB (or `always_through_s3=True`) are stored in S3 as UTF-8. Receive of those messages, including ones sent by older versions of this library, returns a `str`. The Java `PayloadS3Pointer` format is unchanged.

`use_legacy_attribute=True` (default, same as Java) writes `SQSLargePayloadSize`. Set it `False` to write `ExtendedPayloadSize`. Receive accepts both.

Large S3 bodies use multipart upload via boto3 `TransferConfig` (8 MB threshold). Text messages keep the Java pointer format. `send_file`, `pathlib.Path`, and binary file objects stream through that same transfer config. A plain string is never treated as a path.

<a id="only-this-library-receives-and-deletes"></a>

## Only this library receives and deletes

* This library is the only receiver and the only deleter on the queue. Java may send text. The Java extended client still deletes the SQS message when the object is missing, and it still deletes the object before the message. If that client receives or deletes, the payment can still disappear, and this library's `message_left` callback does not fire. There is no change inside this repo that stops the Java client.

Receipt-handle markers stay compatible with Java either way, so a Java producer can still send text this client reads. Do not let the Java extended client receive or delete on this queue. It still deletes the SQS message when the object is missing, and it still deletes the object before the message.

<a id="delete-order"></a>

## Delete order

Delete order is **SQS first**, then a best-effort S3 delete. `delete_s3_before_sqs=True` raises `ValueError` when the client is constructed. There is no S3-then-SQS path. The caller is not told the object went first when it did not.

* **`delete_s3_before_sqs=True` (0.5.0).** `SQSClientExtended` raises `ValueError` at construction. The 0.4.1 construction warning is gone, and delete methods are never reached. There is no S3-then-SQS path and no dead-pointer callback. Leave the flag unset so the SQS message is deleted first.

Configure the client **before** constructing it. After `SQSClientExtended` is created the config is frozen (unlike the Java client, which is documented as not thread-safe). `set_always_through_s3` and `set_message_size_threshold` are not part of the API.

`purge_queue` clears SQS only. S3 objects are left behind (same warning as Java).

SQS still has a 256 KB **message** limit; only the payload bytes go to S3. The default receive path loads the payload into memory, same as the Java client. That is a poor fit for multi-hundred-MB objects: pass `payload_dir` (or `get_payload_from_s3(..., filename=...)`) so boto3 streams the object to a file.

<a id="payload-support-and-encryption"></a>

## Payload support and encryption

`payload_support_enabled=True` (default) is Java's payload-support switch. `is_large_payload_support_enabled()` returns that flag. When it is `False`, send does not offload, receive does not hydrate pointers, and delete does not remove S3 objects or strip receipt handles. Set it on the config before constructing the client.

### Server-side encryption

`server_side_encryption` is the analog of Java `ServerSideEncryptionStrategy`. `aws_managed_cmk()` matches Java `AwsManagedCmk`, and `customer_key(...)` matches Java `CustomerKey`. The strategies, the `TypeError` for anything else, and the SSE-C limitation are in [Server-side encryption](configuration.md#server-side-encryption).
