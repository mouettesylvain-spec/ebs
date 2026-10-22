"""The metadata service with seeded, synthetic data for the dashboard's Playwright tests (P2-06).

    uv run python scripts/dashboard_e2e_server.py --port 8765 --dist web/dist --tmp /tmp/x

It serves the built dashboard at /ui/ over an in-memory store, with the real routes, domain
authorization (fake group lookups) and session cookies. Only this script adds the `/e2e/*`
helpers: `GET /e2e/login?user=NAME` sets a session cookie as the OIDC callback would, and
`POST /e2e/live` advances the running build by one scripted step (events, live log). The data
is invented: no real designs, tools, hosts or licenses.
"""

from __future__ import annotations

import argparse
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import RedirectResponse

from ebs.cas.fs import FsCAS
from ebs.core.clock import SystemClock
from ebs.core.digest import Digest, hash_bytes
from ebs.domains import Domains
from ebs.meta.api import (
    ActionRow,
    BuildCreate,
    BuildId,
    Event,
    EventType,
    OutputResult,
    ReleaseCreate,
    ReleaseOutput,
    ResourceUsage,
    ResultManifest,
    RevalidationCreate,
    RunnerInfo,
)
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.app import create_app
from ebs.meta.service.auth import ChainAuthenticator, Policy
from ebs.meta.service.authz import DomainPolicy
from ebs.meta.service.deps import ServiceDeps
from ebs.meta.service.logs import LIVE_LOG_NAME, LogSource
from ebs.meta.service.oidc import CSRF_COOKIE, SESSION_COOKIE, SessionAuthenticator, SessionSigner
from ebs.runner.collect import debug_action_dir

A, B = "dom-a", "dom-b"
GROUPS = {"alice": {"grp-a"}, "bob": {"grp-b"}, "dave": set()}
TESTS = ("t_alu", "t_fpu", "t_lsu")
SEEDS = (1, 2, 3)
LIB_FEATURE, SIM_FEATURE = "simfeat", "addonfeat"  # invented license feature names


class Groups:
    def groups_of(self, user: str) -> frozenset[str]:
        return frozenset(GROUPS.get(user, set()))


class Ready:
    def check(self) -> str | None:
        return None


def out(data: bytes, *, deterministic: bool = True) -> OutputResult:
    d = hash_bytes(data)
    return OutputResult(
        digest=d, id=d if deterministic else hash_bytes(b"nd:" + data), type="file", size=len(data)
    )


@dataclass
class Seeder:
    store: InMemoryMetadataStore
    cas_root: Path
    debug_root: Path
    t: datetime = field(default_factory=lambda: datetime.now(UTC) - timedelta(hours=3))

    def tick(self, seconds: int = 30) -> datetime:
        self.t += timedelta(seconds=seconds)
        return self.t

    def build(self, domain: str, user: str, project: str) -> BuildId:
        return self.store.create_build(
            BuildCreate(
                uuid=uuid4(), domain=domain, project=project, plan_digest=hash_bytes(b"plan"),
                flow_repo="https://git.example.invalid/demo/flow.git",
                flow_commit="0123456789abcdef0123456789abcdef01234567", flow_dirty=False,
                user_name=user, cache_mode="write",
            )
        )  # fmt: skip

    def emit(self, build: BuildId, type_: EventType, action: str | None, **data: object) -> None:
        self.store.emit(Event(ts=self.tick(), build=build, type=type_, action_id=action, data=data))

    def log(self, domain: str, text: str) -> Digest:
        return FsCAS(self.cas_root, domain).put_bytes(text.encode())

    def result(
        self,
        build: BuildId,
        action: str,
        *,
        passed: bool,
        inputs: dict[str, Digest],
        outputs: dict[str, OutputResult],
        cpu_s: int,
        summary: dict[str, str | int] | None = None,
        log: Digest | None = None,
        cached: bool = False,
    ) -> None:
        key = hash_bytes(f"key:{action}:{sorted(inputs.items())}".encode())
        self.store.set_action_state(build, action, "cached" if cached else "running", key=key)
        self.store.record_result(
            build,
            action,
            ResultManifest(
                action_key=key, status="passed" if passed else "failed",
                exit_code=0 if passed else 1, inputs=inputs, outputs=outputs, log=log,
                summary=summary or {},
                resources=ResourceUsage(max_rss_kb=1024, cpu_s=cpu_s, wall_s=cpu_s + 5),
                runner=RunnerInfo(version="0", host="node-a"),
            ),
        )  # fmt: skip
        if not cached:
            self.store.set_action_state(build, action, "done" if passed else "failed")


@dataclass(frozen=True)
class Seeded:
    regression: BuildId
    live: BuildId
    live_action: str
    other: BuildId
    live_log: Path


