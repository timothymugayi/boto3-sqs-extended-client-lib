# Contributing

pysqs-extended-client uses GitHub to manage reviews of pull requests.

* If you have a trivial fix or improvement, go ahead and create a pull request,
  addressing (with `@...`) the maintainer of this repository (see
  [MAINTAINERS](https://github.com/timothymugayi/boto3-sqs-extended-client-lib/graphs/contributors)).

* If you plan to do something more involved, first discuss your ideas on
  [by creating an issue]. This will avoid unnecessary work and surely give you and
  us a good deal of inspiration.

## Testing

Submitted changes should pass the current tests, and be covered by new test
cases when adding functionality.

* Run the tests locally using [tox] which executes the full suite on all
  supported Python versions installed.

* Each pull request is gated using [Travis CI] with the results linked on the
  github page. This must pass before the change can land, note pushing a new
  change will trigger a retest.

## Style

* Code style should follow [PEP 8] generally, and can be checked by running:
  ``tox -e flake8``.

* Import statements can be automatically formatted using [isort].

[isort]: https://pypi.org/project/isort/
[PEP 8]: https://www.python.org/dev/peps/pep-0008/
[tox]: https://tox.readthedocs.io/en/latest/
[Travis CI]: https://docs.travis-ci.com/
