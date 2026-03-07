# Security Policy

## Autonomous Agent Warning

Penny spawns Claude Code agents with `--dangerously-skip-permissions`, which means agents skip Claude's interactive permission prompts. This is intentional for unattended operation.

In practice, agents can read and write files in the configured project directory, run shell commands, create git branches, commit code, and open pull requests — all without asking for confirmation. This happens automatically whenever Penny's capacity thresholds are met.

**Mitigations:**

- Scope projects carefully in `config.yaml`. Only add directories you are comfortable having an autonomous agent modify.
- Review all `bd ready` tasks in each project before adding it to your Penny config. Only tasks marked "ready" are eligible for spawning.
- Start with conservative thresholds (`trigger.min_capacity_percent`, `trigger.max_days_remaining`) and `work.max_agents_per_run: 1` until you are comfortable with the behaviour.

## Credential Isolation

The `_ENV_PASSTHROUGH` allowlist in `penny/spawner.py` controls which environment variables are inherited by spawned agents. Only safe, non-credential variables are passed through:

```
HOME, PATH, USER, SHELL, LANG, LC_ALL, TERM, TMPDIR, XDG_RUNTIME_DIR
```

Variables containing credentials — including `ANTHROPIC_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `DATABASE_URL`, `GITHUB_TOKEN`, and `GH_TOKEN` — are **not** passed to spawned agents, even if they exist in the parent environment.

## Dashboard API

The HTTP dashboard server binds to `127.0.0.1:7432` (localhost only). It is not reachable from outside your machine, and no authentication is required because of this network restriction.

Do not expose port 7432 via SSH port forwarding, reverse proxies, or similar mechanisms. There is no built-in authentication — anyone who can reach the port can control Penny.

## Config File Trust

`config.yaml` is loaded with `yaml.safe_load()`, which prevents arbitrary Python object instantiation from config files. However, project paths listed in config are passed as working directories for spawned agents. Only add projects you trust to your config.

## Temporary Files

Prompt and shell runner scripts are created with `tempfile.mkstemp()` and immediately `chmod`-ed to `0o600` (owner read/write only). Other users on the same machine cannot read agent prompts. Temporary files self-delete after 30 seconds.

## Install Script

`install.sh` is designed to be piped from curl. To verify before running:

```bash
git clone https://github.com/gpxl/penny.git
cd penny
# Review install.sh before executing
bash install.sh
```

The script only requires user-level permissions — no `sudo` is needed.

## Reporting Vulnerabilities

Please report security issues via [GitHub Issues](https://github.com/gpxl/penny/issues). For sensitive disclosures, use GitHub's private vulnerability reporting feature on the repository's Security tab.
