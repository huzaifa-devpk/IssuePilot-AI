from app import github, llm, scoring


def test_parse_issue_url():
    assert github.parse_issue_url("https://github.com/kestra-io/kestra/issues/20333") == ("kestra-io", "kestra", 20333)


def test_bad_url():
    try:
        github.parse_issue_url("https://example.com/x")
        assert False
    except ValueError:
        pass


ISSUE_T = "Login button crashes when `validateToken` returns null"
ISSUE_B = "Steps: open src/auth/login.py and click login. Stack trace points to validateToken in auth_utils."


def test_ranking_prefers_named_file():
    sig = scoring.extract_signals(ISSUE_T, ISSUE_B)
    good = scoring.full_score("src/auth/login.py", "def validateToken(t):\n  return None  # login", sig)
    bad = scoring.full_score("docs/changelog.md", "release notes", sig)
    assert good["score"] > bad["score"] + 0.3
    assert good["parts"]["R"] > 0 and good["parts"]["C"] > 0


def test_weights_sum_to_one():
    assert abs(sum(scoring.WEIGHTS.values()) - 1) < 1e-9


def test_json_extraction_handles_think_tags():
    raw = '<think>hmm</think>```json\n{"summary": "ok"}\n```'
    assert llm._extract_json(raw)["summary"] == "ok"


def test_normalise_never_invents_paths():
    r = llm.normalise({"why_files": {"fake.py": "x", "a.py": "y"}}, ["a.py"])
    assert list(r["why_files"]) == ["a.py"]
