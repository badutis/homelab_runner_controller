"""GitHub API skaitymas: runner'iai ir job'ai eilėje. Tik GET, tik išeinantys ryšiai.

Kiekvienas atsakymas saugomas su ETag: pakartotinė užklausa su `If-None-Match` grąžina
304 ir neskaičiuojama į GitHub užklausų limitą, todėl kas 20 s klausti galima.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from .decide import Observation, Runner

API = "https://api.github.com"


@dataclass(frozen=True)
class Source:
    """Viena vieta, kurioje registruoti runner'iai, ir repozitorijos, kurių job'ai juos naudoja."""

    runners_path: str  # pvz. "repos/example-org/app" arba "orgs/example-org2"
    repos: Tuple[str, ...]  # pvz. ("example-org/app",)
    token: str


class GitHub:
    def __init__(self, sources: List[Source], opener=None):
        self.sources = sources
        self._open = opener or urllib.request.urlopen
        self._cache: Dict[str, Tuple[str, object]] = {}
        self._touched: set = set()

    def _get(self, path: str, token: str) -> object:
        url = f"{API}/{path}"
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        self._touched.add(url)
        cached = self._cache.get(url)
        if cached:
            req.add_header("If-None-Match", cached[0])
        try:
            with self._open(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                etag = resp.headers.get("ETag")
                if etag:
                    self._cache[url] = (etag, body)
                return body
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached:
                return cached[1]
            raise

    def _pages(self, path: str, key: str, token: str) -> List[dict]:
        out: List[dict] = []
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            body = self._get(f"{path}{sep}per_page=100&page={page}", token)
            items = body.get(key, []) if isinstance(body, dict) else []
            out.extend(items)
            if len(items) < 100:
                return out
            page += 1

    def observe(self) -> Observation:
        self._touched = set()
        runners: List[Runner] = []
        queued: List[FrozenSet[str]] = []
        for src in self.sources:
            for r in self._pages(f"{src.runners_path}/actions/runners", "runners", src.token):
                runners.append(
                    Runner(
                        name=r["name"],
                        online=r.get("status") == "online",
                        busy=bool(r.get("busy")),
                        labels=frozenset(l["name"] for l in r.get("labels", [])),
                    )
                )
            for repo in src.repos:
                queued.extend(self._queued_jobs(repo, src.token))
        # Baigtų paleidimų atsakymų nebeprireiks; be šito atmintis augtų be ribos.
        self._cache = {k: v for k, v in self._cache.items() if k in self._touched}
        return Observation(runners=tuple(runners), queued=tuple(queued))

    def _queued_jobs(self, repo: str, token: str) -> List[FrozenSet[str]]:
        out: List[FrozenSet[str]] = []
        # Darbas eilėje gali būti ir dar nepradėtame (`queued`), ir jau vykstančiame
        # (`in_progress`) paleidime, kai dalis jo job'ų jau gavo runner'į.
        for status in ("queued", "in_progress"):
            runs = self._pages(f"repos/{repo}/actions/runs?status={status}", "workflow_runs", token)
            for run in runs:
                jobs = self._pages(f"repos/{repo}/actions/runs/{run['id']}/jobs", "jobs", token)
                for job in jobs:
                    labels = frozenset(l.lower() for l in job.get("labels", []))
                    if job.get("status") == "queued" and "self-hosted" in labels:
                        out.append(labels)
        return out


def read_token(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        raise ValueError(f"tuščias raktas: {path}")
    return token


def first_error(exc: BaseException) -> Optional[str]:
    if isinstance(exc, urllib.error.HTTPError):
        return f"GitHub {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"tinklas: {exc.reason}"
    return None
