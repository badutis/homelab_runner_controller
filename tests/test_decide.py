import unittest
from datetime import datetime, timedelta

from rackctl.decide import Controller, Machine, Observation, QueuedJob, Runner, Settings

T0 = datetime(2026, 10, 17, 12, 0, 0)
MIN = timedelta(minutes=1)

LIGHT = frozenset({"self-hosted", "Linux", "X64", "home", "light"})
NPM = frozenset({"self-hosted", "Linux", "X64", "home", "npm"})
CPP = frozenset({"self-hosted", "Linux", "X64", "home", "cpp"})
Q_LIGHT = frozenset({"self-hosted", "home", "light"})
Q_NPM = frozenset({"self-hosted", "home", "npm"})
Q_CPP = frozenset({"self-hosted", "home", "cpp"})


def machine(n, rank=1):
    return Machine(
        name=f"host{n}",
        mac=f"00:00:00:00:00:0{n}",
        host=f"192.0.2.{n}",
        ssh_user=f"host{n}",
        runner_prefixes=(f"host{n}-a-", f"host{n}-b-"),
        rank=rank,
    )


def runners(n, labels, count, online, busy=0):
    return [
        Runner(name=f"host{n}-a-{i + 1}", online=online, busy=online and i < busy, labels=labels)
        for i in range(count)
    ]


def obs(rs, queued=(), complete=True):
    jobs = tuple(q if isinstance(q, QueuedJob) else QueuedJob(labels=q) for q in queued)
    return Observation(runners=tuple(rs), queued=jobs, complete=complete)


def kinds(actions):
    return [(a.kind, a.machine) for a in actions]


class Fleet:
    """host1 ir host4 — light (i5), host5 — light (i3), host2 — npm (i5)."""

    def __init__(self):
        self.ctl = Controller(machines=(machine(1), machine(4), machine(5, rank=2), machine(2)))
        for s in self.ctl.state.values():
            s.last_online = T0  # savaitinis pažadinimas netrukdo testams

    def view(self, awake=(), busy=None, queued=(), complete=True):
        busy = busy or {}
        rs = []
        rs += runners(1, LIGHT, 4, 1 in awake, busy.get(1, 0))
        rs += runners(4, LIGHT, 4, 4 in awake, busy.get(4, 0))
        rs += runners(5, LIGHT, 3, 5 in awake, busy.get(5, 0))
        rs += runners(2, NPM, 4, 2 in awake, busy.get(2, 0))
        return obs(rs, queued, complete)


class WakeTest(unittest.TestCase):
    def test_nothing_queued_nothing_done(self):
        f = Fleet()
        self.assertEqual(f.ctl.step(f.view(), T0), [])

    def test_one_light_job_wakes_one_fast_machine(self):
        f = Fleet()
        self.assertEqual(kinds(f.ctl.step(f.view(queued=[Q_LIGHT]), T0)), [("wake", "host1")])

    def test_npm_job_wakes_only_the_npm_machine(self):
        f = Fleet()
        self.assertEqual(kinds(f.ctl.step(f.view(queued=[Q_NPM]), T0)), [("wake", "host2")])

    def test_enough_jobs_wake_more_machines_slow_one_last(self):
        f = Fleet()
        got = kinds(f.ctl.step(f.view(queued=[Q_LIGHT] * 9), T0))
        self.assertEqual(got, [("wake", "host1"), ("wake", "host4"), ("wake", "host5")])

    def test_free_awake_runner_takes_the_job(self):
        f = Fleet()
        f.ctl.step(f.view(awake=(1,)), T0)
        self.assertEqual(f.ctl.step(f.view(awake=(1,), queued=[Q_LIGHT]), T0 + MIN), [])

    def test_busy_runners_do_not_count(self):
        f = Fleet()
        f.ctl.step(f.view(awake=(1,), busy={1: 4}), T0)
        got = kinds(f.ctl.step(f.view(awake=(1,), busy={1: 4}, queued=[Q_LIGHT]), T0 + MIN))
        self.assertEqual(got, [("wake", "host4")])

    def test_waking_machine_is_not_woken_twice(self):
        f = Fleet()
        f.ctl.step(f.view(queued=[Q_LIGHT]), T0)
        self.assertEqual(f.ctl.step(f.view(queued=[Q_LIGHT]), T0 + MIN), [])

    def test_job_with_unknown_label_wakes_nothing(self):
        f = Fleet()
        self.assertEqual(f.ctl.step(f.view(queued=[Q_CPP]), T0), [])