def seed(s: Seeder) -> Seeded:
    """A finished regression build with a release (dom-a), a running build (dom-a) and another
    domain's build (dom-b)."""
    store = s.store
    reg = s.build(A, "alice", "rv-core")
    steps = {"compile": [], "elab": ["compile"], "sim": ["elab"], "lint": []}
    rows = [
        ActionRow(action_id="compile[core]", step="compile", rule="questa.compile"),
        ActionRow(action_id="compile[periph]", step="compile", rule="questa.compile"),
        ActionRow(action_id="elab", step="elab", rule="questa.opt"),
        ActionRow(action_id="lint", step="lint", rule="make"),
        *(
            ActionRow(
                action_id=f"sim[test={t},seed={n}]", step="sim", rule="questa.sim",
                params={"test": t, "seed": str(n)},
            )
            for t in TESTS
            for n in SEEDS
        ),
    ]  # fmt: skip
    store.add_actions(reg, rows)
    s.emit(reg, "build_started", None, uuid=str(uuid4()), user="alice", cache_mode="write")
    s.emit(reg, "plan_ready", None, plan=str(hash_bytes(b"plan")), actions=len(rows), steps=steps)
    src = hash_bytes(b"rtl sources")
    core, periph = out(b"worklib core", deterministic=False), out(b"worklib periph")
    s.emit(reg, "cache_hit", "compile[core]", key="k", status="passed")
    s.result(reg, "compile[core]", passed=True, inputs={"src": src}, outputs={"worklib": core},
             cpu_s=5400, cached=True)  # fmt: skip
    s.emit(reg, "submitted", "compile[periph]", job_id="101", attempt=1, batch_size=1)
    s.emit(reg, "pending", "compile[periph]", reason="licenses", licenses=[LIB_FEATURE])
    s.emit(reg, "running", "compile[periph]", job_id="101")
    s.result(reg, "compile[periph]", passed=True, inputs={"src": src},
             outputs={"worklib": periph}, cpu_s=1200)  # fmt: skip
    s.emit(reg, "finished", "compile[periph]", state="done", exit_code=0)
    model = out(b"optimized model", deterministic=False)
    s.result(reg, "elab", passed=True, inputs={"libs/core": core.id, "libs/periph": periph.id},
             outputs={"model": model}, cpu_s=600)  # fmt: skip
    s.emit(reg, "finished", "elab", state="done", exit_code=0)
    s.result(reg, "lint", passed=True, inputs={"src": src}, outputs={}, cpu_s=60, cached=True)
    s.emit(reg, "cache_hit", "lint", key="k2", status="passed")
    failing = {("t_lsu", 2), ("t_lsu", 3), ("t_fpu", 1)}
    for t in TESTS:
        for n in SEEDS:
            a = f"sim[test={t},seed={n}]"
            s.emit(reg, "submitted", a, job_id=f"2{n}", attempt=1, batch_size=9)
            s.emit(reg, "pending", a, reason="licenses", licenses=[SIM_FEATURE, LIB_FEATURE])
            s.tick(120)
            if (t, n) == ("t_alu", 3):  # an infrastructure failure: no result
                s.emit(reg, "running", a, job_id=f"2{n}")
                store.set_action_state(reg, a, "running", key=hash_bytes(a.encode()))
                store.set_action_state(reg, a, "infra_failed", infra_reason="node_fail")
                s.emit(reg, "finished", a, state="infra_failed", reasons=["node_fail"])
                continue
            s.emit(reg, "running", a, job_id=f"2{n}")
            bad = (t, n) in failing
            first = (
                f"UVM_ERROR tb/scb.sv({40 + n}) @ {1000 * n}: top.env.scb [SCB] data mismatch"
                if t == "t_lsu"
                else f"** Fatal: timeout after {500 + n} us"
            )
            body = "".join(
                f"# UVM_INFO tb/seq.sv(10) @ {i}: top [SEQ] item {i}\n" for i in range(3000)
            )
            text = f"# vsim -c top +UVM_TESTNAME={t} -sv_seed {n}\n" + body
            text += f"# {first}\n# ** Note: $finish\n" if bad else "# ** Note: $finish\n"
            sig = None
            if bad:
                sig = (
                    "UVM_ERROR <path>(<n>) @ <n>: top.env.scb [SCB] data mismatch"
                    if t == "t_lsu"
                    else "** Fatal: timeout after <n> us"
                )
            summary: dict[str, str | int] = {
                "test": t,
                "seed": n,
                "status": "FAILED" if bad else "PASSED",
            }
            if sig:
                summary["signature"] = sig
            s.result(reg, a, passed=not bad, inputs={"model": model.id},
                     outputs={"result": out(f"result {t} {n}".encode())}, cpu_s=300,
                     summary=summary, log=s.log(A, text))  # fmt: skip
            s.emit(reg, "finished", a, state="failed" if bad else "done", exit_code=int(bad))
    store.finish_build(reg, "failed")
    s.emit(reg, "build_finished", None, status="failed", counts={})
    release = store.create_release(
        ReleaseCreate(
            domain=A, name="rtl/core", version="2026.10.1", build=reg,
            outputs={"compile[core].worklib": ReleaseOutput(
                action_id="compile[core]", output="worklib", **core.model_dump())},
            notes="Synthetic release for the dashboard tests.",
        ),
        by="alice",
    )  # fmt: skip
    store.set_channel(A, "rtl/core", "stable", release.id, by="alice")
    store.record_revalidation(  # P3-05: a quarterly re-run on a newer simulator
        release.id,
        RevalidationCreate(
            build=reg, verdict="differences", report=hash_bytes(b"revalidation report"),
            markdown=hash_bytes(b"revalidation report.md"),
            toolchains={"questa": "questa/2026.1"},
            counts={"equivalent": 7, "changed": 1, "incomparable": 0},
        ),
        by="ebs-revalidate",
    )  # fmt: skip

    live = s.build(A, "alice", "rv-core")
    store.add_actions(
        live,
        [
            ActionRow(action_id="compile[core]", step="compile", rule="questa.compile"),
            ActionRow(action_id="sim[test=t_live,seed=1]", step="sim", rule="questa.sim",
                      params={"test": "t_live", "seed": "1"}),
        ],
    )  # fmt: skip
    s.emit(live, "build_started", None, uuid=str(uuid4()), user="alice", cache_mode="write")
    s.emit(live, "plan_ready", None, plan="p", actions=2, steps={"compile": [], "sim": ["compile"]})
    s.emit(live, "cache_hit", "compile[core]", key="k", status="passed")
    s.result(live, "compile[core]", passed=True, inputs={"src": src}, outputs={"worklib": core},
             cpu_s=5400, cached=True)  # fmt: skip
    sim = "sim[test=t_live,seed=1]"
    store.set_action_state(live, sim, "running", key=hash_bytes(b"live sim"))
    s.emit(live, "running", sim, job_id="301")
    live_log = debug_action_dir(s.debug_root, A, str(live), sim) / LIVE_LOG_NAME
    live_log.parent.mkdir(parents=True, exist_ok=True)
    live_log.write_text("# vsim -c top +UVM_TESTNAME=t_live\n# UVM_INFO started\n")

    other = s.build(B, "bob", "secret-soc")
    store.add_actions(other, [ActionRow(action_id="lint", step="lint")])
    return Seeded(reg, live, sim, other, live_log)


