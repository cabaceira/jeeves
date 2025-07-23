# setup.py
from setuptools import setup, find_packages

setup(
    name="jeeves",
    version="0.1.0",
    description="Rocket.Chat provisioning butler",
    author="Your Name",
    author_email="you@example.com",
    url="https://github.com/your-org/jeeves",
    packages=find_packages(where="."),

    install_requires=[
        "boto3>=1.26.0",
        "PyYAML>=6.0",
         "click>=8.0",
        # …add any other runtime deps your pipelines need…
    ],

    entry_points={
        "console_scripts": [
            # adjust this to point at your CLI entrypoint
            "jeeves=jeeves.cli:main",
        ],
    },
)