class WakeFailureTest(unittest.TestCase):
    def test_retry_then_alert_then_back_off(self):
        f = Fleet()
        f.ctl.step(f.view(queued=[Q_NPM]), T0)
        self.assertEqual(kinds(f.ctl.step(f.view(queued=[Q_NPM]), T0 + 3 * MIN)), [("wake", "host2")])
        self.assertEqual(kinds(f.ctl.step(f.view(queued=[Q_NPM]), T0 + 6 * MIN)), [("alert", "host2")])
        self.assertEqual(f.ctl.step(f.view(queued=[Q_NPM]), T0 + 7 * MIN), [])
        self.assertEqual(kinds(f.ctl.step(f.view(queued=[Q_NPM]), T0 + 37 * MIN)), [("wake", "host2")])

    def test_waking_up_clears_the_attempts(self):
        f = Fleet()
        f.ctl.step(f.view(queued=[Q_NPM]), T0)
        f.ctl.step(f.view(awake=(2,)), T0 + MIN)
        s = f.ctl.state["host2"]
        self.assertEqual((s.wake_sent, s.wake_attempts), (None, 0))


class SleepTest(unittest.TestCase):
    def test_sleeps_after_thirty_idle_minutes_from_waking(self):
        f = Fleet()
        f.ctl.step(f.view(awake=(1,)), T0)
        self.assertEqual(f.ctl.step(f.view(awake=(1,)), T0 + 29 * MIN), [])
        self.assertEqual(kinds(f.ctl.step(f.view(awake=(1,)), T0 + 30 * MIN)), [("sleep", "host1")])

    def test_busy_restarts_the_idle_clock(self):
        f = Fleet()
        f.ctl.step(f.view(awake=(1,)), T0)
        f.ctl.step(f.view(awake=(1,), busy={1: 1}), T0 + 20 * MIN)
        self.assertEqual(f.ctl.step(f.view(awake=(1,)), T0 + 45 * MIN), [])
        self.assertEqual(kinds(f.ctl.step(f.view(awake=(1,)), T0 + 50 * MIN)), [("sleep", "host1")])

    def test_never_sleeps_while_a_runner_is_busy(self):
        f = Fleet()
        f.ctl.step(f.view(awake=(1,), busy={1: 1}), T0)
        self.assertEqual(f.ctl.step(f.view(awake=(1,), busy={1: 1}), T0 + 120 * MIN), [])

    def test_does_not_sleep_with_its_label_in_the_queue(self):
        f = Fleet()
        f.ctl.step(f.view(awake=(1,), busy={1: 4}), T0)
        # Visi užimti 40 min, o eilėje dar vienas — pažadinama kita, ši nemigdoma.
        got = kinds(f.ctl.step(f.view(awake=(1,), queued=[Q_LIGHT]), T0 + 40 * MIN))
        self.assertNotIn(("sleep", "host1"), got)

    def test_sleep_is_not_resent_every_poll(self):
        # GitHub dar kurį laiką rodo užmigusios mašinos runner'ius prisijungusius.
        f = Fleet()
        f.ctl.step(f.view(awake=(1,)), T0)
        f.ctl.step(f.view(awake=(1,)), T0 + 30 * MIN)
        self.assertEqual(f.ctl.step(f.view(awake=(1,)), T0 + 35 * MIN), [])
        self.assertEqual(kinds(f.ctl.step(f.view(awake=(1,)), T0 + 40 * MIN)), [("sleep", "host1")])

    def test_cpp_runner_on_the_same_machine_keeps_it_awake(self):
        ctl = Controller(machines=(machine(1),))
        ctl.state["host1"].last_online = T0
        cpp_busy = [Runner("host1-b-1", True, True, CPP)]
        view = obs(runners(1, LIGHT, 4, True) + cpp_busy)
        ctl.step(view, T0)
        self.assertEqual(ctl.step(view, T0 + 60 * MIN), [])


class WeeklyTest(unittest.TestCase):
    def test_wakes_a_machine_not_seen_for_a_week_and_holds_it_an_hour(self):
        ctl = Controller(machines=(machine(1),))
        ctl.state["host1"].last_online = T0
        asleep = obs(runners(1, LIGHT, 4, False))
        awake = obs(runners(1, LIGHT, 4, True))
        self.assertEqual(ctl.step(asleep, T0 + timedelta(days=6)), [])
        week = T0 + timedelta(days=7)
        self.assertEqual(kinds(ctl.step(asleep, week)), [("wake", "host1")])
        ctl.step(awake, week + MIN)
        self.assertEqual(ctl.step(awake, week + 50 * MIN), [])
        self.assertEqual(kinds(ctl.step(awake, week + 62 * MIN)), [("sleep", "host1")])

    def test_unknown_last_online_wakes_once(self):
        ctl = Controller(machines=(machine(1),))
        asleep = obs(runners(1, LIGHT, 4, False))
        self.assertEqual(kinds(ctl.step(asleep, T0)), [("wake", "host1")])
        self.assertEqual(ctl.step(asleep, T0 + MIN), [])


