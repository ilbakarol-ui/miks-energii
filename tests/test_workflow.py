"""B-20261116-1: zapasowe starty workflow (GitHub opóźnia albo pomija zdarzenia harmonogramu) – bramki uruchamiane
naprawdę w tymczasowym repozytorium git."""
import datetime as dt
import os
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _wf(name):
    d = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    return d, d.get("on", d.get(True))


def _step(wf, job, step_id):
    return next(s for s in wf["jobs"][job]["steps"] if s.get("id") == step_id)


def _today():
    """Data z `date -u`, jak w bramkach (testy z FAKE_NOW przestawiają zegar Pythona, nie powłoki)."""
    out = subprocess.run(["date", "-u", "+%Y-%m-%d"], capture_output=True, text=True, check=True).stdout.strip()
    return dt.date.fromisoformat(out)


def _repo(tmp_path, commits=(), prices=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for code, last in (prices or {}).items():
        (tmp_path / "data" / "prices").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "prices" / f"{code}.csv").write_text(f"date,avg\n{last},50\n")
    for msg in commits or ["start"]:
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", msg], check=True, env=env)
    return tmp_path


def _gate(script, cwd, **env):
    out = cwd / "gh_output"
    out.write_text("")
    subprocess.run(["bash", "-e", "-c", script], cwd=cwd, check=True,
                   env={**os.environ, "GITHUB_OUTPUT": str(out), **env}, capture_output=True)
    return dict(line.split("=", 1) for line in out.read_text().split())["run"]


def test_daily_every_cron_has_a_branch_and_backups_exist():
    wf, on = _wf("daily.yml")
    crons = [c["cron"] for c in on["schedule"]]
    assert {"53 0 * * *", "37 12 * * *", "7 14 * * *", "23 3 * * *", "13 15 * * *"} == set(crons)
    assert wf["jobs"]["update"]["steps"][0]["with"]["fetch-depth"] >= 20
    text = (WF / "daily.yml").read_text(encoding="utf-8")
    # zapas popołudniowy działa jak przebieg uzupełniający: popołudnie, tylko brakujące ceny, dopisek w commicie
    for key in ("AFTERNOON:", "ONLY_MISSING:", "SUFFIX:"):
        line = next(x for x in text.splitlines() if x.strip().startswith(key))
        assert "'13 15 * * *'" in line and "'23 3 * * *'" not in line


def test_daily_night_backup_gate(tmp_path):
    wf, _ = _wf("daily.yml")
    script = _step(wf, "update", "need")["run"]
    t = _today().isoformat()
    done = _repo(tmp_path / "a", [f"Dane z {t}", f"Wspólna pogoda temp {t}"])
    assert _gate(script, done, SCHEDULE="23 3 * * *") == "0"
    late = _repo(tmp_path / "b", [f"Dane z {(_today() - dt.timedelta(days=1)).isoformat()}", f"Dane z {t} (popołudnie)"])
    assert _gate(script, late, SCHEDULE="23 3 * * *") == "1"
    assert _gate(script, done, SCHEDULE="53 0 * * *") == "1"      # zwykłe starty zawsze liczą


def test_daily_afternoon_backup_gate(tmp_path):
    wf, _ = _wf("daily.yml")
    script = _step(wf, "update", "need")["run"]
    tomorrow = (_today() + dt.timedelta(days=1)).isoformat()
    full = _repo(tmp_path / "a", prices={"PL": tomorrow, "DE_LU": tomorrow})
    part = _repo(tmp_path / "b", prices={"PL": tomorrow, "DE_LU": _today().isoformat()})
    for sched in ("7 14 * * *", "13 15 * * *"):
        assert _gate(script, full, SCHEDULE=sched) == "0"
        assert _gate(script, part, SCHEDULE=sched) == "1"


def test_shared_weather_backup_gates(tmp_path):
    wf, on = _wf("shared_weather.yml")
    crons = {c["cron"] for c in on["schedule"]}
    assert {"17 0 * * *", "47 17 * * *", "41 3 * * *", "53 18 * * *"} == crons
    job = wf["jobs"]["weather"]
    assert "'53 18 * * *'" in job["strategy"]["matrix"]["job"]            # zapas wieczorny = zadanie evening
    later = [s for s in job["steps"][2:]]
    assert all("steps.need.outputs.run == '1'" in str(s.get("if", "")) for s in later)
    script = _step(wf, "weather", "need")["run"]
    t = _today().isoformat()
    repo = _repo(tmp_path, [f"Wspólna pogoda extra {t}", f"Dane z {t}"])
    assert _gate(script, repo, SCHEDULE="41 3 * * *", JOB="extra") == "0"
    assert _gate(script, repo, SCHEDULE="41 3 * * *", JOB="temp") == "1"   # temp dziś nie zapisało
    assert _gate(script, repo, SCHEDULE="53 18 * * *", JOB="evening") == "1"
    assert _gate(script, repo, SCHEDULE="17 0 * * *", JOB="temp") == "1"
