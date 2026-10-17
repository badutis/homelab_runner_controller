"""Kada pažadinti ir kada užmigdyti mašiną. Gryna logika: nei tinklo, nei laiko, nei disko.

Įėjimas — ką šiuo metu rodo GitHub (runner'iai ir job'ai eilėje) ir valdiklio atmintis;
išėjimas — veiksmų sąrašas. Taip logiką galima patikrinti testais be GitHub, be
Wake-on-LAN ir be SSH (`tests/test_decide.py`).

Job'ą runner'is gali paimti tik tada, kai jie iš to paties šaltinio (projekto) ir runner'is
turi VISAS job'o etiketes — taip, kaip skirsto pats GitHub.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, List, Optional, Tuple


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
    source: str = ""


@dataclass(frozen=True)
class QueuedJob:
    labels: FrozenSet[str]  # mažosiomis raidėmis
    since: Optional[datetime] = None  # kada job'as sukurtas
    source: str = ""


@dataclass(frozen=True)
class Observation:
    runners: Tuple[Runner, ...]
    queued: Tuple[QueuedJob, ...]
    # False, kai bent vieno šaltinio nepavyko perskaityti: tada nežinoma, ar kuris nors
    # tos mašinos runner'is dirba, todėl niekas nemigdoma (žadinti galima).
    complete: bool = True


@dataclass(frozen=True)
class Settings:
    idle: timedelta = timedelta(minutes=30)
    wake_timeout: timedelta = timedelta(minutes=3)
    wake_attempts: int = 2
    failed_backoff: timedelta = timedelta(minutes=30)
    alert_quiet: timedelta = timedelta(hours=6)
    # Kiek laiko po užmigdymo komandos mašinos runner'iai dar gali atrodyti prisijungę.
    sleep_grace: timedelta = timedelta(minutes=5)
    sleep_retry: timedelta = timedelta(minutes=10)
    # Ilgiau eilėje laukiantis job'as (GitHub jo niekam neatiduoda) mašinos nebelaiko.
    stale_queue: timedelta = timedelta(minutes=30)
    weekly: timedelta = timedelta(days=7)
    weekly_hold: timedelta = timedelta(hours=1)


@dataclass
class MachineState:
    awake: bool = False
    last_busy: Optional[datetime] = None
    wake_sent: Optional[datetime] = None
    wake_attempts: int = 0
    failed_until: Optional[datetime] = None
    last_alert: Optional[datetime] = None
    sleep_sent: Optional[datetime] = None
    hold_until: Optional[datetime] = None
    weekly_pending: bool = False
    # Paskutinis kartas, kai bent vienas runner'is buvo prisijungęs. Saugoma diske kartu su
    # hold_until: GitHub ištrina runner'į, neprisijungusį 14 dienų (docs/valdymas.md).
    last_online: Optional[datetime] = None


@dataclass(frozen=True)
class Action:
    kind: str  # "wake" | "sleep" | "alert"
    machine: str
    reason: str


def can_take(runner: Runner, job: QueuedJob) -> bool:
    return runner.source == job.source and job.labels <= frozenset(l.lower() for l in runner.labels)


@dataclass
class Controller:
    machines: Tuple[Machine, ...]
    settings: Settings = field(default_factory=Settings)
    state: Dict[str, MachineState] = field(default_factory=dict)
    # Stebėjimo režime niekas nesiunčiama, todėl „nepabudo“ nėra gedimas.
    dry_run: bool = False

    def __post_init__(self):
        for m in self.machines:
            self.state.setdefault(m.name, MachineState())

    # --- pagalbinės -------------------------------------------------------------------

    def _runners_of(self, machine: Machine, obs: Observation) -> List[Runner]:
        return [r for r in obs.runners if machine.owns(r.name)]

    def _machine_of(self, runner_name: str) -> Optional[Machine]:
        for m in self.machines:
            if m.owns(runner_name):
                return m
        return None

    def _going_to_sleep(self, name: str, now: datetime) -> bool:
        s = self.state[name]
        return s.sleep_sent is not None and now - s.sleep_sent < self.settings.sleep_grace

    def _usable(self, name: str, now: datetime) -> bool:
        """Pabudusi ir neužmiganti: jos laisvi runner'iai tikrai gali imti darbą."""
        return self.state[name].awake and not self._going_to_sleep(name, now)

    def _waking(self, name: str, now: datetime) -> bool:
        s = self.state[name]
        return (
            not self._usable(name, now)
            and s.wake_sent is not None
            and now - s.wake_sent < self.settings.wake_timeout
        )

    def _fresh(self, obs: Observation, now: datetime) -> List[QueuedJob]:
        """Job'ai, kuriuos GitHub dar gali atiduoti: ilgiau laukiantis greičiausiai įstrigo
        (pvz. runner'ių grupė neleidžia jo repozitorijai) ir mašinų nei žadina, nei laiko."""
        return [j for j in obs.queued if j.since is None or now - j.since < self.settings.stale_queue]

    def _alert(self, s: MachineState, name: str, reason: str, now: datetime, actions: List[Action]) -> None:
        if s.last_alert is None or now - s.last_alert >= self.settings.alert_quiet:
            s.last_alert = now
            actions.append(Action("alert", name, reason))

    def _send_wake(self, m: Machine, reason: str, now: datetime, actions: List[Action]) -> None:
        s = self.state[m.name]
        s.wake_sent = now
        s.wake_attempts = 1
        if s.sleep_sent is not None:
            # Pažadinama ką tik užmigdyta: tuščiosios eigos laikas skaičiuojamas iš naujo.
            s.sleep_sent = None
            s.last_busy = now
        actions.append(Action("wake", m.name, reason))

    # --- sprendimas -------------------------------------------------------------------

    def step(self, obs: Observation, now: datetime) -> List[Action]:
        actions: List[Action] = []
        self._observe(obs, now, actions)
        self._wake_for_queue(obs, now, actions)
        self._weekly(now, actions)
        if obs.complete:
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
                # Pabudusiai mašinai senas pažadinimas nebeaktualus — kitaip, kai ji vėliau
                # užmigtų, valdiklis ją tuoj pat žadintų „pakartotinai“.
                if s.wake_sent is not None and now - s.wake_sent >= self.settings.wake_timeout:
                    s.wake_sent = None
                    s.wake_attempts = 0
            if any(r.busy for r in runners):
                s.last_busy = now
            if (
                not awake
                and s.wake_sent is not None
                and now - s.wake_sent >= self.settings.wake_timeout
            ):
                if s.wake_attempts < self.settings.wake_attempts and not self.dry_run:
                    s.wake_sent = now
                    s.wake_attempts += 1
                    actions.append(Action("wake", m.name, "pakartotinai: anksčiau nepabudo"))
                else:
                    s.wake_sent = None
                    s.wake_attempts = 0
                    s.failed_until = now + self.settings.failed_backoff
                    s.weekly_pending = False
                    if not self.dry_run:
                        self._alert(s, m.name, f"nepabudo po {self.settings.wake_attempts} bandymų", now, actions)

    def _wake_for_queue(self, obs: Observation, now: datetime, actions: List[Action]) -> None:
        demand: Dict[Tuple[str, FrozenSet[str]], int] = {}
        for job in self._fresh(obs, now):
            key = (job.source, job.labels)
            demand[key] = demand.get(key, 0) + 1
        for key in sorted(demand, key=lambda k: (k[0], sorted(k[1]))):
            probe = QueuedJob(labels=key[1], source=key[0])
            have = 0
            for r in obs.runners:
                if not (r.online and not r.busy and can_take(r, probe)):
                    continue
                owner = self._machine_of(r.name)
                if owner is None or self._usable(owner.name, now):
                    have += 1
            coming = 0
            candidates = []
            for m in sorted(self.machines, key=lambda m: (m.rank, m.name)):
                slots = sum(1 for r in self._runners_of(m, obs) if can_take(r, probe))
                if slots == 0:
                    continue
                s = self.state[m.name]
                if self._waking(m.name, now):
                    coming += slots
                elif not self._usable(m.name, now) and (s.failed_until is None or now >= s.failed_until):
                    candidates.append((m, slots))
            missing = demand[key] - have - coming
            labels = ",".join(sorted(key[1] - {"self-hosted"}))
            for m, slots in candidates:
                if missing <= 0:
                    break
                missing -= slots
                self._send_wake(m, f"eilėje {demand[key]} job'ai su `{labels}`", now, actions)

    def _weekly(self, now: datetime, actions: List[Action]) -> None:
        for m in self.machines:
            s = self.state[m.name]
            if s.awake or self._waking(m.name, now):
                continue
            if s.failed_until is not None and now < s.failed_until:
                continue
            if s.last_online is None or now - s.last_online >= self.settings.weekly:
                self._send_wake(m, "savaitinis: runner'iai turi prisijungti", now, actions)
                s.weekly_pending = True

    def _sleep_idle(self, obs: Observation, now: datetime, actions: List[Action]) -> None:
        fresh = self._fresh(obs, now)
        for m in self.machines:
            s = self.state[m.name]
            if not s.awake:
                continue
            runners = self._runners_of(m, obs)
            if any(r.busy for r in runners):
                continue
            if any(can_take(r, j) for r in runners if r.online for j in fresh):
                continue
            if s.hold_until is not None and now < s.hold_until:
                continue
            if s.last_busy is None or now - s.last_busy < self.settings.idle:
                continue
            if s.sleep_sent is not None and now - s.sleep_sent < self.settings.sleep_retry:
                continue
            s.sleep_sent = now
            s.wake_sent = None
            s.wake_attempts = 0
            idle_min = int((now - s.last_busy).total_seconds() // 60)
            actions.append(Action("sleep", m.name, f"be darbo {idle_min} min"))
