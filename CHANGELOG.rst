Changelog
=========

0.5.0
-----

Breaking release. A confirmed missing payload stays on the queue, FIFO
offload requires a caller-supplied deduplication id, and a FIFO queue
without a redrive policy is refused. This does not make the library
production ready on its own. The bucket lifecycle rule must be longer
than the sum of the source-queue retention and the dead-letter
retention. The dead-letter queue must be FIFO. This library must be the
only receiver and only deleter. ``sqs:GetQueueAttributes`` is a new IAM permission and is
breaking for least-privilege FIFO producers. An idempotent handler
still has to be in place outside this package.

* **Reverses 0.4.1.** A confirmed S3 not-found (``NoSuchKey``, ``404``, or ``NotFound``) no longer deletes the SQS message, including when ``ignore_payload_not_found`` is set. That flag does not delete. The ``payload_missing_deleted`` callback is gone. The message stays in the queue and, if ``on_event`` is set, the callback is ``message_left`` with ``cause=s3_not_found``. The receipt handle is not returned to the caller. A timeout or an HTTP 5xx raises, does not delete, emits nothing for itself, and is not treated as not-found. The receive is not undone. The whole batch is classified first: every not-found and every broken pointer still emits ``message_left``, and payload files for messages that will not be returned are removed, before the first other error is raised.
* **Reverses 0.4.1.** ``delete_s3_before_sqs=True`` no longer logs a warning and no longer deletes S3 first. ``SQSClientExtended`` raises ``ValueError`` at construction. There is no dead-pointer path and no event for this raise.
* A pointer that is missing ``s3BucketName`` or ``s3Key``, or has an empty value, is not downloaded. A body that has the size attribute but is not valid JSON is the same case. The SQS message is left in place and ``message_left`` is emitted with ``cause=broken_pointer``.
* An inline binary body is sized as the base64 body, plus the caller's attributes, plus 38 bytes for the ``SQSExtendedContentType`` / ``String`` / ``binary-b64`` attribute. Over ``message_size_threshold`` (default 262144), the raw bytes are offloaded with content type ``binary``. Text sizing is unchanged.
* ``message_group_id`` may be sent without ``message_deduplication_id``. On a queue whose name ends in ``.fifo``, a payload that would be offloaded without ``message_deduplication_id`` raises before any S3 upload, on ``send_message`` and on ``send_message_batch``. The batch raises before any entry is uploaded or sent. The library does not invent a deduplication id. An inline FIFO send is attempted; if SQS rejects it, that rejection is not a posted message and nothing is emitted.
* Before ``send_message``, ``send_message_batch``, and ``receive_message`` on a ``.fifo`` queue, the client reads ``RedrivePolicy`` with ``GetQueueAttributes``. It raises if the call fails, the policy is missing or empty, or it lacks ``deadLetterTargetArn`` or a ``maxReceiveCount`` of at least 1. Zero is refused. The send raise happens before any S3 upload. The receive raise happens before the receive call. The result is not cached. Nothing is emitted. The library does not create the queue or the policy, and it checks only the queue it was given. Standard queues are not checked. ``sqs:GetQueueAttributes`` is required on FIFO producers and consumers; a least-privilege producer that only had ``sqs:SendMessage`` will fail closed.
* A failed S3 cleanup emits ``s3_object_orphaned``. ``case=after_sqs_succeeded`` means the SQS delete already succeeded. ``case=after_upload_sqs_rejected`` means the send was a definite rejection and the uploaded object could not be deleted. A definite rejection is a botocore ``ClientError`` with a 4xx status and ``RetryAttempts`` of 0, a client-side validation error raised before the request is sent (``ParamValidationError`` and the same class of check), or a batch ``Failed`` entry with ``SenderFault`` true. A 4xx whose ``RetryAttempts`` is greater than 0 is not definite: boto3 may have stored the first attempt and then been throttled. ``case=after_sqs_uncertain`` means the send outcome is unknown (timeout, connection error, 5xx, a retried 4xx, or a ``Failed`` entry without ``SenderFault`` true): the object is kept for the lifecycle rule and is not deleted. A successful offload, hydrate, or delete does not use ``message_left`` or ``s3_object_orphaned``. The FIFO dedup raise and the redrive raise emit nothing. An exception raised inside ``on_event`` is logged and ignored. It does not drop the rest of the batch and it does not fail a delete that SQS already accepted. A bad inline ``binary-b64`` body is an other error: sibling ``message_left`` events are still emitted before it is raised.

0.4.1
-----

