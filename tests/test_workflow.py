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
    # B-20261119-1: także główny start nocny pomija się, gdy dzisiejsze dane już są (spóźnione zdarzenie)
    assert _gate(script, done, SCHEDULE="53 0 * * *") == "0"
    assert _gate(script, late, SCHEDULE="53 0 * * *") == "1"
    assert _gate(script, done, SCHEDULE="") == "1"                # start ręczny zawsze liczy


def test_daily_afternoon_backup_gate(tmp_path):
    wf, _ = _wf("daily.yml")
    script = _step(wf, "update", "need")["run"]
    tomorrow = (_today() + dt.timedelta(days=1)).isoformat()
    full = _repo(tmp_path / "a", prices={"PL": tomorrow, "DE_LU": tomorrow})
    part = _repo(tmp_path / "b", prices={"PL": tomorrow, "DE_LU": _today().isoformat()})
    for sched in ("7 14 * * *", "13 15 * * *"):
        assert _gate(script, full, SCHEDULE=sched, GATE_HOUR="14") == "0"
        assert _gate(script, part, SCHEDULE=sched, GATE_HOUR="14") == "1"
        assert _gate(script, part, SCHEDULE=sched, GATE_HOUR="01") == "0"   # B-20261124-1: spóźnione za północ


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
    assert _gate(script, repo, SCHEDULE="53 18 * * *", JOB="evening", GATE_HOUR="18") == "1"
    assert _gate(script, repo, SCHEDULE="17 0 * * *", JOB="temp") == "1"
    assert _gate(script, repo, SCHEDULE="17 0 * * *", JOB="extra") == "0"   # B-20261119-1: zapas 03:41 był pierwszy
    # B-20261119-1: wieczorny start po północy jest pomijany; start ręczny – nie
    assert _gate(script, repo, SCHEDULE="53 18 * * *", JOB="evening", GATE_HOUR="01") == "0"
    assert _gate(script, repo, SCHEDULE="", JOB="evening", GATE_HOUR="01") == "1"


def _git(*a, cwd=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *a], cwd=cwd, check=True, env=env, capture_output=True)


def _late_clone(tmp_path, msg):
    """Klon z chwili zdarzenia, a potem nowy commit w origin – jak spóźnione zdarzenie harmonogramu (B-20261119-1)."""
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    seed = _repo(tmp_path / "seed", ["start"])
    _git("-C", str(seed), "branch", "-M", "main")
    _git("-C", str(seed), "push", "-q", str(origin), "main")
    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone))
    _git("-C", str(seed), "commit", "-q", "--allow-empty", "-m", msg)
    _git("-C", str(seed), "push", "-q", str(origin), "main")
    return clone


def test_daily_gate_sees_origin_after_delay(tmp_path):
    wf, _ = _wf("daily.yml")
    script = _step(wf, "update", "need")["run"]
    clone = _late_clone(tmp_path, f"Dane z {_today().isoformat()}")
    for sched in ("53 0 * * *", "23 3 * * *"):
        assert _gate(script, clone, SCHEDULE=sched) == "0"


def test_shared_weather_gate_sees_origin_after_delay(tmp_path):
    wf, _ = _wf("shared_weather.yml")
    script = _step(wf, "weather", "need")["run"]
    clone = _late_clone(tmp_path, f"Wspólna pogoda temp {_today().isoformat()}")
    assert _gate(script, clone, SCHEDULE="41 3 * * *", JOB="temp") == "0"
    assert _gate(script, clone, SCHEDULE="17 0 * * *", JOB="temp") == "0"
    assert _gate(script, clone, SCHEDULE="17 0 * * *", JOB="extra") == "1"


def test_daily_afternoon_main_gate(tmp_path):
    wf, _ = _wf("daily.yml")
    script = _step(wf, "update", "need")["run"]
    t, tm = _today().isoformat(), (_today() + dt.timedelta(days=1)).isoformat()
    full = _repo(tmp_path / "a", [f"Dane z {t} (popołudnie)"], prices={"PL": tm, "NL": tm})
    part = _repo(tmp_path / "b", [f"Dane z {t} (popołudnie)"], prices={"PL": tm, "NL": t})
    none_ = _repo(tmp_path / "c", [f"Dane z {t}"], prices={"PL": tm, "NL": tm})
    # B-20261124-2: godzina bramki podana wprost – wynik nie zależy od godziny uruchomienia testów
    for h, exp in (("13", ["0", "1", "1"]), ("00", ["0", "0", "0"])):
        assert [_gate(script, r, SCHEDULE="37 12 * * *", GATE_HOUR=h) for r in (full, part, none_)] == exp


def test_daily_gate_hour_overridable():
    """B-20261124-2: bramka daily.yml czyta GATE_HOUR (testy przed 11:00 UTC nie mogą zależeć od zegara)."""
    wf, _ = _wf("daily.yml")
    assert "GATE_HOUR" in _step(wf, "update", "need")["run"]


def test_evening_commit_title_from_forecast_issued():
    text = (WF / "shared_weather.yml").read_text(encoding="utf-8")
    assert "forecast_issued" in text and 'Wspólna pogoda ${{ matrix.job }} ${D:-$(date -u +%Y-%m-%d)}' in text
