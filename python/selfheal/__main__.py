"""Commands for an integrated app that is already running in another terminal."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .models import RepairEvent
from .providers.compatible import _NoRedirect
from .terminal import TerminalReporter


def _session(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or type(data.get("url")) is not str
            or type(data.get("token")) is not str):
        raise ValueError("Invalid local session")
    url = urlsplit(data["url"])
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
            or url.username or url.password or url.path or url.query or url.fragment
            or type(data["token"]) is not str or len(data["token"]) < 32):
        raise ValueError("Invalid local session")
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config", help="Validate your local API configuration without making a network request")
    for name in ("status", "heal", "improve"):
        command = commands.add_parser(name)
        command.add_argument("--session", type=Path, default=Path(".selfheal/live.json"))
        command.add_argument("--timeout", type=float, default=300,
                             help="Maximum seconds without stream output; not a server cancellation")
        if name != "status":
            command.add_argument("--target", required=True)
            command.add_argument("--preview", action="store_true", help="Show a verified candidate without applying")
            command.add_argument("instructions", nargs="?", default="")
    agent = commands.add_parser("agent", help="Control the app's background caretaker")
    agent.add_argument("action", choices=("status", "pause", "resume", "scan", "reset"))
    agent.add_argument("--target", default=None)
    agent.add_argument("--session", type=Path, default=Path(".selfheal/live.json"))
    agent.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if args.command == "check-config":
        from .errors import ConfigurationError
        from .providers import provider_from_env
        from .config import SelfHealConfig
        try:
            config = SelfHealConfig(Path.cwd())
            provider = provider_from_env()
        except (ConfigurationError, OSError) as exc:
            print(f"Configuration error: {exc}")
            return 2
        print(f"Configuration loaded: {type(provider).__name__}. Your key and model are configured.")
        print(f"Repair budget: {config.repair_timeout_seconds:g}s total | hard timeout: {config.hard_timeout}")
        print("No API request made. Account access and model availability are checked on the first request.")
        return 0
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    if args.command == "improve" and not args.instructions.strip():
        parser.error("improve requires a quoted improvement request")
    reporter = TerminalReporter()
    try:
        session = _session(args.session)
        headers = {"Authorization": "Bearer " + session["token"]}
        body = None
        path = "/status"
        if args.command == "agent" and args.action != "status":
            body = json.dumps({"action": args.action, "target": args.target}).encode("utf-8")
            headers["Content-Type"] = "application/json"
            path = "/agent"
        elif args.command not in ("status", "agent"):
            body = json.dumps({"operation": args.command, "target": args.target,
                               "instructions": args.instructions, "preview": args.preview}).encode("utf-8")
            headers["Content-Type"] = "application/json"
            path = "/command"
        request = Request(session["url"] + path, data=body, headers=headers)
        # Bypass ambient HTTP proxies and refuse redirects of the session credential.
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=args.timeout) as response:
            if args.command in ("status", "agent"):
                data = json.loads(response.read(64_000))
                print(f"Running process: {data['pid']} | busy: {data['busy']} | disabled: {data['healing_disabled']}")
                print("Registered targets:", ", ".join(data["targets"]))
                if data.get("repair_timeout_seconds") is not None:
                    print(f"Repair budget: {data['repair_timeout_seconds']:g}s | hard timeout: {data['hard_timeout']}")
                if data["last_result"]:
                    result = data["last_result"]
                    reporter(RepairEvent(result["status"], result["message"], result.get("proposal_id")))
                if data.get("agent"):
                    state = data["agent"]
                    print(f"Agent: running={state['running']} paused={state['paused']} active={state['active_target']}")
                    print(f"AI transactions this hour: {state['attempts_last_hour']}/{state['config']['max_attempts_per_hour']}")
                    print("Suspended targets:", ", ".join(state["suspended"]) or "none")
                    for name, result in state["last_results"].items():
                        reporter(RepairEvent(result["status"], name + ": " + result["message"], result.get("proposal_id")))
                elif args.command == "agent":
                    print("This app has no background agent configured.")
                    return 1
                return 0
            while True:
                line = response.readline(2_000_001)
                if not line:
                    raise ValueError("Stream ended without a final result")
                if len(line) > 2_000_000:
                    raise ValueError("Event too large")
                data = json.loads(line)
                if data["kind"] == "event":
                    reporter(RepairEvent(data["phase"], data["message"], data.get("proposal_id"),
                                         data.get("diff", ""), data["timestamp"]))
                elif data["kind"] == "result":
                    print(f"\nResult: {data['status']} | application PID: {data['pid']}")
                    reporter(RepairEvent("result", data["message"], data.get("proposal_id")))
                    if data.get("snapshot_file"):
                        print("Recovery snapshot:", data["snapshot_file"])
                    return 0 if data["status"] in ("applied", "no_change", "preview") else 1
                else:
                    raise ValueError("Unknown stream record")
    except FileNotFoundError:
        print("No live session found. Start the integrated app first; use the same working directory or --session.")
    except HTTPError as exc:
        print(f"Control request rejected (HTTP {exc.code}). Check session credentials, target and active repairs.")
        exc.close()
    except (URLError, TimeoutError, OSError):
        print("Cannot reach the running app or the stream timed out. Check status before repeating a repair.")
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        print("Invalid session or incomplete control response. Check the app terminal and status before retrying.")
    except KeyboardInterrupt:
        print("\nTerminal detached. An accepted repair can still finish; check status before issuing another command.")
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