Fail-fast configuration. Invalid buckets, thresholds, credentials, and
encryption strategies are rejected when the config or client is created.

* ``SQSClientExtended`` requires a non-blank ``s3_bucket_name`` at construction when ``payload_support_enabled`` is true (the default). That includes ``always_through_s3=True``, which offloads every payload, and the default mode, which offloads any message larger than ``message_size_threshold``. The missing-bucket ``ValueError`` used to wait until the first offload (``S3 bucket name cannot be null``). Set ``payload_support_enabled=False`` to run without a bucket; that mode does not offload, hydrate, or delete S3 payloads, and ``always_through_s3`` has no effect. A constructor bucket that differs from ``config.s3_bucket_name`` raises ``ValueError`` instead of being ignored. The same name in both places is fine, and a constructor name fills an empty config. Blank names are ``ValueError``; non-strings are ``TypeError``.
* ``message_size_threshold`` must be an int from 1 to 262144 (the SQS 256 KiB limit). ``None`` still selects 262144. Out of range is ``ValueError``. Booleans and other types are ``TypeError``.
* An access key without a secret, or a secret without an access key, raises ``ValueError`` at construction. Those pairs used to be ignored, and boto3 then used the default chain. Region-only construction still uses the default chain. Both keys are passed to boto3 even when region is omitted; they used to be dropped unless region was set too. Whitespace-only key material counts as omitted. Non-string key or region values raise ``TypeError``.
* ``server_side_encryption`` must be a ``ServerSideEncryptionStrategy`` or ``None``. Anything else raises ``TypeError`` at config construction instead of failing later inside ``extra_args`` during upload.
* ``delete_s3_before_sqs=True`` logs one warning at client construction about the dead-pointer risk. The error log when an SQS delete actually fails after the S3 object is removed is unchanged.
* When ``ignore_payload_not_found`` deletes an SQS message whose S3 payload is missing, the client still logs a warning and, if ``on_event`` is set, calls it as ``payload_missing_deleted`` with ``queue_url``, ``s3_bucket_name``, ``s3_key``, and ``receipt_handle``.

0.4.0
-----

Production-readiness release. Pointer JSON, reserved attribute names, and
receipt-handle markers stay compatible with the Java extended client.

* Fixed message-attribute sizing when ``BinaryValue`` is ``bytes`` or ``bytearray`` (normal boto3). The previous code called ``str.encode`` and crashed.
* A plain ``str`` is always message text, even when that string is an existing filesystem path. Upload a file with ``send_file()`` or pass a ``pathlib.Path`` (or any ``os.PathLike``). File-like objects are unchanged.
* Delete the SQS message first, then best-effort delete the S3 object. If the queue delete fails, the payload is still there and the message can be retried. If S3 cleanup fails afterward, the object is orphaned. The Java client deletes S3 first; that leaves a dead pointer when SQS delete fails. Set ``delete_s3_before_sqs=True`` to match Java.
* ``receive_message`` returns ``[]`` when the queue is empty.
* ``receive_message(..., payload_dir=...)`` and ``get_payload_from_s3(..., filename=...)`` stream an S3 object to disk. The default receive path still loads the body into memory.
* If S3 upload succeeds and the SQS send fails, the client tries to delete that object. Configure an S3 lifecycle rule anyway: cleanup can fail, and ``purge_queue`` does not delete payloads.
* Optional S3 server-side encryption via ``server_side_encryption=`` (``aws_managed_cmk()``, ``customer_key(key_id)``, ``sse_s3()``), analogous to Java ``ServerSideEncryptionStrategy``.
* ``payload_support_enabled`` (default ``True``) gates offload, hydrate, and S3 delete. ``is_large_payload_support_enabled()`` returns that flag. There is no post-init setter.
* Removed ``set_always_through_s3`` and ``set_message_size_threshold``. Set ``ExtendedClientConfiguration`` before constructing the client. Mutating a frozen config still raises ``FrozenConfigError``.
* AWS access key and secret are passed to boto3 at construction and are not stored on the client. Omit them to use the default credential chain, or inject ``sqs_client`` / ``s3_client``.
* Packaging metadata matches the Apache-2.0 ``LICENSE``. ``python_requires`` is ``>=3.9``. The wheel is Python 3 only.

0.3.0
-----

* Stream binary payloads and large files to S3. Text messages stay strings. Added an optional S3 canned ACL.

0.2.0
-----

* Java-parity extended client, worker-scale settings, LocalStack tests, and GitHub Actions CI.

0.0.1
-----

* Initial PyPI release. That is the only version currently published; see GitHub issue #19.