class ReviewFindingsTest(unittest.TestCase):
    """Peržiūros (2026-10-17) radiniai, po vieną testą kiekvienam."""

    def test_job_needing_a_label_no_runner_has_wakes_nothing_and_keeps_nothing_awake(self):
        f = Fleet()
        gpu = frozenset({"self-hosted", "home", "light", "gpu"})
        self.assertEqual(f.ctl.step(f.view(queued=[gpu]), T0), [])
        f.ctl.step(f.view(awake=(1,)), T0)
        got = kinds(f.ctl.step(f.view(awake=(1,), queued=[gpu]), T0 + 30 * MIN))
        self.assertEqual(got, [("sleep", "host1")])

    def test_runner_of_another_project_does_not_cover_the_queue(self):
        ctl = Controller(machines=(machine(1), machine(2)))
        for s in ctl.state.values():
            s.last_online = T0
        rs = [Runner("host1-a-1", True, False, LIGHT, source="a")] + [
            Runner(f"host2-a-{i}", False, False, LIGHT, source="b") for i in range(1, 3)
        ]
        job = QueuedJob(labels=Q_LIGHT, source="b")
        self.assertEqual(kinds(ctl.step(obs(rs, [job]), T0)), [("wake", "host2")])

    def test_incomplete_observation_wakes_but_never_sleeps(self):
        f = Fleet()
        f.ctl.step(f.view(awake=(1,)), T0)
        self.assertEqual(f.ctl.step(f.view(awake=(1,), complete=False), T0 + 60 * MIN), [])
        got = kinds(f.ctl.step(f.view(queued=[Q_NPM], complete=False), T0 + 61 * MIN))
        self.assertIn(("wake", "host2"), got)

    def test_stale_queued_job_does_not_keep_the_machine_awake(self):
        f = Fleet()
        f.ctl.step(f.view(awake=(1,)), T0)
        stale = QueuedJob(labels=Q_LIGHT, since=T0 - 60 * MIN)
        got = kinds(f.ctl.step(f.view(awake=(1,), queued=[stale]), T0 + 30 * MIN))
        self.assertIn(("sleep", "host1"), got)

    def test_machine_just_put_to_sleep_is_not_counted_as_free(self):
        ctl = Controller(machines=(machine(1),))
        ctl.state["host1"].last_online = T0
        awake = obs(runners(1, LIGHT, 4, True))
        ctl.step(awake, T0)
        self.assertEqual(kinds(ctl.step(awake, T0 + 30 * MIN)), [("sleep", "host1")])
        # Runner'iai dar „online“, bet mašina jau užmiga: job'as turi ją pažadinti.
        got = kinds(ctl.step(obs(runners(1, LIGHT, 4, True), [Q_LIGHT]), T0 + 31 * MIN))
        self.assertEqual(got, [("wake", "host1")])

    def test_old_wake_is_forgotten_once_awake(self):
        f = Fleet()
        f.ctl.step(f.view(queued=[Q_NPM]), T0)
        f.ctl.step(f.view(awake=(2,)), T0 + MIN)
        f.ctl.step(f.view(awake=(2,), busy={2: 1}), T0 + 10 * MIN)
        f.ctl.step(f.view(awake=(2,)), T0 + 50 * MIN)  # užmigdoma
        # Užmigo: jokio „pakartotinio“ pažadinimo be priežasties.
        self.assertEqual(f.ctl.step(f.view(), T0 + 60 * MIN), [])

    def test_wake_sent_to_an_awake_machine_is_forgotten(self):
        # Pažadinimas išsiųstas, kol mašina dar buvo „užmigdoma“, bet ji neužmigo ir dirbo.
        ctl = Controller(machines=(machine(1),))
        ctl.state["host1"].last_online = T0
        awake = obs(runners(1, LIGHT, 4, True))
        ctl.step(awake, T0)
        ctl.step(awake, T0 + 30 * MIN)  # užmigdoma
        ctl.step(obs(runners(1, LIGHT, 4, True), [Q_LIGHT]), T0 + 31 * MIN)  # pažadinama
        ctl.step(obs(runners(1, LIGHT, 4, True, busy=1)), T0 + 40 * MIN)
        # Po valandos mašina dingsta pati (perkrovimas), be valdiklio užmigdymo:
        # senas pažadinimas neturi sukelti „pakartotinio“.
        self.assertEqual(ctl.step(obs(runners(1, LIGHT, 4, False)), T0 + 100 * MIN), [])

    def test_dry_run_never_retries_or_alerts(self):
        f = Fleet()
        f.ctl.dry_run = True
        f.ctl.step(f.view(queued=[Q_NPM]), T0)
        self.assertEqual(f.ctl.step(f.view(queued=[Q_NPM]), T0 + 3 * MIN), [])
        self.assertEqual(f.ctl.step(f.view(queued=[Q_NPM]), T0 + 6 * MIN), [])

    def test_dead_machine_alerts_once_per_quiet_period(self):
        ctl = Controller(machines=(machine(1),))
        asleep = obs(runners(1, LIGHT, 4, False))
        alerts = 0
        t = T0
        for _ in range(int(5 * 60 / 1)):  # 5 val. kas minutę
            alerts += sum(1 for a in ctl.step(asleep, t) if a.kind == "alert")
            t += MIN
        self.assertEqual(alerts, 1)


if __name__ == "__main__":
    unittest.main()
