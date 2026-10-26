"""Static guards for the SearXNG hardening (compose + monitoring config).

No Python is involved: these read the repo's YAML so a later edit can't quietly
undo the hardening. Why each invariant exists:

- Loopback-only publish: SearXNG has no auth, its limiter is off and ufw is
  inactive on Jupiter, so an all-interfaces publish lets any LAN/tailnet host
  burst searches through it (getting the upstream engines suspended) and use
  /image_proxy.
- Digest-pinned image: `:latest` can silently change the JSON that
  web_search.py parses and whether SearXNG still refuses the placeholder key.
- No server.secret_key in searxng/settings.yml: the repo is public and the key
  signs /image_proxy URLs; it comes from SEARXNG_SECRET in .env.
- SEARXNG_SECRET=${SEARXNG_SECRET:-ultrasecretkey}: fail closed. SearXNG uses an
  empty value as the key, but refuses to start on 'ultrasecretkey'.
- No SEARXNG_PORT in the container env: the image entrypoint turns it into
  GRANIAN_PORT and the server would leave 8080, where Jess and the probe call it.
- Non-root, no capabilities, no-new-privileges, read-only rootfs: it parses
  untrusted upstream pages.
- searxng-probe-net: with a loopback publish, blackbox reaches searxng by name
  over an internal network of its own. Blackbox must stay off gateway-net,
  because anything sharing a network with it can drive its /probe endpoint.
- Probe /healthz, never /search: bursts of real searches get the engines
  suspended.
- Blackbox modules are GET-only http and all used by Prometheus: a POST module
  would let a probe-network neighbour POST prometheus:9090/-/quit, and a tcp
  module turns blackbox into a port scanner.

Needs a full checkout (GitHub CI runs it from one). The documented local runner
mounts only orchestrator/, scripts/ and data/ into the image, so there every
test skips. To run it locally, mount the whole worktree and work from it:
`docker run --rm -v "$PWD:/repo:ro" -w /repo gateway_nerves-orchestrator ...`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "docker-compose.yml"
_MON_COMPOSE = _REPO / "monitoring" / "docker-compose.yml"
_SETTINGS = _REPO / "searxng" / "settings.yml"
_PROM_TEMPLATE = _REPO / "monitoring" / "prometheus" / "prometheus.yml.template"
_BLACKBOX = _REPO / "monitoring" / "blackbox" / "blackbox.yml"

_PROBE_NET = "searxng-probe-net"
_PROBE_NET_NAME = "gateway_nerves_searxng-probe-net"
_GATEWAY_NET_NAME = "gateway_nerves_gateway-net"
_SEARXNG_PROBE_TARGET = "http://searxng:8080/healthz"
_SECRET_ENV = "${SEARXNG_SECRET:-ultrasecretkey}"
_OLD_PUBLIC_SECRET = "brain-gateway-searxng-key"
_NO_NEW_PRIVS = {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}


def _rel(path: Path) -> str:
    return str(path.relative_to(_REPO))


def _needs(*paths: Path):
    missing = [_rel(p) for p in paths if not p.is_file()]
    return pytest.mark.skipif(
        bool(missing),
        reason=f"needs a full repo checkout (missing: {', '.join(missing)})",
    )


def _load(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        pytest.fail(f"{_rel(path)} no longer parses as YAML ({exc}); fix it or switch this guard to a text check")
    assert isinstance(data, dict), f"{_rel(path)} did not parse to a mapping"
    return data


def _service(compose: Path, name: str) -> dict:
    svc = (_load(compose).get("services") or {}).get(name)
    assert isinstance(svc, dict), f"{_rel(compose)} has no services.{name}; if it was renamed, update this guard"
    return svc


def _searxng() -> dict:
    return _service(_COMPOSE, "searxng")


def _env(svc: dict) -> dict[str, str | None]:
    """Compose `environment` as a dict. A bare list entry (`- KEY`) maps to None."""
    env = svc.get("environment") or {}
    if isinstance(env, dict):
        return {str(k): (None if v is None else str(v)) for k, v in env.items()}
    out: dict[str, str | None] = {}
    for item in env:
        key, sep, value = str(item).partition("=")
        out[key.strip()] = value if sep else None
    return out


def _network_keys(svc: dict) -> list[str]:
    nets = svc.get("networks") or []
    return [str(n) for n in (nets.keys() if isinstance(nets, dict) else nets)]


def _scrape_jobs() -> list[dict]:
    jobs = _load(_PROM_TEMPLATE).get("scrape_configs") or []
    assert jobs, f"{_rel(_PROM_TEMPLATE)} has no scrape_configs"
    return jobs


def _blackbox_jobs() -> list[dict]:
    """Jobs that go through blackbox: a `module` param or a relabel to blackbox-exporter."""
    out = []
    for job in _scrape_jobs():
        via_blackbox = any(
            str(r.get("replacement", "")).startswith("blackbox-exporter") for r in job.get("relabel_configs") or []
        )
        if via_blackbox or "module" in (job.get("params") or {}):
            out.append(job)
    assert out, f"found no blackbox jobs in {_rel(_PROM_TEMPLATE)}; the guard's job detection needs updating"
    return out


def _targets(job: dict):
    for sc in job.get("static_configs") or []:
        for target in sc.get("targets") or []:
            yield str(target), sc.get("labels") or {}


def _referenced_modules() -> set[str]:
    refs: set[str] = set()
    for job in _blackbox_jobs():
        mods = (job.get("params") or {}).get("module") or []
        refs.update(str(m) for m in ([mods] if isinstance(mods, str) else mods))
        for r in job.get("relabel_configs") or []:
            if r.get("target_label") == "__param_module" and r.get("replacement"):
                refs.add(str(r["replacement"]))
    return refs


def _blackbox_modules() -> dict:
    modules = _load(_BLACKBOX).get("modules") or {}
    assert modules, f"{_rel(_BLACKBOX)} defines no modules"
    return modules


# -- 1. publish address --------------------------------------------------------


@_needs(_COMPOSE)
def test_searxng_publishes_on_loopback_only():
    ports = _searxng().get("ports") or []
    assert ports, (
        "services.searxng.ports is empty. CI's deploy check reads `docker compose port searxng 8080` and the "
        "browser UI is documented on 127.0.0.1:8090; keep a 127.0.0.1 publish (or update CI and this guard)."
    )
    bad = []
    for entry in ports:
        if isinstance(entry, dict):  # long syntax
            ok = str(entry.get("host_ip", "")) == "127.0.0.1"
        else:
            ok = str(entry).startswith("127.0.0.1:")
        if not ok:
            bad.append(entry)
    assert not bad, (
        f"services.searxng.ports publishes beyond loopback: {bad!r}. SearXNG has no auth and no limiter and ufw "
        "is inactive on Jupiter, so this lets any LAN/tailnet host search through it and use /image_proxy. "
        'Use "127.0.0.1:${SEARXNG_PORT:-8090}:8080".'
    )


# -- 2. image pin --------------------------------------------------------------


@_needs(_COMPOSE)
def test_searxng_image_is_digest_pinned():
    image = str(_searxng().get("image") or "")
    assert re.search(r"@sha256:[0-9a-f]{64}$", image), (
        f"services.searxng.image is {image!r}, not digest-pinned. Pin it as tag@sha256:<digest> and re-check "
        "web_search.py's unresponsive_engines parsing + the 'ultrasecretkey' refusal in the new image."
    )


# -- 3. no committed secret ----------------------------------------------------


@_needs(_SETTINGS)
def test_settings_yml_has_no_secret_key():
    server = _load(_SETTINGS).get("server") or {}
    assert "secret_key" not in server, (
        "searxng/settings.yml sets server.secret_key. The repo is public and the key signs /image_proxy URLs: "
        "remove it; SearXNG reads it from SEARXNG_SECRET in .env (docker-compose.yml)."
    )


@_needs(_COMPOSE, _SETTINGS)
def test_old_public_secret_is_gone():
    found = [_rel(p) for p in (_COMPOSE, _SETTINGS) if _OLD_PUBLIC_SECRET in p.read_text(encoding="utf-8")]
    assert not found, f"the old public SearXNG secret {_OLD_PUBLIC_SECRET!r} is back in: {', '.join(found)}"


# -- 4. container environment --------------------------------------------------


@_needs(_COMPOSE)
def test_searxng_secret_comes_from_env_and_fails_closed():
    env = _env(_searxng())
    assert env.get("SEARXNG_SECRET") == _SECRET_ENV, (
        f"services.searxng.environment SEARXNG_SECRET is {env.get('SEARXNG_SECRET')!r}, expected {_SECRET_ENV!r}. "
        "SearXNG uses an empty value as the key but refuses to start on 'ultrasecretkey', so this default makes "
        "a missing .env secret stop the container instead of running with an empty or committed key."
    )


@_needs(_COMPOSE)
def test_searxng_port_not_passed_into_container():
    svc = _searxng()
    assert "SEARXNG_PORT" not in _env(svc), (
        "services.searxng.environment sets SEARXNG_PORT. The image entrypoint turns it into GRANIAN_PORT, so the "
        "server would leave 8080, where Jess (SEARXNG_URL) and the blackbox probe call it. SEARXNG_PORT is "
        "the host port only (ports:)."
    )
    assert "env_file" not in svc, (
        "services.searxng has an env_file. The repo .env carries SEARXNG_PORT (the host port), which the image "
        "entrypoint would turn into GRANIAN_PORT and move the server off 8080. Pass single variables in "
        "environment: instead."
    )


# -- 5. container privileges ---------------------------------------------------


@_needs(_COMPOSE)
def test_searxng_runs_unprivileged():
    svc = _searxng()
    user = str(svc.get("user") or "").strip()
    assert user, "services.searxng has no user: it would run as root. Use the image's searxng user (977:977)."
    assert user.split(":", 1)[0] not in {"0", "root"}, f"services.searxng.user is {user!r}: that is root"
    assert not svc.get("privileged"), "services.searxng is privileged: that hands back every capability"

    cap_drop = [str(c).upper() for c in svc.get("cap_drop") or []]
    assert "ALL" in cap_drop, f"services.searxng.cap_drop is {cap_drop!r}; it must contain ALL"

    security_opt = [str(o).replace(" ", "") for o in svc.get("security_opt") or []]
    assert _NO_NEW_PRIVS & set(security_opt), (
        f"services.searxng.security_opt is {security_opt!r}; it must contain no-new-privileges:true"
    )

    assert svc.get("read_only") is True, (
        f"services.searxng.read_only is {svc.get('read_only')!r}; it must be true (its caches live in the /tmp tmpfs)"
    )


# -- 6. probe network ----------------------------------------------------------


@_needs(_COMPOSE)
def test_searxng_probe_network_in_main_compose():
    nets = _network_keys(_searxng())
    for needed in ("gateway-net", _PROBE_NET):
        assert needed in nets, (
            f"services.searxng.networks is {nets!r}; it must include {needed} (gateway-net: Jess calls "
            f"http://searxng:8080; {_PROBE_NET}: the blackbox /healthz probe, since the publish is loopback-only)"
        )

    top = (_load(_COMPOSE).get("networks") or {}).get(_PROBE_NET)
    assert isinstance(top, dict), f"docker-compose.yml has no top-level networks.{_PROBE_NET}"
    assert top.get("internal") is True, (
        f"networks.{_PROBE_NET} must be internal: true (no gateway; searxng's upstream traffic uses gateway-net)"
    )
    assert top.get("name") == _PROBE_NET_NAME, (
        f"networks.{_PROBE_NET}.name is {top.get('name')!r}; monitoring/docker-compose.yml joins it as external "
        f"{_PROBE_NET_NAME!r}, so the fixed name must match"
    )


@_needs(_MON_COMPOSE)
def test_blackbox_on_searxng_probe_net_and_off_gateway_net():
    mon = _load(_MON_COMPOSE)
    top_nets = mon.get("networks") or {}
    bb = _service(_MON_COMPOSE, "blackbox-exporter")
    keys = _network_keys(bb)

    assert _PROBE_NET in keys, f"blackbox-exporter networks are {keys!r}; it must join {_PROBE_NET} to probe searxng"

    def real_name(key: str) -> str:
        decl = top_nets.get(key) or {}
        return str(decl.get("name") or key)

    on_gateway = [k for k in keys if k == "gateway-net" or real_name(k) == _GATEWAY_NET_NAME]
    assert not on_gateway, (
        f"blackbox-exporter is on gateway-net (via {on_gateway!r}). Anything on its networks can drive /probe, "
        "and gateway-net holds the orchestrator, frontend and searxng. Use a dedicated probe network."
    )

    top = top_nets.get(_PROBE_NET)
    assert isinstance(top, dict), f"monitoring/docker-compose.yml has no top-level networks.{_PROBE_NET}"
    assert top.get("external") is True, (
        f"monitoring networks.{_PROBE_NET} must be external: true (the main compose project creates it)"
    )
    assert top.get("name") == _PROBE_NET_NAME, (
        f"monitoring networks.{_PROBE_NET}.name is {top.get('name')!r}, expected {_PROBE_NET_NAME!r} "
        "(the name the main compose project creates)"
    )


# -- 7. probe target -----------------------------------------------------------


@_needs(_PROM_TEMPLATE)
def test_searxng_probe_target_is_healthz_by_container_name():
    found = [
        (job.get("job_name"), target)
        for job in _blackbox_jobs()
        for target, labels in _targets(job)
        if labels.get("service") == "searxng" or "searxng" in target.lower()
    ]
    assert [t for _, t in found] == [_SEARXNG_PROBE_TARGET], (
        f"SearXNG blackbox targets are {found!r}; expected exactly [{_SEARXNG_PROBE_TARGET!r}]. The publish is "
        "loopback-only, so the probe goes by container name over searxng-probe-net, and /healthz sends nothing "
        "upstream."
    )


@_needs(_PROM_TEMPLATE)
def test_no_blackbox_target_runs_a_search():
    bad = [(job.get("job_name"), t) for job in _blackbox_jobs() for t, _ in _targets(job) if "/search" in t]
    assert not bad, (
        f"blackbox targets hit /search: {bad!r}. A real search every scrape gets the upstream engines suspended; "
        "probe /healthz."
    )


# -- 8. blackbox modules -------------------------------------------------------


@_needs(_BLACKBOX)
def test_blackbox_modules_are_get_only_http():
    bad = {}
    for name, mod in _blackbox_modules().items():
        mod = mod or {}
        prober = mod.get("prober")
        method = (mod.get("http") or {}).get("method")
        if prober != "http" or method != "GET":
            bad[name] = {"prober": prober, "method": method}
    assert not bad, (
        f"blackbox modules that aren't explicit `prober: http` + `method: GET`: {bad!r}. Every container on a "
        "probe network can call /probe with any module: a POST module could hit prometheus:9090/-/quit, a tcp "
        "module is a port scanner."
    )


@_needs(_BLACKBOX, _PROM_TEMPLATE)
def test_every_blackbox_module_is_used_by_prometheus():
    refs = _referenced_modules()
    assert refs, f"{_rel(_PROM_TEMPLATE)} references no blackbox module; the guard's parsing needs updating"
    unused = sorted(set(_blackbox_modules()) - refs)
    assert not unused, (
        f"blackbox.yml defines modules Prometheus never uses: {unused!r}. Unused modules are still callable by "
        "anything on a probe network; delete them."
    )


@_needs(_BLACKBOX, _PROM_TEMPLATE)
def test_every_referenced_module_exists():
    refs = _referenced_modules()
    assert refs, f"{_rel(_PROM_TEMPLATE)} references no blackbox module; the guard's parsing needs updating"
    missing = sorted(refs - set(_blackbox_modules()))
    assert not missing, (
        f"prometheus.yml.template references blackbox modules that blackbox.yml doesn't define: {missing!r} "
        "(those probes would fail with 'unknown module')"
    )
