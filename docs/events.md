# Events

`message_left` and `s3_object_orphaned`: when they fire, and the delete order that produces them.

[README](../README.md) · [Configuration](configuration.md) · [IAM](iam.md) · [FIFO and redrive](fifo-and-redrive.md) · Events · [Binary and files](binary-and-files.md) · [Java compatibility](java-compatibility.md) · [Migration](migration.md) · [Testing](testing.md)

<a id="what-to-alert-on"></a>

## What to alert on

`s3_offload`, `s3_hydrate`, and `s3_delete` are debug metrics. `message_left` and `s3_object_orphaned` are the failure signals. A raise inside the callback is logged and ignored.

A successful offload, hydrate, or delete does not use `message_left` or `s3_object_orphaned`. The FIFO deduplication raise and the redrive raise emit nothing: both happen before any upload. An exception raised inside `on_event` is logged and ignored.

<a id="message-left"></a>

### message_left

* `cause=s3_not_found`. A confirmed S3 not-found leaves the message in the queue and, if `on_event` is set, calls it as `message_left` with `cause=s3_not_found`. The flag `ignore_payload_not_found` does not delete the SQS message. The 0.4.1 `payload_missing_deleted` callback is gone.
* `cause=broken_pointer`. The same `message_left` event, with `cause=broken_pointer`, fires when `s3BucketName` or `s3Key` is missing or empty, and when the body has the size attribute but is not valid JSON.
* A timeout or an HTTP 5xx raises, does not delete, and emits nothing for itself. Every `message_left` in that batch is still emitted, and payload files for messages that will not be returned are removed, before that error is raised.

<a id="s3-object-orphaned"></a>

### s3_object_orphaned

* `case=after_sqs_succeeded`. The SQS delete succeeds and the S3 delete fails. The queue message is gone and the object is left in the bucket. The SQS delete still stands. The event means the object is still in the bucket. It does not mean a lifecycle rule removed it.
* `case=after_upload_sqs_rejected`. SQS definitely rejected the send and the best-effort delete of the uploaded object failed. A definite rejection is a botocore `ClientError` whose operation is `SendMessage` or `SendMessageBatch`, whose HTTP status is 4xx, and whose `RetryAttempts` is explicitly 0; a batch `Failed` entry with `SenderFault` true; or any other exception when this call never emitted `before-send` for `SendMessage` or `SendMessageBatch`. That last case is a failure before any HTTP attempt, including `ParamValidationError` and `NoCredentialsError` raised while signing the first attempt. The client records `before-send.sqs.SendMessage` and `before-send.sqs.SendMessageBatch`. A 4xx whose `RetryAttempts` key is missing is not explicit zero. A 4xx from another operation, such as STS `AssumeRole` during a credential refresh, is not an SQS rejection. An injected client with no `meta.events` cannot record `before-send`; there, only `ParamValidationError`, `ValidationError`, `MissingParametersError`, `NoCredentialsError`, and `UnknownCredentialError` count as definite among non-`ClientError` failures. The same delete runs for S3 objects uploaded in a batch whose offload fails before any send.
* `case=after_sqs_uncertain`. botocore re-signs, and may refresh credentials, before every retry. The refresh callback can raise any exception after attempt 1 already reached SQS, including `NoCredentialsError` and `ParamValidationError`. When `before-send` already ran for this call, those errors leave the object in place. A timeout, a connection error (including `EndpointConnectionError`), an HTTP 5xx, a 4xx whose `RetryAttempts` is missing or greater than 0, a 4xx whose operation is not `SendMessage` or `SendMessageBatch`, or a batch `Failed` entry without `SenderFault` true does too. boto3 can also surface a throttled 400 after SQS stored the first attempt. The object is left for the lifecycle rule, and a raised send error is re-raised. An object is also left behind if the process dies between the S3 put and the SQS send, or if `purge_queue` is used.

<a id="deletes-and-orphaned-objects"></a>

### Deletes and orphaned objects

Delete order is **SQS first**, then a best-effort S3 delete. `delete_s3_before_sqs=True` raises `ValueError` when the client is constructed. There is no S3-then-SQS path. The caller is not told the object went first when it did not.

* If `DeleteMessage` fails, the S3 object is still there. Retry the delete after the visibility timeout.
* If the SQS delete succeeds and the S3 delete fails, the queue message is gone and the object is left in the bucket. `on_event` is called as `s3_object_orphaned` with `case=after_sqs_succeeded`. The SQS delete still stands. The event means the object is still in the bucket. It does not mean a lifecycle rule removed it.

Receipt-handle markers stay compatible with Java either way, so a Java producer can still send text this client reads. Do not let the Java extended client receive or delete on this queue. It still deletes the SQS message when the object is missing, and it still deletes the object before the message.

`purge_queue` clears SQS only. S3 objects are left behind.

If `send_message` uploads to S3 and SQS definitely rejects the send, the client tries to delete that object. A definite rejection is a botocore `ClientError` whose operation is `SendMessage` or `SendMessageBatch`, whose HTTP status is 4xx, and whose `RetryAttempts` is explicitly 0; a batch `Failed` entry with `SenderFault` true; or any other exception when this call never emitted `before-send` for `SendMessage` or `SendMessageBatch`. That last case is a failure before any HTTP attempt, including `ParamValidationError` and `NoCredentialsError` raised while signing the first attempt. The client records `before-send.sqs.SendMessage` and `before-send.sqs.SendMessageBatch`. A 4xx whose `RetryAttempts` key is missing is not explicit zero. A 4xx from another operation, such as STS `AssumeRole` during a credential refresh, is not an SQS rejection. An injected client with no `meta.events` cannot record `before-send`; there, only `ParamValidationError`, `ValidationError`, `MissingParametersError`, `NoCredentialsError`, and `UnknownCredentialError` count as definite among non-`ClientError` failures. That cleanup is best-effort. If it fails, `on_event` is called as `s3_object_orphaned` with `case=after_upload_sqs_rejected`. The same delete runs for S3 objects uploaded in a batch whose offload fails before any send.

botocore re-signs, and may refresh credentials, before every retry. The refresh callback can raise any exception after attempt 1 already reached SQS, including `NoCredentialsError` and `ParamValidationError`. When `before-send` already ran for this call, those errors leave the object in place. A timeout, a connection error (including `EndpointConnectionError`), an HTTP 5xx, a 4xx whose `RetryAttempts` is missing or greater than 0, a 4xx whose operation is not `SendMessage` or `SendMessageBatch`, or a batch `Failed` entry without `SenderFault` true does too. boto3 can also surface a throttled 400 after SQS stored the first attempt. The object is left for the lifecycle rule, `on_event` is called as `s3_object_orphaned` with `case=after_sqs_uncertain`, and a raised send error is re-raised. An object is also left behind if the process dies between the S3 put and the SQS send, or if `purge_queue` is used.

A confirmed missing object, a pointer missing `s3BucketName` or `s3Key`, or a size-attribute body that is not valid JSON does not delete the SQS message. `on_event` is `message_left`. A successful offload, hydrate, or delete does not use `message_left` or `s3_object_orphaned`. The FIFO deduplication raise and the redrive raise emit nothing: both happen before any upload. An exception raised inside `on_event` is logged and ignored.

Put a lifecycle rule on the payload prefix so leftovers expire. Abort incomplete multipart uploads in the same rule. The expiration must be longer than the sum of the source-queue retention and the dead-letter retention (see [Required before production](../README.md#required-before-production) and [S3 lifecycle](fifo-and-redrive.md#s3-lifecycle)). Example (expire payloads after 14 days, which is only enough when that sum is shorter):

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

