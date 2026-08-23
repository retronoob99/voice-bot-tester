from __future__ import annotations

import argparse


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from src.media_bridge import app

    uvicorn.run(app, host=args.host, port=args.port)


def _cmd_call(args: argparse.Namespace) -> None:
    from src.orchestrator import run_call

    run_call(args.scenario, wait=args.wait)


def _cmd_list_scenarios(args: argparse.Namespace) -> None:
    from src.scenario import list_scenarios

    for name in list_scenarios():
        print(name)


def main() -> None:
    parser = argparse.ArgumentParser(prog="voice-bot-tester")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser(
        "serve", help="Run the Twilio<->Deepgram media bridge WebSocket server"
    )
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.set_defaults(func=_cmd_serve)

    call_parser = subparsers.add_parser("call", help="Place an outbound test call for a scenario")
    call_parser.add_argument("--scenario", required=True, help="Scenario name (scenarios/<name>.yaml)")
    call_parser.add_argument(
        "--wait",
        action="store_true",
        help="Block until the call completes, then download the recording and run bug analysis",
    )
    call_parser.set_defaults(func=_cmd_call)

    list_parser = subparsers.add_parser("scenarios", help="List available scenario names")
    list_parser.set_defaults(func=_cmd_list_scenarios)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
