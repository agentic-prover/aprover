"""AProver top-level CLI — dispatches to individual agent CLIs."""
import argparse
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="aprover",
        description="AProver: Agentic Prover — formal verification agent suite",
    )
    subparsers = parser.add_subparsers(dest="agent", metavar="AGENT")
    subparsers.required = True

    bmc = subparsers.add_parser("bmc-agent", help="BMC-Agent: LLM-driven bounded model checking")
    bmc.add_argument("args", nargs=argparse.REMAINDER, help="Arguments forwarded to bmc-agent")

    args = parser.parse_args()

    if args.agent == "bmc-agent":
        sys.exit(subprocess.call(["bmc-agent"] + args.args))


if __name__ == "__main__":
    main()
