#!/usr/bin/env python3
"""Validate security invariants in a rendered Wishkr Docker Compose model."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


EXPECTED_SERVICES = {"backend", "scheduler", "db", "redis"}


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_model() -> dict[str, Any]:
    if len(sys.argv) != 2:
        fail(f"usage: {Path(sys.argv[0]).name} RENDERED_COMPOSE_JSON")

    source = Path(sys.argv[1])
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read rendered Compose JSON: {exc}")


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def is_positive_quantity(value: Any) -> bool:
    if isinstance(value, (int, float)):
        return value > 0
    return isinstance(value, str) and bool(value) and not value.startswith(("0b", "-"))


def main() -> None:
    model = load_model()
    services = model.get("services", {})
    require(set(services) == EXPECTED_SERVICES, "unexpected service set")

    for name, service in services.items():
        user = str(service.get("user", ""))
        require(user and user.split(":", 1)[0] != "0", f"{name}: root user is forbidden")
        require(service.get("read_only") is True, f"{name}: root filesystem is writable")
        require(service.get("privileged") is not True, f"{name}: privileged mode enabled")
        require(
            "no-new-privileges:true" in service.get("security_opt", []),
            f"{name}: no-new-privileges is missing",
        )
        require("ALL" in service.get("cap_drop", []), f"{name}: cap_drop ALL is missing")
        require(bool(service.get("healthcheck")), f"{name}: healthcheck is missing")
        require(
            is_positive_quantity(service.get("pids_limit")),
            f"{name}: PID limit is missing",
        )
        require(is_positive_quantity(service.get("cpus")), f"{name}: CPU limit is missing")
        require(
            is_positive_quantity(service.get("mem_limit")),
            f"{name}: memory limit is missing",
        )
        require(
            is_positive_quantity(service.get("mem_reservation")),
            f"{name}: memory reservation is missing",
        )

        for volume in service.get("volumes", []):
            source = str(volume.get("source", ""))
            target = str(volume.get("target", ""))
            require(
                source != "/var/run/docker.sock" and target != "/var/run/docker.sock",
                f"{name}: Docker socket mount is forbidden",
            )
            require(volume.get("type") != "bind", f"{name}: host bind mount is forbidden")

    for name in ("db", "redis", "scheduler"):
        require(not services[name].get("ports"), f"{name}: must not publish a port")

    backend_ports = services["backend"].get("ports", [])
    require(len(backend_ports) == 1, "backend: expected exactly one published port")
    backend_port = backend_ports[0]
    require(backend_port.get("host_ip") == "127.0.0.1", "backend: not bound to localhost")
    require(
        backend_port.get("published") == "8000" and backend_port.get("target") == 8000,
        "backend: unexpected port mapping",
    )

    require(set(services["db"].get("networks", {})) == {"data"}, "db: wrong networks")
    require(set(services["redis"].get("networks", {})) == {"data"}, "redis: wrong networks")
    for name in ("backend", "scheduler"):
        require(
            set(services[name].get("networks", {})) == {"app", "data"},
            f"{name}: wrong networks",
        )

    networks = model.get("networks", {})
    require(networks.get("app", {}).get("external") is True, "app network is not external")
    require(networks.get("data", {}).get("external") is True, "data network is not external")
    require(networks["app"].get("name") == "wishkr-app", "app network name is wrong")
    require(networks["data"].get("name") == "wishkr-data", "data network name is wrong")

    require(
        any(secret.get("source") == "postgres_password" for secret in services["db"].get("secrets", [])),
        "db: PostgreSQL password secret is missing",
    )
    require(
        any(secret.get("source") == "redis_password" for secret in services["redis"].get("secrets", [])),
        "redis: Redis password secret is missing",
    )

    print("PASS: rendered Compose model satisfies all Wishkr security invariants")


if __name__ == "__main__":
    main()
