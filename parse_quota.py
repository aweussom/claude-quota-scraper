#!/usr/bin/env python3
"""Parse and monitor Claude quota screenshots for Claude Code status line use."""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request
from urllib.parse import urlparse

DEFAULT_MODEL = "qwen3-vl:235b-cloud"
DEFAULT_HOST = "https://ollama.com"
DEFAULT_PATTERN = "claude_usage_*.png"


def now_iso() -> str:
    """Return UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def empty_quota_response() -> dict:
    """Return empty parse output."""
    return {
        "captured_at": now_iso(),
        "current_session": {"percent_used": None, "resets_in": ""},
        "weekly_limits": {"percent_used": None, "resets": ""},
    }


def validate_percent(value: object) -> int | float | None:
    """Validate percent value in range 0..100."""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not (0 <= num <= 100):
        return None
    if num.is_integer():
        return int(num)
    return num


def validate_string(value: object) -> str:
    """Ensure output value is a string."""
    if isinstance(value, str):
        return value
    return ""


def validate_quota_data(raw_data: object) -> tuple[dict, bool]:
    """Validate parser output schema."""
    result = empty_quota_response()
    is_valid = True

    if not isinstance(raw_data, dict):
        return result, False

    current = raw_data.get("current_session")
    if isinstance(current, dict):
        pct = validate_percent(current.get("percent_used"))
        result["current_session"]["percent_used"] = pct
        result["current_session"]["resets_in"] = validate_string(current.get("resets_in"))
        if pct is None:
            is_valid = False
    else:
        is_valid = False

    weekly = raw_data.get("weekly_limits")
    if isinstance(weekly, dict):
        pct = validate_percent(weekly.get("percent_used"))
        result["weekly_limits"]["percent_used"] = pct
        result["weekly_limits"]["resets"] = validate_string(weekly.get("resets"))
        if pct is None:
            is_valid = False
    else:
        is_valid = False

    return result, is_valid


def encode_image_base64(image_path: Path) -> str:
    """Read and base64 encode image."""
    with open(image_path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("utf-8")


def strip_thinking_tags(text: str) -> str:
    """Remove common model thinking tags."""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<thinking>.*?</thinking>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def extract_json_from_response(text: str, debug: bool = False) -> dict:
    """Extract first valid JSON object from model response."""
    text = strip_thinking_tags(text)
    if debug:
        print(f"DEBUG response:\n{text}\n", file=sys.stderr)

    code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_block:
        try:
            return json.loads(code_block.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start != -1:
        depth = 0
        end = -1
        for index, char in enumerate(text[start:], start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                candidate = re.sub(r",\s*}", "}", candidate)
                candidate = re.sub(r",\s*]", "]", candidate)
                return json.loads(candidate)

    return json.loads(text)


def call_ollama_vision(
    image_path: Path,
    model: str,
    host: str,
    api_key: str | None,
    timeout: int,
) -> str:
    """Call Ollama vision API and return model text response."""
    prompt = """Analyze this Claude.ai usage settings screenshot and extract quota information.

Return ONLY valid JSON with this exact structure:
{
  "current_session": {
    "percent_used": <number>,
    "resets_in": "<time string>"
  },
  "weekly_limits": {
    "percent_used": <number>,
    "resets": "<time string>"
  }
}

