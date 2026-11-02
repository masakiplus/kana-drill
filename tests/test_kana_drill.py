# 通信しない・playwright 不要のセルフテスト。python3 -m pytest tests/ か python3 tests/test_kana_drill.py
import datetime, os, pathlib, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "kana_drill.py"


def run(args, env):
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, cwd=ROOT)


def test_pools_validate_and_html_generates():
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "KANA_DRILL_DIR": d, "KANA_DRILL_NAME": "テスト"}
        r = run(["--html-only", "--date", "2026-01-15"], env)
        assert r.returncode == 0, r.stderr
        out = pathlib.Path(d) / "out"
        html = (out / "kana_drill_2026-01-15.html").read_text(encoding="utf-8")
        ans = (out / "kana_drill_2026-01-15_answer.html").read_text(encoding="utf-8")
        assert "テストの カタカナ" in html and "こたえ" in ans
        # 既定の配分: K3 / KC2 / KE4、A は review が空なので 0
        assert html.count('class="krow"') == 3
        assert html.count('class="crow"') == 2
        assert html.count('class="erow') == 4
        assert "ひらがな ふくしゅう" not in html


def test_out_option_on_fresh_dir_and_bad_inputs():
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "KANA_DRILL_DIR": os.path.join(d, "fresh")}
        r = run(["--html-only", "--date", "2026-02-01", "--out", os.path.join(d, "x")], env)
        assert r.returncode == 0, r.stderr
        assert (pathlib.Path(d) / "x/kana_drill_2026-02-01.html").exists()
        r = run(["--html-only", "--date", "notadate"], env)
        assert r.returncode != 0 and "YYYY-MM-DD" in r.stderr
        r = run(["--html-only"], {**env, "KANA_DRILL_N_K": "abc"})
        assert r.returncode != 0 and "KANA_DRILL_N_K" in r.stderr


def test_srs_mark_splits_joined_chars():
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "KANA_DRILL_DIR": d}
        assert run(["srs-mark", "miss", "のは"], env).returncode == 0
        review = (pathlib.Path(d) / "review.txt").read_text(encoding="utf-8").split()
        assert review == ["の", "は"]


def test_srs_mark_and_review_section_appears():
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "KANA_DRILL_DIR": d}
        r = run(["srs-mark", "miss", "の", "は"], env)
        assert r.returncode == 0, r.stderr
        state = (pathlib.Path(d) / "review_state.json").read_text(encoding="utf-8")
        assert '"の"' in state and '"stage": 0' in state
        # miss の翌日は due なので、翌日のドリルに A（ひらがな ふくしゅう）が出る
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        r = run(["--html-only", "--date", tomorrow], env)
        assert r.returncode == 0, r.stderr
        html = (pathlib.Path(d) / f"out/kana_drill_{tomorrow}.html").read_text(encoding="utf-8")
        assert "ひらがな ふくしゅう" in html and html.count('class="arow"') == 2
        # correct を4回で卒業（due が null）
        for _ in range(4):
            assert run(["srs-mark", "correct", "の"], env).returncode == 0
        r = run(["srs-status"], env)
        assert "卒業した字: の" in r.stdout


def test_env_overrides_counts():
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "KANA_DRILL_DIR": d, "KANA_DRILL_N_K": "1", "KANA_DRILL_N_KC": "0", "KANA_DRILL_N_KE": "2"}
        r = run(["--html-only", "--date", "2026-03-03"], env); assert r.returncode == 0, r.stderr
        html = (pathlib.Path(d) / "out/kana_drill_2026-03-03.html").read_text(encoding="utf-8")
        assert html.count('class="krow"') == 1 and html.count('class="crow"') == 0 and html.count('class="erow') == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
