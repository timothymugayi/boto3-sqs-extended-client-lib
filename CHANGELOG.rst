Changelog
=========

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
