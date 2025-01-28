"""Setup module for aiowebostv."""

from pathlib import Path

from setuptools import setup

PROJECT_DIR = Path(__file__).parent.resolve()
README_FILE = PROJECT_DIR / "README.md"
VERSION = "0.6.1"


setup(
    name="aiowebostv",
    version=VERSION,
    url="https://github.com/home-assistant-libs/aiowebostv",
    download_url="https://github.com/home-assistant-libs/aiowebostv",
    author="Home Assistant Team",
    author_email="hello@home-assistant.io",
    description="Library to control webOS based LG TV devices",
    long_description=README_FILE.read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    packages=["aiowebostv"],
    python_requires=">=3.11",
    package_data={"aiowebostv": ["py.typed"]},
    install_requires=["aiohttp>=3.11"],
    include_package_data=True,
    zip_safe=False,
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Natural Language :: English",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Topic :: Home Automation",
        "License :: OSI Approved :: Apache Software License",
    ],
)
