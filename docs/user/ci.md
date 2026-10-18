# EBS in GitLab CI

A pipeline runs `ebs build --ci`. GitLab then shows the simulation results in the merge request,
links the build, and can gate promotion of a channel. Start from the template in
[`examples/ci/.gitlab-ci.yml`](../../examples/ci/.gitlab-ci.yml).

## Setup

1. **Runner.** Use a GitLab runner on a login node (or any host that can reach SLURM, the CAS
   and the metadata service) with `ebs` on its `PATH`, and tag it, e.g. `ebs-login`.
2. **Service account token.** Each team's CI runs as that team's service account, so SLURM
   accounting, fair-share and the action cache all see CI as the team. Ask the EBS admins for a
   token, then add it to the GitLab project as the CI/CD variable `EBS_META_TOKEN`, **masked**
   and **protected**. `ebs build --ci` refuses to start against the metadata service without
   one, because CI runners have no MUNGE. You can use `[metadata].token_file` (mode 600)
   instead.
3. **Optional: build links.** In the runner's `ebs.toml`:

   ```toml
   [ci]
   build_url = "https://ebs.example/builds/{uuid}"   # {build} = numeric id, {uuid}

   [slurm.qos]
   ci = "ci-high"                                   # the QOS of CI builds
   ```

## What `--ci` does

| | |
| --- | --- |
| Cache | `--cache write`: CI results feed the shared cache. A different `--cache` (or `--no-cache`) is an error |
| Hashing | `--rehash`: every source file is hashed, the stat cache is not trusted |
| Output | One line per event, even on a terminal (no live table) |
| SLURM | The `[slurm.qos].ci` QOS (once `ebs build` runs on the SLURM executor; today it runs locally) |
| Metadata | `CI_PROJECT_PATH`, `CI_PIPELINE_ID`, `CI_JOB_URL` and `CI_COMMIT_SHA` are stored with the build (`build.json`, the `build_started` event, and the build's `ci_job`) |

When the build ends, whether it passed or not, ebs writes three files into `$CI_PROJECT_DIR`:

- **`ebs-junit.xml`**: one test case per simulation action (any action with a `test` param).
  The class name is the step and the name is `test[seed]`. A failure carries the first error
  signature, and the output carries the last 16 KiB of the log. Results reused from the cache
  have the property `ebs.cached=true`. Declare it under `artifacts:reports:junit`.
- **`ebs-summary.md`**: status, build link, per-step counts and the first 20 failed tests.
- **`ebs.env`**: `EBS_BUILD_ID=<id>`. Declared as a `dotenv` report, it hands the build id to
  later jobs.

Secrets never reach these files. The values of `EBS_META_TOKEN`, `CI_JOB_TOKEN` and GitLab's
other job credentials are replaced by `[MASKED]`, even when a tool printed them into its log.

## Gating a channel

```shell
ebs channel set rtl-team/cpu stable 2026.10.1 --if-passing
```

The channel moves only if the gate build passed and is in the release's domain. The gate build
is `$EBS_BUILD_ID` (from the build job's `ebs.env`) or `--gate-build ID`. If the channel also
has a `[release.gates]` policy, the build must also be a passed build of the gate flow that
consumed the release. Otherwise the command fails without moving the channel, and so does the
pipeline. With direct database access the move records the gate build (`ebs channel history`).
Through the metadata service, the "passed" check runs in `ebs` and the service records the gate
its own `[release.gates]` policy finds.
