import os

from setuptools import setup, find_packages, Command


with open("README.md", "r") as fh:
    long_description = fh.read()


base_path = os.path.abspath(os.path.dirname(__file__))

# What packages are required for this module to be executed?
try:
    with open(os.path.join(base_path, "requirements.txt"), encoding="utf-8") as f:
        required_packages = f.read().split("\n")
except:
    required_packages = []


class CleanCommand(Command):
    """Custom clean command to tidy up the project root."""
    user_options = []
    def initialize_options(self):
        pass
    def finalize_options(self):
        pass
    def run(self):
        os.system('rm -vrf ./build ./dist ./*.pyc ./*.tgz ./*.egg-info')


setup(
    name="boto3-sqs-extended-client",
    version="0.0.1",
    author="timothy.mugayi",
    author_email="django.course@gmail.com",
    description="Amazon SQS Extended Client Library for Python for sending large payloads that exceed sqs limitations via S3",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/timothymugayi/boto3-sqs-extended-client-lib",
    platforms='any',
    packages=[p for p in find_packages() if not p.startswith('tests')],
    classifiers=[
        'Environment :: Web Environment',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python :: Implementation :: PyPy',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
        'Topic :: Software Development :: Libraries :: Python Modules'
    ],
    zip_safe=True,
    install_requires=required_packages,
    keywords='Amazon SQS Extended Client Library for Python',
    cmdclass={
        'clean': CleanCommand,
    }
)