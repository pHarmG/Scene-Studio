"""Installer topology contract, mirrored by topology.ps1 for the CLI wizard.

No installation logic belongs here. Local transport is deliberately unsupported:
the existing deployers require SSH; visible paths and localhost cannot establish
filesystem namespace identity or a supported restart boundary.
"""
from urllib.parse import urlsplit
import re


def endpoint_host(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Use an HTTP(S) address without credentials, query or fragment")
    # Validate the port too, before any probe or support report.
    parsed.port
    return parsed.hostname


def new_topology(answers):
    ha_url = (answers.get("ha_url") or "").rstrip("/")
    ha_host = endpoint_host(ha_url) if ha_url else ""
    host = answers.get("ssh_host") or ha_host
    user = answers.get("ssh_user") or ""
    port = int(answers.get("ssh_port", 22))
    if host and not re.fullmatch(r"[a-zA-Z0-9_\[\]][a-zA-Z0-9_.:@\[\]-]*", host):
        raise ValueError("Invalid SSH filesystem host")
    if user and not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", user):
        raise ValueError("Invalid SSH username")
    if not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    http_url = answers.get("appdaemon_http_url")
    if http_url:
        endpoint_host(http_url)
    else:
        http_host = host.split("@")[-1]
        if ":" in http_host and not http_host.startswith("["):
            http_host = f"[{http_host}]"
        http_url = f"http://{http_host}:5050" if host else ""
    return {
        "ha_api_url": ha_url, "filesystem_transport": "ssh",
        "filesystem_host": host, "filesystem_user": user, "filesystem_port": port,
        "appdaemon_config_root": answers.get("appdaemon_config_root"),
        "appdaemon_http_url": http_url.rstrip("/"),
        "appdaemon_restart_strategy": "manual",
        "capabilities": {name: False for name in (
            "ssh_filesystem_available", "appdaemon_root_detected",
            "appdaemon_http_reachable", "supervisor_restart_available",
            "ha_www_access_available", "local_filesystem_supported")},
    }


def ha_filesystem_binding(topology, confirmed_split=False):
    host = topology["filesystem_host"].split("@")[-1].strip("[]")
    return bool(host and (host.lower() == endpoint_host(topology["ha_api_url"]).lower() or confirmed_split))


def common_ready(topology):
    return all(topology["capabilities"][key] for key in (
        "ssh_filesystem_available", "appdaemon_root_detected",
        "appdaemon_http_reachable", "supervisor_restart_available"))
