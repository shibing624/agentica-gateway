from setuptools import setup, find_packages

# Read version from the package to ensure single source of truth
def _get_version():
    import re
    with open("src/__init__.py") as f:
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', f.read())
        return m.group(1) if m else "0.0.0"


setup(
    name="agentica-gateway",
    version=_get_version(),
    description="Agentica Gateway Service - Python OpenClaw",
    python_requires=">=3.10",
    packages=find_packages(),
    install_requires=[
        "agentica>=0.2.0",
        "fastapi>=0.109.0",
        "uvicorn>=0.27.0",
        "websockets>=12.0",
        "lark-oapi>=1.0.0",
        "apscheduler>=3.10.0",
        "python-dotenv>=1.0.0",
        "pydantic>=2.0.0",
        "loguru>=0.7.0",
        "pyyaml>=6.0",
    ],
    extras_require={
        "telegram": ["python-telegram-bot>=20.0"],
        "discord": ["discord.py>=2.0"],
        "test": [
            "pytest>=7.0",
            "pytest-asyncio>=0.23.0",
            "httpx>=0.27.0",
        ],
        "all": [
            "python-telegram-bot>=20.0",
            "discord.py>=2.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "agentica-gateway=src.main:main",
        ],
    },
)
