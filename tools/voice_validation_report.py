from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


MARKER_RE = re.compile(r"\[VOICE TEST\] START (?P<label>[A-Z0-9_ -]+)")
LATENCY_RE = re.compile(
    r"latency (?P<name>final_transcript_to_reply_start|user_speech_start_to_reply_start)=(?P<value>\d+(?:\.\d+)?)s"
)
CRITICAL_RE = re.compile(r"LATENCIA CRITICA|reply_start demorou", re.IGNORECASE)
GEN_TIMEOUT_RE = re.compile(r"generate_reply excedeu|generate_reply timeout", re.IGNORECASE)


@dataclass
class ScenarioStats:
    label: str
    lines: list[str]

    def count(self, pattern: str) -> int:
        token = pattern.lower()
        return sum(1 for line in self.lines if token in line.lower())

    def max_reply_start_latency(self) -> float:
        values: list[float] = []
        for line in self.lines:
            match = LATENCY_RE.search(line)
            if not match:
                continue
            try:
                values.append(float(match.group("value")))
            except Exception:
                continue
        return max(values) if values else 0.0

    def critical_latency(self) -> bool:
        if any(CRITICAL_RE.search(line) for line in self.lines):
            return True
        return self.max_reply_start_latency() > 2.0

    def generate_reply_timeouts(self) -> int:
        return sum(1 for line in self.lines if GEN_TIMEOUT_RE.search(line))

    def reply_started(self) -> int:
        return self.count("agent_reply_started")

    def reply_finished(self) -> int:
        return self.count("agent_reply_finished")

    def possible_stall(self) -> bool:
        return self.reply_started() > self.reply_finished()

    def verdict(self) -> str:
        if self.critical_latency():
            return "FAIL"
        if self.generate_reply_timeouts() > 0:
            return "FAIL"
        if self.possible_stall():
            return "FAIL"
        return "PASS"


def split_scenarios(lines: list[str]) -> list[ScenarioStats]:
    scenarios: list[ScenarioStats] = []
    current_label = "SEM_MARCADOR"
    current_lines: list[str] = []

    for line in lines:
        marker = MARKER_RE.search(line)
        if marker:
            scenarios.append(ScenarioStats(label=current_label, lines=current_lines))
            current_label = marker.group("label").strip()
            current_lines = [line]
            continue
        current_lines.append(line)

    scenarios.append(ScenarioStats(label=current_label, lines=current_lines))
    return [s for s in scenarios if s.label != "SEM_MARCADOR" or s.lines]


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera relatorio assistido da validacao de voz.")
    parser.add_argument(
        "--log",
        default="logs/voice_runtime.log",
        help="Caminho do log de runtime (default: logs/voice_runtime.log).",
    )
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        raise SystemExit(f"Log nao encontrado: {log_path}")

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    scenarios = split_scenarios(lines)
    if not scenarios:
        raise SystemExit("Nenhum bloco encontrado. Use /api/debug/voice-marker antes dos testes.")

    for index, scenario in enumerate(scenarios, start=1):
        print(f"TESTE {index} - {scenario.label}")
        print(f"- latency_max={scenario.max_reply_start_latency():.3f}s")
        print(f"- critical_latency={str(scenario.critical_latency()).lower()}")
        print(f"- timeouts={scenario.generate_reply_timeouts()}")
        print(f"- speech_started={scenario.reply_started()}")
        print(f"- speech_finished={scenario.reply_finished()}")
        print(f"- cancel_previous_speech={scenario.count('[VOICE] cancel_previous_speech')}")
        print(f"- queue_cleared={scenario.count('[VOICE] queue_cleared')}")
        print(f"- speech_timeout_recovered={scenario.count('[VOICE] speech_timeout_recovered')}")
        print(f"- begin_messages_action={scenario.count('[VOICE] begin_messages_action')}")
        print(f"- end_messages_action={scenario.count('[VOICE] end_messages_action')}")
        print(f"- possible_stall={str(scenario.possible_stall()).lower()}")
        print(f"- verdict={scenario.verdict()}")
        print("")


if __name__ == "__main__":
    main()

