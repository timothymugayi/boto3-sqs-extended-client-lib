# FIFO and redrive

Deduplication ids, the redrive policy checked before send and receive, how long the S3 lifecycle rule has to live, and idempotent handlers.

[README](../README.md) · [Configuration](configuration.md) · [IAM](iam.md) · FIFO and redrive · [Events](events.md) · [Binary and files](binary-and-files.md) · [Java compatibility](java-compatibility.md) · [Migration](migration.md) · [Testing](testing.md)

<a id="s3-lifecycle"></a>

## S3 lifecycle

* The payload prefix has an S3 lifecycle rule whose expiration is longer than the **sum** of the source-queue retention and the dead-letter retention. The dead-letter clock starts when the message arrives there, after it has already sat in the source, so being longer than each retention on its own is not enough. A shorter rule deletes the object while the message can still be read. That is the missing-object loss. Purge leaves the objects. A failed delete after SQS has already succeeded leaves the object. A failed delete after a send that SQS did not accept can leave one too. The library cannot create the rule and cannot remove the object while S3 is down.

Put the rule on the payload prefix, and abort incomplete multipart uploads in the same rule. An example (expire after 14 days, which is only enough when the sum of the two retentions is shorter) is in [Deletes and orphaned objects](events.md#deletes-and-orphaned-objects).

<a id="redrive-policy"></a>

## Redrive policy

* The FIFO queue has a redrive policy with a `deadLetterTargetArn` and a `maxReceiveCount` of at least 1. Send, batch send, and receive on a `.fifo` queue read `RedrivePolicy` and refuse to run when it is missing, empty, lacks either field, or has `maxReceiveCount` of 0. The check is not cached. Someone still has to create the policy. The dead-letter queue must also be FIFO, or SQS rejects the policy. This library checks only the source queue's `RedrivePolicy`. It creates neither queue. A dead-letter queue that nobody reads is not enough. The read needs `sqs:GetQueueAttributes`. That permission is new in 0.5.0 and is breaking for a least-privilege FIFO producer that previously only had `sqs:SendMessage`.

<a id="idempotent-handlers"></a>

## Idempotent handlers

* The handler is idempotent on the instruction id inside the payload, not the SQS message id. A `message_deduplication_id` expires after 5 minutes, and a move to the dead-letter queue is a new message id. This library cannot extend that interval, and it does not invent a deduplication id.

<a id="receive-timeouts"></a>

## Receive timeouts

A timeout during receive still counts as a receive. A message dead-lettered that way is recoverable until the dead-letter retention, as long as the lifecycle rule has not deleted the object. It is not the missing-object loss.

<a id="fifo-queues"></a>

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