def make_app(dist: Path, tmp: Path) -> FastAPI:
    cas_root, debug_root = tmp / "cas", tmp / "debug"
    for d in (A, B):
        (debug_root / d).mkdir(parents=True, exist_ok=True)
    store = InMemoryMetadataStore(clock=SystemClock())
    seeded = seed(Seeder(store, cas_root, debug_root))
    signer = SessionSigner([secrets.token_bytes(32)], SystemClock())
    domains = Domains.from_config(
        {"domains": {A: {"unix_group": "grp-a"}, B: {"unix_group": "grp-b"}}}
    )
    deps = ServiceDeps(
        store=store,
        authenticator=ChainAuthenticator(SessionAuthenticator(signer)),
        readiness=Ready(),
        authorize=DomainPolicy(Policy(), domains, [A, B], Groups()),
        logs=LogSource(lambda _d: cas_root, lambda _d: debug_root),
    )
    app = create_app(deps, dashboard_dir=dist)
    lock = threading.Lock()
    script = _live_script(store, seeded)

    @app.get("/e2e/login", include_in_schema=False)
    def login(user: str) -> Response:
        csrf = secrets.token_urlsafe(16)
        r = RedirectResponse("/ui/", status_code=303)
        r.set_cookie(
            SESSION_COOKIE,
            signer.sign({"user": user, "csrf": csrf}, 3600),
            path="/",
            httponly=True,
            samesite="lax",
        )
        r.set_cookie(CSRF_COOKIE, csrf, path="/", samesite="lax")
        return r

    @app.get("/e2e/ids", include_in_schema=False)
    def ids() -> dict[str, object]:
        return {
            "regression": seeded.regression,
            "live": seeded.live,
            "live_action": seeded.live_action,
            "other": seeded.other,
        }

    @app.post("/e2e/live", include_in_schema=False)
    def advance() -> dict[str, str]:
        with lock:
            what = next(script, "done")
        deps.notify(seeded.live, A)
        return {"step": what}

    return app


def _live_script(store: InMemoryMetadataStore, seeded: Seeded) -> Iterator[str]:
    """The running build's next steps: more live log, then the sim finishes, then the build."""
    build, sim = seeded.live, seeded.live_action
    with seeded.live_log.open("a") as f:
        f.write(
            "# UVM_INFO more output\n# UVM_ERROR tb/live.sv(7) @ 42: top [LIVE] late mismatch\n"
        )
    yield "log"
    store.set_action_state(build, sim, "failed")
    store.emit(Event(ts=datetime.now(UTC), build=build, type="finished", action_id=sim,
                     data={"state": "failed", "exit_code": 1}))  # fmt: skip
    store.finish_build(build, "failed")
    store.emit(
        Event(ts=datetime.now(UTC), build=build, type="build_finished", data={"status": "failed"})
    )
    yield "finished"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--tmp", type=Path, required=True)
    args = parser.parse_args()
    args.tmp.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        make_app(args.dist.resolve(), args.tmp.resolve()),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
