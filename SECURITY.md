# Security Policy

## Data Privacy

Penny reads Claude Code session files (`~/.claude/**/*.jsonl` and `stats-cache.json`) locally on your machine. No data is transmitted to external servers or third-party services.

## Dashboard

The HTTP dashboard binds to `127.0.0.1:7432` (localhost only). It is not reachable from outside your machine and requires no authentication because of this network restriction. Do not expose port 7432 via SSH port forwarding, reverse proxies, or similar mechanisms.

## Configuration

`config.yaml` is loaded with `yaml.safe_load()`, which prevents arbitrary Python object instantiation. No config value triggers code execution.

## Installation

`install.sh` only requires user-level permissions — no `sudo` is needed. The launchd service runs as your user account. To verify before running:

```bash
git clone https://github.com/gpxl/penny.git
cd penny
# Review install.sh before executing
bash install.sh
```

## Reporting Vulnerabilities

Please report security issues via [GitHub Issues](https://github.com/gpxl/penny/issues). For sensitive disclosures, use GitHub's private vulnerability reporting feature on the repository's Security tab.
