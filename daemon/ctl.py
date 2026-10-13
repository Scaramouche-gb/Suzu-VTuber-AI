"""CLI control tool (vtuberctl) for Suzu AI-VTuber."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_SERVER_URL = "http://127.0.0.1:8765"


def _request_json(
    url: str, method: str = "GET", data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Execute HTTP request to daemon API and parse JSON response."""
    encoded_data = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content)
    except urllib.error.URLError as exc:
        print(
            f"[vtuberctl Error] Cannot connect to daemon at {DEFAULT_SERVER_URL}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_status(_args: argparse.Namespace) -> None:
    """Print daemon status and metrics."""
    res = _request_json(f"{DEFAULT_SERVER_URL}/api/status")
    print("=== Suzu VTuber Status ===")
    print(f"State:        {res.get('state', 'unknown')}")
    print(f"Mood:         {res.get('mood', 'unknown')}")
    print(f"Energy:       {res.get('energy', 0.0):.2f} / 1.00")
    print(f"DND:          {'ON' if res.get('dnd') else 'OFF'}")
    print(f"Muted:        {'ON' if res.get('muted') else 'OFF'}")
    print(f"Blind Mode:   {'ON' if res.get('blind') else 'OFF'}")
    print(f"Active App:   {res.get('active_app', 'none')} ({res.get('active_title', '')})")
    print(f"Screen Desc:  {res.get('screen_context', 'none')}")
    print(
        f"Today Cost:   ${res.get('daily_cost', 0.0):.4f} / ${res.get('max_usd_per_day', 0.0):.2f}"
    )


def cmd_mute(args: argparse.Namespace) -> None:
    state = args.state.lower() in ("on", "1", "true")
    res = _request_json(
        f"{DEFAULT_SERVER_URL}/api/command", "POST", {"command": "mute", "args": {"enabled": state}}
    )
    print(f"Microphone mute set to: {'ON' if state else 'OFF'} ({res.get('status')})")


def cmd_blind(args: argparse.Namespace) -> None:
    state = args.state.lower() in ("on", "1", "true")
    res = _request_json(
        f"{DEFAULT_SERVER_URL}/api/command",
        "POST",
        {"command": "blind", "args": {"enabled": state}},
    )
    print(f"Vision blind mode set to: {'ON' if state else 'OFF'} ({res.get('status')})")


def cmd_dnd(args: argparse.Namespace) -> None:
    state = args.state.lower() in ("on", "1", "true")
    res = _request_json(
        f"{DEFAULT_SERVER_URL}/api/command", "POST", {"command": "dnd", "args": {"enabled": state}}
    )
    print(f"Do Not Disturb set to: {'ON' if state else 'OFF'} ({res.get('status')})")


def cmd_say(args: argparse.Namespace) -> None:
    text = " ".join(args.text).strip()
    if not text:
        print("[vtuberctl Error] Missing text argument to say", file=sys.stderr)
        sys.exit(1)
    res = _request_json(
        f"{DEFAULT_SERVER_URL}/api/command", "POST", {"command": "say", "args": {"text": text}}
    )
    print(f"Dispatched say: «{text}» ({res.get('status')})")


def cmd_ask(args: argparse.Namespace) -> None:
    text = " ".join(args.text).strip()
    if not text:
        print("[vtuberctl Error] Missing text argument to ask", file=sys.stderr)
        sys.exit(1)
    res = _request_json(
        f"{DEFAULT_SERVER_URL}/api/command", "POST", {"command": "ask", "args": {"text": text}}
    )
    print(f"Asked Suzu: «{text}» ({res.get('status')})")


def cmd_look(args: argparse.Namespace) -> None:
    target = args.target.lower()
    res = _request_json(
        f"{DEFAULT_SERVER_URL}/api/command", "POST", {"command": "look", "args": {"target": target}}
    )
    print(f"Look target set to: {target} ({res.get('status')})")


def cmd_mood(args: argparse.Namespace) -> None:
    mood = args.mood.lower()
    res = _request_json(
        f"{DEFAULT_SERVER_URL}/api/command", "POST", {"command": "mood", "args": {"mood": mood}}
    )
    print(f"Mood set to: {mood} ({res.get('status')})")


def cmd_hide(_args: argparse.Namespace) -> None:
    res = _request_json(f"{DEFAULT_SERVER_URL}/api/command", "POST", {"command": "hide"})
    print(f"Avatar hidden ({res.get('status')})")


def cmd_show(_args: argparse.Namespace) -> None:
    res = _request_json(f"{DEFAULT_SERVER_URL}/api/command", "POST", {"command": "show"})
    print(f"Avatar visible ({res.get('status')})")


def cmd_memory(args: argparse.Namespace) -> None:
    sub = args.subaction.lower()
    if sub == "list":
        res = _request_json(f"{DEFAULT_SERVER_URL}/api/memory")
        facts = res.get("facts", [])
        episodes = res.get("episodes", [])

        print("=== Known Facts ===")
        if not facts:
            print("  (нет фактов)")
        for f in facts:
            print(f"  [{f.get('id')}] (важность: {f.get('importance', 1.0):.1f}) {f.get('text')}")

        print("\n=== Recent Episodes ===")
        if not episodes:
            print("  (нет эпизодов)")
        for e in episodes:
            print(f"  [{e.get('id')}] {e.get('summary')}")

    elif sub == "forget":
        if not args.fact_id:
            print(
                "[vtuberctl Error] Specify fact ID to forget (e.g. vtuberctl memory forget 3)",
                file=sys.stderr,
            )
            sys.exit(1)
        res = _request_json(
            f"{DEFAULT_SERVER_URL}/api/memory/forget",
            "POST",
            {"id": args.fact_id},
        )
        print(f"Fact #{args.fact_id} deleted: {res.get('deleted')}")

    elif sub == "wipe":
        res = _request_json(f"{DEFAULT_SERVER_URL}/api/memory/wipe", "POST", {})
        print("All persistent memory wiped successfully.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vtuberctl", description="Control client for Suzu AI-VTuber daemon"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = subparsers.add_parser("status", help="Show daemon status, mood, and usage costs")
    p_status.set_defaults(func=cmd_status)

    # mute
    p_mute = subparsers.add_parser("mute", help="Toggle microphone capture")
    p_mute.add_argument("state", choices=["on", "off"], help="on or off")
    p_mute.set_defaults(func=cmd_mute)

    # blind
    p_blind = subparsers.add_parser("blind", help="Toggle vision blind mode")
    p_blind.add_argument("state", choices=["on", "off"], help="on or off")
    p_blind.set_defaults(func=cmd_blind)

    # dnd
    p_dnd = subparsers.add_parser("dnd", help="Toggle Do Not Disturb proactive speech")
    p_dnd.add_argument("state", choices=["on", "off"], help="on or off")
    p_dnd.set_defaults(func=cmd_dnd)

    # say
    p_say = subparsers.add_parser("say", help="Force avatar to speak custom text")
    p_say.add_argument("text", nargs="+", help="Text to speak")
    p_say.set_defaults(func=cmd_say)

    # ask
    p_ask = subparsers.add_parser("ask", help="Send user prompt/question to Suzu")
    p_ask.add_argument("text", nargs="+", help="Prompt/question to ask")
    p_ask.set_defaults(func=cmd_ask)

    # look
    p_look = subparsers.add_parser("look", help="Change avatar gaze target")
    p_look.add_argument("target", choices=["screen", "cursor", "user"], default="cursor", nargs="?")
    p_look.set_defaults(func=cmd_look)

    # mood
    p_mood = subparsers.add_parser("mood", help="Force avatar mood")
    p_mood.add_argument("mood", choices=["playful", "curious", "sleepy", "sassy", "caring"])
    p_mood.set_defaults(func=cmd_mood)

    # hide / show
    p_hide = subparsers.add_parser("hide", help="Hide avatar window")
    p_hide.set_defaults(func=cmd_hide)

    p_show = subparsers.add_parser("show", help="Show avatar window")
    p_show.set_defaults(func=cmd_show)

    # memory
    p_mem = subparsers.add_parser("memory", help="Inspect and manage persistent memory")
    p_mem.add_argument(
        "subaction", choices=["list", "forget", "wipe"], help="list, forget <id>, or wipe"
    )
    p_mem.add_argument("fact_id", type=int, nargs="?", help="Fact ID (for forget)")
    p_mem.set_defaults(func=cmd_memory)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
