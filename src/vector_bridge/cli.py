"""`vector-bridge` command line entry point: serve plus the management subcommands in manage.py."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from . import config as configmod
from . import manage, version_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vector-bridge", description="VectorBridge KX2 network audio bridge")
    parser.add_argument("--version", action="version", version=f"vector-bridge {version_text()}")
    parser.add_argument("--config", type=Path, default=None,
                        help=f"config file (default {configmod.DEFAULT_PATH})")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the bridge service in the foreground")
    serve.add_argument("--audio", choices=["alsa", "fake"], default="alsa",
                       help="audio backend: alsa (Pi) or fake (1 kHz tone, no hardware)")
    serve.add_argument("--bind", default="0.0.0.0", help="address to bind the UDP ports on")
    serve.set_defaults(func=cmd_serve)
    manage.add_subcommands(sub)
    return parser


def cmd_serve(args) -> int:
    from .service import BridgeService

    cfg = configmod.load(args.config)
    svc = BridgeService(cfg, audio=args.audio, bind=args.bind, config_path=args.config)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: svc._stop.set())
    svc.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
