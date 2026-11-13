"""CLI for the two modes an agy session needs: priming from its shell, and the PreInvocation hook."""
import sys
import json
import argparse

from .prime import Prime


def main(prime: Prime = None):
    prime = prime or Prime()
    parser = argparse.ArgumentParser(description="Wake an idle Antigravity conversation")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prime", action="store_true", help="From an agy shell tool: hand this agy's push endpoint to its host process")
    group.add_argument("--prime-hook", action="store_true", help="PreInvocation hook: reads hook JSON on stdin and prints the JSON that makes the session prime push")
    args = parser.parse_args()

    if args.prime_hook:
        print(prime.run_hook(sys.stdin.read()))
        sys.exit(0)

    result = prime.prime_from_shell()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
