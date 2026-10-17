"""Kada pažadinti ir kada užmigdyti mašiną. Gryna logika: nei tinklo, nei laiko, nei disko.

Įėjimas — ką šiuo metu rodo GitHub (runner'iai ir job'ai eilėje) ir valdiklio atmintis;
išėjimas — veiksmų sąrašas. Taip logiką galima patikrinti testais be GitHub, be
Wake-on-LAN ir be SSH (`tests/test_decide.py`).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, List, Optional, Tuple

# Etiketės, kurias turi kiekvienas savas runner'is; darbo rūšies jos nenusako.
COMMON_LABELS = frozenset({"self-hosted", "home", "linux", "x64"})


@dataclass(frozen=True)
class Machine:
    name: str
    mac: str
    host: str
    ssh_user: str
    runner_prefixes: Tuple[str, ...]
    rank: int  # mažesnis žadinamas pirmiau (i5 prieš i3)

    def owns(self, runner_name: str) -> bool:
        return any(runner_name.startswith(p) for p in self.runner_prefixes)


@dataclass(frozen=True)
class Runner:
    name: str
    online: bool
    busy: bool
    labels: FrozenSet[str]


@dataclass(frozen=True)
class Observation:
    runners: Tuple[Runner, ...]
    # Kiekvieno eilėje laukiančio savo runner'io job'o etiketės (mažosiomis raidėmis).
    queued: Tuple[FrozenSet[str], ...]


@dataclass(frozen=True)
class Settings:
    idle: timedelta = timedelta(minutes=30)
    wake_timeout: timedelta = timedelta(minutes=3)
    wake_attempts: int = 2
    failed_backoff: timedelta = timedelta(minutes=30)
    sleep_retry: timedelta = timedelta(minutes=5)
    weekly: timedelta = timedelta(days=7)
    weekly_hold: timedelta = timedelta(hours=1)


@dataclass
class MachineState:
    awake: bool = False
    last_busy: Optional[datetime] = None
    wake_sent: Optional[datetime] = None
    wake_attempts: int = 0
    failed_until: Optional[datetime] = None
    sleep_sent: Optional[datetime] = None
    hold_until: Optional[datetime] = None
    weekly_pending: bool = False
    # Paskutinis kartas, kai bent vienas runner'is buvo prisijungęs. Saugoma diske:
    # GitHub ištrina runner'į, neprisijungusį 14 dienų (žr. docs/valdymas.md).
    last_online: Optional[datetime] = None


@dataclass(frozen=True)
class Action:
    kind: str  # "wake" | "sleep" | "alert"
    machine: str
    reason: str


@dataclass
class Controller:
    machines: Tuple[Machine, ...]
    settings: Settings = field(default_factory=Settings)
    state: Dict[str, MachineState] = field(default_factory=dict)

    def __post_init__(self):
        for m in self.machines:
            self.state.setdefault(m.name, MachineState())

    # --- pagalbinės -------------------------------------------------------------------

    def _runners_of(self, machine: Machine, obs: Observation) -> List[Runner]:
        return [r for r in obs.runners if machine.owns(r.name)]

    def _work_labels(self, runner: Runner) -> FrozenSet[str]:
        return frozenset(l.lower() for l in runner.labels) - COMMON_LABELS

    def _waking(self, name: str, now: datetime) -> bool:
        s = self.state[name]
        return (
            not s.awake
            and s.wake_sent is not None
            and now - s.wake_sent < self.settings.wake_timeout
        )

    # --- sprendimas -------------------------------------------------------------------

    def step(self, obs: Observation, now: datetime) -> List[Action]:
        actions: List[Action] = []
        self._observe(obs, now, actions)
        self._wake_for_queue(obs, now, actions)
        self._weekly(now, actions)
        self._sleep_idle(obs, now, actions)
        return actions

    def _observe(self, obs: Observation, now: datetime, actions: List[Action]) -> None:
        for m in self.machines:
            s = self.state[m.name]
            runners = self._runners_of(m, obs)
            awake = any(r.online for r in runners)
            if awake and not s.awake:
                # Ką tik pabudo: tuščiosios eigos laikas skaičiuojamas nuo pabudimo.
                s.last_busy = now
                s.wake_sent = None
                s.wake_attempts = 0
                s.failed_until = None
                s.sleep_sent = None
                if s.weekly_pending:
                    s.hold_until = now + self.settings.weekly_hold
                    s.weekly_pending = False
            if not awake and s.awake:
                s.sleep_sent = None
            s.awake = awake
            if awake:
                s.last_online = now
            if any(r.busy for r in runners):
                s.last_busy = now
            # Pažadinimas nesuveikė: kartoti arba pranešti.
            if (
                not awake
                and s.wake_sent is not None
                and now - s.wake_sent >= self.settings.wake_timeout
            ):
                if s.wake_attempts < self.settings.wake_attempts:
                    s.wake_sent = now
                    s.wake_attempts += 1
                    actions.append(Action("wake", m.name, "pakartotinai: anksčiau nepabudo"))
                else:
                    s.wake_sent = None
                    s.wake_attempts = 0
                    s.failed_until = now + self.settings.failed_backoff
                    actions.append(
                        Action("alert", m.name, f"nepabudo po {self.settings.wake_attempts} bandymų")
                    )

    def _wake_for_queue(self, obs: Observation, now: datetime, actions: List[Action]) -> None:
        demand: Dict[str, int] = {}
        for labels in obs.queued:
            for label in labels - COMMON_LABELS:
                demand[label] = demand.get(label, 0) + 1
        for label in sorted(demand):
            have = sum(
                1
                for r in obs.runners
                if r.online and not r.busy and label in self._work_labels(r)
            )
            coming = 0
            candidates = []
            for m in sorted(self.machines, key=lambda m: (m.rank, m.name)):
                slots = sum(1 for r in self._runners_of(m, obs) if label in self._work_labels(r))
                if slots == 0:
                    continue
                s = self.state[m.name]
                if self._waking(m.name, now):
                    coming += slots
                elif not s.awake and (s.failed_until is None or now >= s.failed_until):
                    candidates.append((m, slots))
            missing = demand[label] - have - coming
            for m, slots in candidates:
                if missing <= 0:
                    break
                s = self.state[m.name]
                s.wake_sent = now
                s.wake_attempts = 1
                missing -= slots
                actions.append(Action("wake", m.name, f"eilėje {demand[label]} job'ai su `{label}`"))

    def _weekly(self, now: datetime, actions: List[Action]) -> None:
        for m in self.machines:
            s = self.state[m.name]
            if s.awake or self._waking(m.name, now):
                continue
            if s.failed_until is not None and now < s.failed_until:
                continue
            if s.last_online is None or now - s.last_online >= self.settings.weekly:
                s.wake_sent = now
                s.wake_attempts = 1
                s.weekly_pending = True
                actions.append(Action("wake", m.name, "savaitinis: runner'iai turi prisijungti"))

    def _sleep_idle(self, obs: Observation, now: datetime, actions: List[Action]) -> None:
        wanted = set()
        for labels in obs.queued:
            wanted |= labels - COMMON_LABELS
        for m in self.machines:
            s = self.state[m.name]
            if not s.awake:
                continue
            runners = self._runners_of(m, obs)
            if any(r.busy for r in runners):
                continue
            if any(self._work_labels(r) & wanted for r in runners if r.online):
                continue
            if s.hold_until is not None and now < s.hold_until:
                continue
            if s.last_busy is None or now - s.last_busy < self.settings.idle:
                continue
            if s.sleep_sent is not None and now - s.sleep_sent < self.settings.sleep_retry:
                continue
            s.sleep_sent = now
            actions.append(Action("sleep", m.name, f"be darbo {int((now - s.last_busy).total_seconds() // 60)} min"))