If a value is unclear, use null."""

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [encode_image_base64(image_path)],
            }
        ],
        "stream": False,
        "options": {"temperature": 0.0},
    }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    with request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    response = json.loads(body)
    message = response.get("message", {})
    return message.get("content") or response.get("response", "") or ""


def require_api_key_if_needed(host: str, api_key: str | None) -> None:
    """Require API key for non-local Ollama hosts."""
    host_name = (urlparse(host).hostname or "").lower()
    is_local = host_name in {"localhost", "127.0.0.1", "::1"}
    if not is_local and not api_key:
        raise ValueError("OLLAMA_API_KEY is required for non-local --host")


def parse_quota_image(
    image_path: Path,
    model: str,
    host: str,
    api_key: str | None,
    timeout: int,
    debug: bool,
) -> tuple[dict, bool]:
    """Parse one image and return validated quota data."""
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    api_key = api_key or os.environ.get("OLLAMA_API_KEY")
    require_api_key_if_needed(host, api_key)

    response_text = call_ollama_vision(image_path, model, host, api_key, timeout)
    raw_data = extract_json_from_response(response_text, debug=debug)
    return validate_quota_data(raw_data)


def write_json(path: Path | None, payload: dict, atomic: bool = False) -> None:
    """Write JSON to file or stdout."""
    text = json.dumps(payload, indent=2)
    if path is None:
        print(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if atomic:
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)
        return
    path.write_text(text, encoding="utf-8")


def build_status_payload(
    quota_data: dict | None,
    source_image: Path | None,
    valid: bool,
    error: str = "",
) -> dict:
    """Build compact JSON for ~/.claude/quota-data.json."""
    quota_data = quota_data or {}
    current = quota_data.get("current_session", {})
    weekly = quota_data.get("weekly_limits", {})

    payload = {
        "quota_used_pct": current.get("percent_used"),
        "weekly_used_pct": weekly.get("percent_used"),
        "resets_in": current.get("resets_in", ""),
        "weekly_resets": weekly.get("resets", ""),
        "updated": now_iso(),
        "valid": bool(valid),
    }
    if source_image:
        payload["source_image"] = source_image.name
    if error:
        payload["error"] = error
    return payload


def safe_unlink(path: Path, verbose: bool = False) -> None:
    """Best-effort file delete."""
    try:
        path.unlink(missing_ok=True)
        if verbose:
            print(f"[{now_iso()}] deleted {path.name}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"[{now_iso()}] could not delete {path.name}: {exc}", file=sys.stderr)


def cleanup_screenshots(
    watch_dir: Path,
    pattern: str,
    keep: set[Path],
    delete_through_mtime_ns: int | None = None,
    verbose: bool = False,
) -> None:
    """Delete matching screenshots except those in keep.

    If delete_through_mtime_ns is provided, only delete files with mtime <= that
    value. This avoids deleting a screenshot created while we're parsing the
    previously-selected "newest" file.
    """
    for candidate in watch_dir.glob(pattern):
        if not candidate.is_file():
            continue
        if candidate in keep:
            continue
        if delete_through_mtime_ns is not None:
            try:
                if candidate.stat().st_mtime_ns > delete_through_mtime_ns:
                    continue
            except OSError:
                continue
        safe_unlink(candidate, verbose=verbose)


def find_newest_image(watch_dir: Path, pattern: str) -> tuple[Path | None, int]:
    """Return newest matching image and mtime ns."""
    newest = None
    newest_mtime = -1
    for candidate in watch_dir.glob(pattern):
        if not candidate.is_file():
            continue
        mtime = candidate.stat().st_mtime_ns
        if mtime > newest_mtime:
            newest = candidate
            newest_mtime = mtime
    return newest, newest_mtime


def maybe_start_capture(
    capture_script: Path,
    interval: int,
    output_dir: Path,
    verbose: bool,
) -> subprocess.Popen:
    """Start screenshot capture PowerShell script."""
    if not capture_script.exists():
        raise FileNotFoundError(f"Capture script not found: {capture_script}")

    cmd = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(capture_script),
        "-IntervalSeconds",
        str(interval),
        "-OutputDir",
        str(output_dir),
    ]
    if verbose:
        print(f"Starting capture: {' '.join(cmd)}", file=sys.stderr)
    return subprocess.Popen(cmd)


def run_parse(args: argparse.Namespace) -> int:
    """Handle one-shot parse command."""
    output_path = args.output.expanduser() if args.output else None
    try:
        data, is_valid = parse_quota_image(
            image_path=args.image,
            model=args.model,
            host=args.host,
            api_key=args.api_key,
            timeout=args.timeout,
            debug=args.debug,
        )
        write_json(output_path, data)
        if not is_valid:
            print("Warning: extracted data failed validation", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        write_json(output_path, empty_quota_response())
        return 1


def run_monitor(args: argparse.Namespace) -> int:
    """Handle long-running monitor command."""
    watch_dir = args.watch_dir.expanduser().resolve()
    watch_dir.mkdir(parents=True, exist_ok=True)

    quota_file = args.quota_file.expanduser()
    full_output = args.full_output.expanduser() if args.full_output else None

    capture_proc = None
    if args.start_capture:
        capture_proc = maybe_start_capture(
            capture_script=args.capture_script.expanduser().resolve(),
            interval=args.capture_interval,
            output_dir=watch_dir,
            verbose=args.verbose,
        )

    last_image = None
    last_mtime = -1
    first_failed: Path | None = None
    last_failed: Path | None = None

    try:
        while True:
            image, mtime = find_newest_image(watch_dir, args.pattern)
            if image and (image != last_image or mtime != last_mtime):
                try:
                    data, is_valid = parse_quota_image(
                        image_path=image,
                        model=args.model,
                        host=args.host,
                        api_key=args.api_key,
                        timeout=args.timeout,
                        debug=args.debug,
                    )
                    write_json(
                        quota_file,
                        build_status_payload(data, image, is_valid),
                        atomic=True,
                    )
                    if full_output:
                        write_json(full_output, data, atomic=True)
                    if is_valid:
                        # Successful parse: delete the screenshot to avoid unbounded disk growth.
                        safe_unlink(image, verbose=args.verbose)
                    else:
                        # Keep only the first + most recent failure for debugging.
                        if first_failed is None:
                            first_failed = image
                        last_failed = image
                    if args.verbose:
                        print(
                            f"[{now_iso()}] parsed {image.name} ({'ok' if is_valid else 'invalid'})",
                            file=sys.stderr,
                        )
                except Exception as exc:  # noqa: BLE001
                    write_json(
                        quota_file,
                        build_status_payload(None, image, False, str(exc)),
                        atomic=True,
                    )
                    if first_failed is None:
                        first_failed = image
                    last_failed = image
                    if args.verbose:
                        print(
                            f"[{now_iso()}] failed {image.name}: {exc}",
                            file=sys.stderr,
                        )

                if first_failed is not None and not first_failed.exists():
                    first_failed = None
                if last_failed is not None and not last_failed.exists():
                    last_failed = None

                keep = {p for p in (first_failed, last_failed) if p is not None}
                cleanup_screenshots(
                    watch_dir,
                    args.pattern,
                    keep,
                    delete_through_mtime_ns=mtime,
                    verbose=args.verbose,
                )
                last_image = image
                last_mtime = mtime

            if args.once:
                if not image and args.verbose:
                    print(f"[{now_iso()}] no matching screenshots in {watch_dir}", file=sys.stderr)
                return 0

            time.sleep(max(args.poll_seconds, 0.5))
    except KeyboardInterrupt:
        if args.verbose:
            print("Stopped by Ctrl+C", file=sys.stderr)
        return 0
    finally:
        if capture_proc and capture_proc.poll() is None:
            capture_proc.terminate()
            try:
                capture_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                capture_proc.kill()


def add_shared_model_args(parser: argparse.ArgumentParser) -> None:
    """Add API/model options used by parse and monitor commands."""
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Vision model (default: {DEFAULT_MODEL})")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Ollama API host (default: {DEFAULT_HOST})")
    parser.add_argument("--api-key", help="Optional API key override (default: OLLAMA_API_KEY env var)")
    parser.add_argument("--timeout", type=int, default=120, help="API timeout seconds (default: 120)")
    parser.add_argument("--debug", action="store_true", help="Print raw model response to stderr")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="Claude quota screenshot parser and monitor")
    sub = parser.add_subparsers(dest="command", required=True)

    parse_cmd = sub.add_parser("parse", help="Parse one screenshot and print JSON")
    parse_cmd.add_argument("image", type=Path, help="Path to screenshot")
    parse_cmd.add_argument("--output", "-o", type=Path, help="Write JSON to file instead of stdout")
    add_shared_model_args(parse_cmd)
    parse_cmd.set_defaults(func=run_parse)

    monitor_cmd = sub.add_parser(
        "monitor",
        help="Watch screenshots and update ~/.claude/quota-data.json (cleans up screenshots)",
    )
    monitor_cmd.add_argument("--watch-dir", type=Path, default=Path.cwd(), help="Screenshot directory")
    monitor_cmd.add_argument("--pattern", default=DEFAULT_PATTERN, help=f"Screenshot glob (default: {DEFAULT_PATTERN})")
    monitor_cmd.add_argument("--poll-seconds", type=float, default=10.0, help="Polling interval seconds (default: 10)")
    monitor_cmd.add_argument(
        "--quota-file",
        type=Path,
        default=Path.home() / ".claude" / "quota-data.json",
        help="Status line quota JSON path",
    )
    monitor_cmd.add_argument("--full-output", type=Path, help="Optional full parse JSON output file")
    monitor_cmd.add_argument("--once", action="store_true", help="Process newest screenshot once and exit")
    monitor_cmd.add_argument("--start-capture", action="store_true", help="Start capture_claude_usage.ps1 automatically")
    monitor_cmd.add_argument(
        "--capture-script",
        type=Path,
        default=Path(__file__).with_name("capture_claude_usage.ps1"),
        help="Path to capture PowerShell script",
    )
    monitor_cmd.add_argument("--capture-interval", type=int, default=60, help="Capture interval seconds")
    monitor_cmd.add_argument("--verbose", action="store_true", help="Print monitor activity to stderr")
    add_shared_model_args(monitor_cmd)
    monitor_cmd.set_defaults(func=run_monitor)

    return parser


if __name__ == "__main__":
    cli = build_parser()
    cli_args = cli.parse_args()
    sys.exit(cli_args.func(cli_args))
