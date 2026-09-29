Changelog
=========

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
