# EBS — EDA Build System

EBS is a hash-based build orchestrator for EDA flows (Questa simulation and lint first, Synopsys
later) on SLURM. It has a shared content-addressed cache, team releases and provenance.
A step reruns only when its hashed inputs change, and results are shared across users and CI.

> Status: early development (phase 0). Nothing here is usable yet.

- Architecture: [docs/architecture.md](docs/architecture.md)
- Design contracts: [docs/design/](docs/design/overview.md)
- Work items: [docs/tasks/](docs/tasks/README.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)

```sh
uv sync --all-extras
uv run ebs --version
```

Licensed under the [Apache License 2.0](LICENSE).
